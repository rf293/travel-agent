"""LetsFG cloud flight search (PFS Bearer or Developer API).

PFS (preferred): POST https://letsfg.co/api/search + poll /api/results/<id>
  Auth: LETSFG_BEARER_TOKEN or ~/.letsfg/config.json (from `letsfg auth`).

Developer API (fallback): POST /developers/api/v1/flights/search
  Auth: LETSFG_API_KEY — prepaid credits; avoid unless intentionally using it.

Search is free on PFS. Do not call register/setup-payment from agent code.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from scripts.lib.env import load_repo_env
from scripts.lib.models import FareOption, FlightLeg, SourceStatus

PFS_BASE = os.environ.get("LETSFG_BASE_URL", "https://letsfg.co").rstrip("/")
DEV_BASE = f"{PFS_BASE}/developers"
USER_AGENT = "TravelAgent-LetsFG/1.0"
POLL_INTERVAL = 2
MAX_POLLS = 90
LATE_MERGE_INTERVAL = 3
LATE_MERGE_GRACE = 90
IN_PROGRESS = ("pending", "running", "searching")

load_repo_env()


class LetsFGProvider:
    name = "letsfg"

    def __init__(
        self,
        bearer_token: str | None = None,
        api_key: str | None = None,
        wait_for_split: bool | None = None,
    ):
        self.bearer_token = (bearer_token or self._resolve_bearer() or "").strip()
        self.api_key = (api_key or os.getenv("LETSFG_API_KEY", "")).strip()
        if wait_for_split is None:
            wait_for_split = os.getenv("LETSFG_WAIT_FOR_SPLIT", "1").strip() != "0"
        self.wait_for_split = wait_for_split

    @staticmethod
    def _resolve_bearer() -> str:
        env = os.getenv("LETSFG_BEARER_TOKEN", "").strip()
        if env:
            return env
        config_path = Path.home() / ".letsfg" / "config.json"
        if not config_path.exists():
            return ""
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        auth = data.get("pfs_auth") or {}
        token = str(auth.get("token") or "").strip()
        expires_at = float(auth.get("expires_at") or 0)
        if token and time.time() < expires_at - 300:
            return token
        return token  # may still work; refresh is handled by `letsfg auth`

    @property
    def available(self) -> bool:
        return bool(self.bearer_token or self.api_key)

    @property
    def auth_mode(self) -> str:
        if self.bearer_token:
            return "pfs"
        if self.api_key:
            return "developer"
        return "none"

    def search_round_trip_options(
        self,
        origin: str,
        destination: str,
        outbound: str,
        return_date: str,
        currency: str = "USD",
        adults: int = 1,
        stops_rule: str = "any",
        limit: int = 50,
    ) -> tuple[list[FareOption], SourceStatus, str]:
        return self._search_options(
            origin=origin,
            destination=destination,
            date_from=outbound,
            return_date=return_date,
            currency=currency,
            adults=adults,
            stops_rule=stops_rule,
            limit=limit,
            construction="round_trip",
        )

    def search_one_way_options(
        self,
        origin: str,
        destination: str,
        date: str,
        currency: str = "USD",
        adults: int = 1,
        stops_rule: str = "any",
        label: str = "one_way_out",
        limit: int = 50,
    ) -> tuple[list[FareOption], SourceStatus, str]:
        options, status, err = self._search_options(
            origin=origin,
            destination=destination,
            date_from=date,
            return_date=None,
            currency=currency,
            adults=adults,
            stops_rule=stops_rule,
            limit=limit,
            construction=label,
        )
        for option in options:
            option.construction = label
            if not option.self_transfer and not _is_split(option.raw):
                option.ticketing = "two_tickets"
        return options, status, err

    def _search_options(
        self,
        *,
        origin: str,
        destination: str,
        date_from: str,
        return_date: str | None,
        currency: str,
        adults: int,
        stops_rule: str,
        limit: int,
        construction: str,
    ) -> tuple[list[FareOption], SourceStatus, str]:
        if not self.available:
            return [], SourceStatus.NO_KEY, "LETSFG_BEARER_TOKEN or LETSFG_API_KEY not set"

        payload: dict[str, Any] = {
            "origin": origin.upper(),
            "destination": destination.upper(),
            "date_from": date_from,
            "adults": adults,
            "currency": currency,
            "limit": max(1, min(int(limit), 100)),
            "sort": "price",
            "cabin_class": "M",
        }
        max_stops = _max_stopovers(stops_rule)
        if max_stops is not None:
            payload["max_stopovers"] = max_stops
        if return_date:
            payload["return_date"] = return_date

        data, status, err = self._run_search(payload)
        if not data:
            return [], status, err

        offers = data.get("offers") or []
        if not offers:
            return [], SourceStatus.NO_RESULTS, "no offers in response"

        options = [
            option
            for option in (
                self._option_from_offer(
                    offer,
                    currency=currency,
                    construction=construction,
                    is_round_trip=bool(return_date),
                    search_meta=data,
                )
                for offer in offers
            )
            if option is not None
        ]
        if not options:
            return [], SourceStatus.NO_RESULTS, "no priced offers parsed"
        return options, SourceStatus.OK, ""

    def _run_search(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, SourceStatus, str]:
        if self.bearer_token:
            return self._pfs_search(payload)
        return self._developer_search(payload)

    def _pfs_search(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, SourceStatus, str]:
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "User-Agent": USER_AGENT,
            "X-Client-Type": "travel-agent",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            started = requests.post(
                f"{PFS_BASE}/api/search",
                headers=headers,
                json=payload,
                timeout=60,
            )
            if started.status_code in (401, 403):
                return None, SourceStatus.BLOCKED, f"HTTP {started.status_code}: {started.text[:200]}"
            if started.status_code == 429:
                return None, SourceStatus.BLOCKED, f"HTTP 429: {started.text[:200]}"
            if started.status_code == 402:
                return None, SourceStatus.BLOCKED, "payment method required — run letsfg auth"
            if started.status_code >= 400:
                return None, SourceStatus.ERROR, f"HTTP {started.status_code}: {started.text[:300]}"
            body = started.json()
        except requests.RequestException as exc:
            return None, SourceStatus.ERROR, str(exc)

        search_id = body.get("search_id") or body.get("id")
        if not search_id:
            if body.get("offers"):
                return body, SourceStatus.OK, ""
            return None, SourceStatus.ERROR, "missing search_id"

        get_headers = {k: v for k, v in headers.items() if k != "Content-Type"}
        terminal: dict[str, Any] | None = None
        for i in range(MAX_POLLS):
            if i:
                time.sleep(POLL_INTERVAL)
            try:
                poll = requests.get(
                    f"{PFS_BASE}/api/results/{search_id}",
                    headers=get_headers,
                    timeout=30,
                )
            except requests.RequestException as exc:
                return None, SourceStatus.ERROR, str(exc)
            if poll.status_code in (401, 403, 429):
                return None, SourceStatus.BLOCKED, f"HTTP {poll.status_code}"
            if poll.status_code >= 400:
                return None, SourceStatus.ERROR, f"HTTP {poll.status_code}: {poll.text[:200]}"
            data = poll.json()
            if data.get("status", "") not in IN_PROGRESS:
                terminal = data
                break

        if terminal is None:
            return {"offers": [], "search_id": search_id, "status": "timeout"}, SourceStatus.ERROR, "poll timeout"

        waited = 0
        while (
            self.wait_for_split
            and (terminal.get("split_ticket_pending") or terminal.get("gf_enrich_pending"))
            and waited < LATE_MERGE_GRACE
        ):
            time.sleep(LATE_MERGE_INTERVAL)
            waited += LATE_MERGE_INTERVAL
            try:
                poll = requests.get(
                    f"{PFS_BASE}/api/results/{search_id}",
                    headers=get_headers,
                    timeout=30,
                )
                if poll.status_code >= 400:
                    break
                merged = poll.json()
            except requests.RequestException:
                break
            if merged.get("status", "") not in IN_PROGRESS:
                terminal = merged

        terminal.setdefault("search_id", search_id)
        return terminal, SourceStatus.OK, ""

    def _developer_search(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, SourceStatus, str]:
        headers = {
            "X-API-Key": self.api_key,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # Developer API uses return_from rather than return_date in some docs;
        # send both when a return is present.
        body = dict(payload)
        if body.get("return_date"):
            body.setdefault("return_from", body["return_date"])
        try:
            resp = requests.post(
                f"{DEV_BASE}/api/v1/flights/search",
                headers=headers,
                json=body,
                timeout=120,
            )
            if resp.status_code in (401, 403, 429):
                return None, SourceStatus.BLOCKED, f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code == 402:
                return None, SourceStatus.BLOCKED, "Developer API credits/payment required"
            if resp.status_code >= 400:
                return None, SourceStatus.ERROR, f"HTTP {resp.status_code}: {resp.text[:300]}"
            return resp.json(), SourceStatus.OK, ""
        except requests.RequestException as exc:
            return None, SourceStatus.ERROR, str(exc)

    def _option_from_offer(
        self,
        offer: dict[str, Any],
        *,
        currency: str,
        construction: str,
        is_round_trip: bool,
        search_meta: dict[str, Any],
    ) -> FareOption | None:
        price = _price(offer)
        if price is None or price <= 0:
            return None

        outbound_segments, inbound_segments = _route_segments(offer)
        if not outbound_segments and not offer.get("origin"):
            return None

        all_segment_dicts = outbound_segments + inbound_segments
        legs = [_leg_from_segment(seg, offer, index=i) for i, seg in enumerate(all_segment_dicts)]
        if not legs and offer.get("origin") and offer.get("destination"):
            legs = [
                FlightLeg(
                    origin=str(offer.get("origin") or ""),
                    destination=str(offer.get("destination") or ""),
                    departure_time=str(offer.get("departure_time") or offer.get("departure") or ""),
                    arrival_time=str(offer.get("arrival_time") or offer.get("arrival") or ""),
                    airline=_airline_code(offer),
                    operating_airline=_airline_code(offer),
                    marketing_airline=_airline_code(offer),
                    flight_number=str(offer.get("flight_number") or ""),
                    duration_minutes=int(offer.get("duration_minutes") or 0),
                )
            ]

        out_stops = _stops_for_route(outbound_segments, offer.get("stops"))
        in_stops = _stops_for_route(inbound_segments, None) if inbound_segments else None
        stops_by_leg = [out_stops]
        if is_round_trip or inbound_segments:
            stops_by_leg.append(in_stops if in_stops is not None else 0)

        layovers_by_leg = [
            _layovers(outbound_segments),
        ]
        if inbound_segments:
            layovers_by_leg.append(_layovers(inbound_segments))
        elif is_round_trip:
            layovers_by_leg.append([])

        leg_departure_times: list[str] = []
        leg_arrival_times: list[str] = []
        if outbound_segments:
            leg_departure_times.append(_seg_time(outbound_segments[0], "departure"))
            leg_arrival_times.append(_seg_time(outbound_segments[-1], "arrival"))
        elif legs:
            leg_departure_times.append(legs[0].departure_time)
            leg_arrival_times.append(legs[0].arrival_time)
        if inbound_segments:
            leg_departure_times.append(_seg_time(inbound_segments[0], "departure"))
            leg_arrival_times.append(_seg_time(inbound_segments[-1], "arrival"))

        airlines = _airlines(offer, all_segment_dicts)
        split = _is_split(offer)
        self_transfer = _is_self_transfer(offer)
        duration = int(offer.get("duration_minutes") or 0)
        if not duration:
            duration = sum(leg.duration_minutes for leg in legs)

        carry_on, checked, bag_fees = _bag_info(offer)
        offer_currency = str(offer.get("currency") or currency)
        ticketing = "two_tickets" if split else "one_ticket"
        cons = "round_trip" if (is_round_trip or inbound_segments) else construction
        if split and not inbound_segments:
            cons = construction if construction.startswith("one_way") else "one_way_out"

        source_tag = str(offer.get("source") or offer.get("provider") or self.name)
        option = FareOption(
            construction=cons,
            ticketing=ticketing,
            price=float(price),
            currency=offer_currency,
            airlines=airlines,
            stops=sum(stops_by_leg),
            duration_minutes=duration,
            legs=legs,
            source=f"{self.name}:{source_tag}" if source_tag != self.name else self.name,
            stops_by_leg=stops_by_leg,
            layovers_by_leg=layovers_by_leg,
            leg_departure_times=leg_departure_times,
            leg_arrival_times=leg_arrival_times,
            self_transfer=self_transfer or split,
            carry_on_included=carry_on,
            checked_bag_included=checked,
            bag_fees=bag_fees,
            raw={
                **offer,
                "_letsfg_search_id": search_meta.get("search_id"),
                "_letsfg_source_tiers": search_meta.get("source_tiers"),
                "_letsfg_split": split,
                "_letsfg_auth_mode": self.auth_mode,
            },
        )
        return option


def _max_stopovers(rule: str) -> int | None:
    if rule in ("nonstop", "nonstop_preferred"):
        return 0
    if rule == "max_1":
        return 1
    if rule == "max_2":
        return 2
    if rule == "any":
        return 2
    return None


def _price(offer: dict[str, Any]) -> float | None:
    for key in ("price", "total_price", "amount", "price_normalized"):
        value = offer.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _is_split(offer: dict[str, Any]) -> bool:
    conditions = offer.get("conditions") or {}
    if isinstance(conditions, dict):
        if str(conditions.get("split_ticket", "")).lower() in {"true", "1", "yes"}:
            return True
        if str(conditions.get("combo_type", "")).lower() in {
            "virtual_interlining",
            "split",
            "split_ticket",
        }:
            return True
    if offer.get("split_ticket") in (True, "true", "1", 1):
        return True
    if str(offer.get("combo_type", "")).lower() in {"virtual_interlining", "split", "split_ticket"}:
        return True
    if offer.get("is_combo") is True and _is_self_transfer(offer):
        return True
    return False


def _is_self_transfer(offer: dict[str, Any]) -> bool:
    conditions = offer.get("conditions") or {}
    if isinstance(conditions, dict):
        value = conditions.get("self_transfer")
        if value in ("unprotected", "protected", True, "true"):
            return True
    value = offer.get("self_transfer")
    return value in ("unprotected", "protected", True, "true")


def _route_segments(offer: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    outbound = offer.get("outbound") or {}
    inbound = offer.get("inbound") or {}
    if isinstance(outbound, dict) and outbound.get("segments"):
        out_segs = list(outbound.get("segments") or [])
        in_segs = list(inbound.get("segments") or []) if isinstance(inbound, dict) else []
        return out_segs, in_segs

    segments = list(offer.get("segments") or [])
    if isinstance(inbound, dict) and inbound.get("segments"):
        return segments, list(inbound.get("segments") or [])
    if isinstance(inbound, list):
        return segments, list(inbound)

    # Some RT payloads nest return under trip_breakdown legs.
    return segments, []


def _airline_code(obj: dict[str, Any]) -> str:
    for key in ("airline_code", "airline", "owner_airline", "carrier"):
        value = obj.get(key)
        if not value:
            continue
        text = str(value).strip()
        if len(text) <= 3 and text.isalpha():
            return text.upper()
        # Prefer explicit code fields when airline is a name.
        if key == "airline_code":
            return text.upper()[:3]
    code = obj.get("airline_code")
    return str(code).upper() if code else ""


def _airlines(offer: dict[str, Any], segments: list[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    listed = offer.get("airlines")
    if isinstance(listed, list):
        for item in listed:
            code = str(item).strip().upper()
            if code and code not in codes:
                codes.append(code if len(code) <= 3 else _airline_code({"airline": code}) or code[:3])
    for seg in segments:
        code = _airline_code(seg)
        if code and code not in codes:
            codes.append(code)
    top = _airline_code(offer)
    if top and top not in codes:
        codes.insert(0, top)
    return codes


def _seg_time(seg: dict[str, Any], kind: str) -> str:
    if kind == "departure":
        return str(
            seg.get("departure")
            or seg.get("departure_time")
            or seg.get("departing_at")
            or ""
        )
    return str(
        seg.get("arrival")
        or seg.get("arrival_time")
        or seg.get("arriving_at")
        or ""
    )


def _stops_for_route(segments: list[dict[str, Any]], fallback: Any) -> int:
    if segments:
        return max(0, len(segments) - 1)
    if fallback is None or fallback == "":
        return 0
    try:
        return max(0, int(fallback))
    except (TypeError, ValueError):
        return 0


def _minutes_between(start: str, end: str) -> int:
    if not start or not end:
        return 0
    try:
        departure = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        arrival = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        return max(0, int((arrival - departure).total_seconds() // 60))
    except (TypeError, ValueError):
        return 0


def _layovers(segments: list[dict[str, Any]]) -> list[int]:
    layovers: list[int] = []
    for previous, following in zip(segments, segments[1:]):
        layovers.append(
            _minutes_between(_seg_time(previous, "arrival"), _seg_time(following, "departure"))
        )
    return layovers


def _leg_from_segment(seg: dict[str, Any], offer: dict[str, Any], *, index: int) -> FlightLeg:
    origin = str(seg.get("origin") or "")
    destination = str(seg.get("destination") or "")
    if not origin and index == 0:
        origin = str(offer.get("origin") or "")
    if not destination:
        # Sparse fixture segments often only carry destination.
        destination = str(seg.get("destination") or offer.get("destination") or "")
    airline = _airline_code(seg) or _airline_code(offer)
    duration = int(seg.get("duration_seconds") or 0) // 60
    if not duration:
        duration = _minutes_between(_seg_time(seg, "departure"), _seg_time(seg, "arrival"))
    flight_no = str(seg.get("flight_no") or seg.get("flight_number") or "")
    if flight_no and airline and not flight_no.upper().startswith(airline):
        flight_no = f"{airline}{flight_no}"
    return FlightLeg(
        origin=origin,
        destination=destination,
        departure_time=_seg_time(seg, "departure"),
        arrival_time=_seg_time(seg, "arrival"),
        airline=airline,
        operating_airline=airline,
        marketing_airline=airline,
        flight_number=flight_no,
        duration_minutes=duration,
    )


def _bag_info(offer: dict[str, Any]) -> tuple[bool | None, bool | None, float]:
    """Return carry-on / checked inclusion. Do not treat optional bag add-ons as fees."""
    ancillaries = offer.get("ancillaries") or {}
    carry: bool | None = None
    checked: bool | None = None

    cabin = ancillaries.get("cabin_bag") if isinstance(ancillaries, dict) else None
    checked_bag = ancillaries.get("checked_bag") if isinstance(ancillaries, dict) else None
    if isinstance(cabin, dict) and "included" in cabin:
        carry = bool(cabin.get("included"))
    if isinstance(checked_bag, dict) and "included" in checked_bag:
        checked = bool(checked_bag.get("included"))

    return carry, checked, 0.0

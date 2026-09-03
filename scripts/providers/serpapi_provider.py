"""SerpAPI Google Flights provider."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import requests

from scripts.lib.env import load_repo_env
from scripts.lib.models import (
    FareOption,
    FlightLeg,
    SourceStatus,
    parse_duration,
    stops_param,
)

SERPAPI_URL = "https://serpapi.com/search"

load_repo_env()


class SerpApiProvider:
    name = "serpapi"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("SERPAPI_API_KEY", "")

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get(self, params: dict[str, Any]) -> tuple[dict[str, Any] | None, SourceStatus, str]:
        if not self.api_key:
            return None, SourceStatus.NO_KEY, "SERPAPI_API_KEY not set"
        params = {**params, "api_key": self.api_key, "engine": "google_flights"}
        try:
            resp = requests.get(SERPAPI_URL, params=params, timeout=60)
            if resp.status_code in (403, 429):
                return None, SourceStatus.BLOCKED, f"HTTP {resp.status_code}"
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                err = str(data["error"]).lower()
                if "blocked" in err or "captcha" in err:
                    return None, SourceStatus.BLOCKED, data["error"]
                return None, SourceStatus.ERROR, data["error"]
            return data, SourceStatus.OK, ""
        except requests.RequestException as exc:
            msg = str(exc).lower()
            if "403" in msg or "429" in msg:
                return None, SourceStatus.BLOCKED, str(exc)
            return None, SourceStatus.ERROR, str(exc)

    def search_round_trip(
        self,
        origin: str,
        destination: str,
        outbound: str,
        return_date: str,
        currency: str = "USD",
        adults: int = 1,
        stops_rule: str = "any",
        gl: str = "us",
    ) -> tuple[FareOption | None, SourceStatus, str]:
        stops = stops_param(stops_rule)
        params: dict[str, Any] = {
            "type": "1",
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": outbound,
            "return_date": return_date,
            "currency": currency,
            "adults": str(adults),
            "travel_class": "1",
            "hl": "en",
            "gl": gl,
            "deep_search": "true",
        }
        if stops is not None and stops_rule != "nonstop_preferred":
            params["stops"] = str(stops)

        data, status, err = self._get(params)
        if not data:
            return None, status, err

        option = min(
            self._round_trip_options(data, params, origin, destination, currency),
            key=lambda candidate: candidate.total_price,
            default=None,
        )
        if not option:
            return None, SourceStatus.NO_RESULTS, "no flights in response"
        option.source = self.name
        option.construction = "round_trip"
        option.ticketing = "one_ticket"
        return option, SourceStatus.OK, ""

    def search_round_trip_options(
        self,
        origin: str,
        destination: str,
        outbound: str,
        return_date: str,
        currency: str = "USD",
        adults: int = 1,
        stops_rule: str = "any",
        gl: str = "us",
    ) -> tuple[list[FareOption], SourceStatus, str]:
        """Return every priced RT group so constraints run before selection."""
        stops = stops_param(stops_rule)
        params: dict[str, Any] = {
            "type": "1",
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": outbound,
            "return_date": return_date,
            "currency": currency,
            "adults": str(adults),
            "travel_class": "1",
            "hl": "en",
            "gl": gl,
            "deep_search": "true",
        }
        if stops is not None and stops_rule != "nonstop_preferred":
            params["stops"] = str(stops)
        data, status, err = self._get(params)
        if not data:
            return [], status, err
        options = self._round_trip_options(
            data, params, origin, destination, currency
        )
        if not options:
            return [], SourceStatus.NO_RESULTS, "no flights in response"
        for option in options:
            option.source = self.name
            option.construction = "round_trip"
            option.ticketing = "one_ticket"
        return options, SourceStatus.OK, ""

    def search_one_way(
        self,
        origin: str,
        destination: str,
        date: str,
        currency: str = "USD",
        adults: int = 1,
        stops_rule: str = "any",
        gl: str = "us",
        leg_label: str = "one_way",
    ) -> tuple[FareOption | None, SourceStatus, str]:
        stops = stops_param(stops_rule)
        params: dict[str, Any] = {
            "type": "2",
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": date,
            "currency": currency,
            "adults": str(adults),
            "travel_class": "1",
            "hl": "en",
            "gl": gl,
            "deep_search": "true",
        }
        if stops is not None and stops_rule != "nonstop_preferred":
            params["stops"] = str(stops)

        data, status, err = self._get(params)
        if not data:
            return None, status, err

        option = min(
            self._options_from_ow(data, origin, destination, currency, leg_label),
            key=lambda candidate: candidate.total_price,
            default=None,
        )
        if not option:
            return None, SourceStatus.NO_RESULTS, "no flights in response"
        option.source = self.name
        return option, SourceStatus.OK, ""

    def search_one_way_options(
        self,
        origin: str,
        destination: str,
        date: str,
        currency: str = "USD",
        adults: int = 1,
        stops_rule: str = "any",
        gl: str = "us",
        leg_label: str = "one_way",
    ) -> tuple[list[FareOption], SourceStatus, str]:
        """Return every priced OW group so constraints run before selection."""
        stops = stops_param(stops_rule)
        params: dict[str, Any] = {
            "type": "2",
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": date,
            "currency": currency,
            "adults": str(adults),
            "travel_class": "1",
            "hl": "en",
            "gl": gl,
            "deep_search": "true",
        }
        if stops is not None and stops_rule != "nonstop_preferred":
            params["stops"] = str(stops)
        data, status, err = self._get(params)
        if not data:
            return [], status, err
        options = self._options_from_ow(data, origin, destination, currency, leg_label)
        if not options:
            return [], SourceStatus.NO_RESULTS, "no flights in response"
        for option in options:
            option.source = self.name
        return options, SourceStatus.OK, ""

    def search_multi_city(
        self,
        legs: list[tuple[str, str, str]],
        currency: str = "USD",
        adults: int = 1,
        stops_rule: str = "any",
        gl: str = "us",
    ) -> tuple[FareOption | None, SourceStatus, str]:
        multi = [
            {"departure_id": o, "arrival_id": d, "date": dt}
            for o, d, dt in legs
        ]
        stops = stops_param(stops_rule)
        params: dict[str, Any] = {
            "type": "3",
            "multi_city_json": json.dumps(multi),
            "currency": currency,
            "adults": str(adults),
            "travel_class": "1",
            "hl": "en",
            "gl": gl,
            "deep_search": "true",
        }
        if stops is not None:
            params["stops"] = str(stops)

        data, status, err = self._get(params)
        if not data:
            return None, status, err

        option = min(
            self._options_from_multi(data, legs, currency),
            key=lambda candidate: candidate.total_price,
            default=None,
        )
        if not option:
            return None, SourceStatus.NO_RESULTS, "no flights in response"
        option.source = self.name
        option.construction = "multi_city"
        option.ticketing = "one_ticket"
        return option, SourceStatus.OK, ""

    def search_multi_city_options(
        self,
        legs: list[tuple[str, str, str]],
        currency: str = "USD",
        adults: int = 1,
        stops_rule: str = "any",
        gl: str = "us",
    ) -> tuple[list[FareOption], SourceStatus, str]:
        """Return every priced multi-city group for post-fetch filtering."""
        multi = [
            {"departure_id": origin, "arrival_id": destination, "date": date}
            for origin, destination, date in legs
        ]
        stops = stops_param(stops_rule)
        params: dict[str, Any] = {
            "type": "3",
            "multi_city_json": json.dumps(multi),
            "currency": currency,
            "adults": str(adults),
            "travel_class": "1",
            "hl": "en",
            "gl": gl,
            "deep_search": "true",
        }
        if stops is not None:
            params["stops"] = str(stops)
        data, status, err = self._get(params)
        if not data:
            return [], status, err
        options = self._options_from_multi(data, legs, currency)
        if not options:
            return [], SourceStatus.NO_RESULTS, "no flights in response"
        for option in options:
            option.source = self.name
            option.construction = "multi_city"
            option.ticketing = "one_ticket"
        return options, SourceStatus.OK, ""

    def _iter_groups(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        return list(data.get("best_flights") or []) + list(data.get("other_flights") or [])

    def _round_trip_options(
        self,
        outbound_data: dict[str, Any],
        params: dict[str, Any],
        origin: str,
        destination: str,
        currency: str,
    ) -> list[FareOption]:
        """Resolve each outbound token into priced return pairings."""
        paired_groups: list[dict[str, Any]] = []
        unpaired_groups: list[dict[str, Any]] = []
        for outbound in self._iter_groups(outbound_data):
            token = outbound.get("departure_token")
            if not token:
                if len(outbound.get("flights") or []) >= 2:
                    unpaired_groups.append(outbound)
                continue
            return_params = {**params, "departure_token": token}
            return_data, status, _ = self._get(return_params)
            if not return_data or status != SourceStatus.OK:
                continue
            for returning in self._iter_groups(return_data):
                return_flights = returning.get("flights") or []
                if not return_flights or self._price_from_group(returning) <= 0:
                    continue
                paired_groups.append(
                    {
                        "price": self._price_from_group(returning),
                        "flights": (outbound.get("flights") or []) + return_flights,
                        "extensions": list(
                            dict.fromkeys(
                                (outbound.get("extensions") or [])
                                + (returning.get("extensions") or [])
                            )
                        ),
                        "outbound": outbound,
                        "return": returning,
                    }
                )
        options = self._options_from_rt(
            {"best_flights": paired_groups + unpaired_groups},
            origin,
            destination,
            currency,
        )
        return options

    def _segments_to_legs(self, segments: list[dict[str, Any]]) -> list[FlightLeg]:
        legs = []
        for seg in segments:
            legs.append(
                FlightLeg(
                    origin=seg.get("departure_airport", {}).get("id", ""),
                    destination=seg.get("arrival_airport", {}).get("id", ""),
                    departure_time=seg.get("departure_airport", {}).get("time", ""),
                    arrival_time=seg.get("arrival_airport", {}).get("time", ""),
                    airline=seg.get("airline", ""),
                    operating_airline=seg.get("operated_by", seg.get("airline", "")),
                    marketing_airline=seg.get("airline", ""),
                    flight_number=seg.get("flight_number", ""),
                    duration_minutes=parse_duration(seg.get("duration", "")),
                )
            )
        return legs

    def _airlines_from_segments(self, segments: list[dict[str, Any]]) -> list[str]:
        codes = []
        for seg in segments:
            code = seg.get("airline", "")
            if code and code not in codes:
                codes.append(code)
        return codes

    def _stops_from_flights(self, flights: list[dict[str, Any]]) -> int:
        total = 0
        for fl in flights:
            segs = _segments_for_flight(fl)
            total += max(0, len(segs) - 1)
        return total

    def _price_from_group(self, group: dict[str, Any]) -> float:
        return float(group.get("price") or 0)

    def _options_from_rt(
        self, data: dict[str, Any], origin: str, destination: str, currency: str
    ) -> list[FareOption]:
        options: list[FareOption] = []
        for group in self._iter_groups(data):
            price = self._price_from_group(group)
            if price <= 0:
                continue
            flights = group.get("flights") or []
            all_segments = []
            stops_by_leg = []
            layovers_by_leg = []
            leg_departure_times = []
            leg_arrival_times = []
            for fl in flights:
                segments = _segments_for_flight(fl)
                all_segments.extend(segments)
                stops_by_leg.append(max(0, len(segments) - 1))
                layovers_by_leg.append(_layovers_for_segments(segments))
                if segments:
                    leg_departure_times.append(
                        segments[0].get("departure_airport", {}).get("time", "")
                    )
                    leg_arrival_times.append(
                        segments[-1].get("arrival_airport", {}).get("time", "")
                    )
            stops = self._stops_from_flights(flights)
            duration = sum(parse_duration(fl.get("duration", "")) for fl in flights)
            opt = FareOption(
                construction="round_trip",
                ticketing="one_ticket",
                price=price,
                currency=currency,
                airlines=self._airlines_from_segments(all_segments),
                stops=stops,
                duration_minutes=duration,
                legs=self._segments_to_legs(all_segments),
                stops_by_leg=stops_by_leg,
                layovers_by_leg=layovers_by_leg,
                leg_departure_times=leg_departure_times,
                leg_arrival_times=leg_arrival_times,
                carry_on_included=_bag_status(group, "carry"),
                checked_bag_included=_bag_status(group, "checked"),
                bag_fees=_bag_fees(group),
                raw=group,
            )
            options.append(opt)
        return options

    def _options_from_ow(
        self,
        data: dict[str, Any],
        origin: str,
        destination: str,
        currency: str,
        leg_label: str,
    ) -> list[FareOption]:
        options: list[FareOption] = []
        for group in self._iter_groups(data):
            price = self._price_from_group(group)
            if price <= 0:
                continue
            flights = group.get("flights") or []
            segments = _segments_for_flight(flights[0]) if flights else []
            stops = max(0, len(segments) - 1)
            opt = FareOption(
                construction=leg_label,
                ticketing="two_tickets",
                price=price,
                currency=currency,
                airlines=self._airlines_from_segments(segments),
                stops=stops,
                duration_minutes=sum(parse_duration(s.get("duration", "")) for s in segments),
                legs=self._segments_to_legs(segments),
                stops_by_leg=[stops],
                layovers_by_leg=[_layovers_for_segments(segments)],
                leg_departure_times=[
                    segments[0].get("departure_airport", {}).get("time", "")
                ] if segments else [],
                leg_arrival_times=[
                    segments[-1].get("arrival_airport", {}).get("time", "")
                ] if segments else [],
                carry_on_included=_bag_status(group, "carry"),
                checked_bag_included=_bag_status(group, "checked"),
                bag_fees=_bag_fees(group),
                raw=group,
            )
            options.append(opt)
        return options

    def _options_from_multi(
        self, data: dict[str, Any], legs: list[tuple[str, str, str]], currency: str
    ) -> list[FareOption]:
        options: list[FareOption] = []
        for group in self._iter_groups(data):
            price = self._price_from_group(group)
            if price <= 0:
                continue
            flights = group.get("flights") or []
            all_segments = []
            stops_by_leg = []
            layovers_by_leg = []
            leg_departure_times = []
            leg_arrival_times = []
            for fl in flights:
                segments = _segments_for_flight(fl)
                all_segments.extend(segments)
                stops_by_leg.append(max(0, len(segments) - 1))
                layovers_by_leg.append(_layovers_for_segments(segments))
                if segments:
                    leg_departure_times.append(
                        segments[0].get("departure_airport", {}).get("time", "")
                    )
                    leg_arrival_times.append(
                        segments[-1].get("arrival_airport", {}).get("time", "")
                    )
            stops = self._stops_from_flights(flights)
            duration = sum(parse_duration(fl.get("duration", "")) for fl in flights)
            opt = FareOption(
                construction="multi_city",
                ticketing="one_ticket",
                price=price,
                currency=currency,
                airlines=self._airlines_from_segments(all_segments),
                stops=stops,
                duration_minutes=duration,
                legs=self._segments_to_legs(all_segments),
                stops_by_leg=stops_by_leg,
                layovers_by_leg=layovers_by_leg,
                leg_departure_times=leg_departure_times,
                leg_arrival_times=leg_arrival_times,
                carry_on_included=_bag_status(group, "carry"),
                checked_bag_included=_bag_status(group, "checked"),
                bag_fees=_bag_fees(group),
                raw=group,
            )
            options.append(opt)
        return options


def _bag_status(group: dict[str, Any], bag_type: str) -> bool | None:
    text = " ".join(str(value) for value in (group.get("extensions") or [])).lower()
    if bag_type == "carry":
        if "carry-on" not in text and "carry on" not in text:
            return None
        return (
            "0 carry-on" not in text
            and "0 carry on" not in text
            and "no carry-on" not in text
        )
    if "checked bag" not in text and "checked baggage" not in text:
        return None
    return "0 checked" not in text and "no checked" not in text


def _segments_for_flight(flight: dict[str, Any]) -> list[dict[str, Any]]:
    segments = flight.get("segments") or []
    return segments if segments else ([flight] if flight.get("departure_airport") else [])


def _layovers_for_segments(segments: list[dict[str, Any]]) -> list[int]:
    layovers: list[int] = []
    for previous, following in zip(segments, segments[1:]):
        arrival = previous.get("arrival_airport", {}).get("time", "")
        departure = following.get("departure_airport", {}).get("time", "")
        try:
            start = datetime.fromisoformat(str(arrival).replace("Z", ""))
            end = datetime.fromisoformat(str(departure).replace("Z", ""))
            layovers.append(max(0, int((end - start).total_seconds() // 60)))
        except (TypeError, ValueError):
            layovers.append(0)
    return layovers


def _bag_fees(group: dict[str, Any]) -> float:
    text = " ".join(str(value) for value in (group.get("extensions") or []))
    import re

    fees = re.findall(r"(?:bag|baggage)[^$0-9]{0,20}\$?\s*(\d+(?:\.\d+)?)", text, re.I)
    return sum(float(fee) for fee in fees)

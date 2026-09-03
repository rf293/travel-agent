"""Duffel Flights API provider."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import requests

from scripts.lib.env import load_repo_env
from scripts.lib.models import FareOption, FlightLeg, SourceStatus

DUFFEL_API = "https://api.duffel.com"
DEFAULT_OFFER_LIMIT = 50

load_repo_env()


class DuffelProvider:
    name = "duffel"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("DUFFEL_API_KEY", "")

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Duffel-Version": "v2",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: int = 60,
    ) -> tuple[dict[str, Any] | None, SourceStatus, str]:
        if not self.api_key:
            return None, SourceStatus.NO_KEY, "DUFFEL_API_KEY not set"
        try:
            response = requests.request(
                method,
                f"{DUFFEL_API}{path}",
                headers=self._headers(),
                params=params,
                json=json_body,
                timeout=timeout,
            )
            if response.status_code in (403, 429):
                return None, SourceStatus.BLOCKED, f"HTTP {response.status_code}"
            if response.status_code >= 400:
                detail = response.text[:300]
                return None, SourceStatus.ERROR, f"HTTP {response.status_code}: {detail}"
            return response.json(), SourceStatus.OK, ""
        except requests.RequestException as exc:
            message = str(exc).lower()
            if "403" in message or "429" in message:
                return None, SourceStatus.BLOCKED, str(exc)
            return None, SourceStatus.ERROR, str(exc)

    def search_itinerary(
        self,
        legs: list[tuple[str, str, str]],
        currency: str = "USD",
        adults: int = 1,
        max_stops: int | None = None,
        construction: str = "multi_city",
    ) -> tuple[FareOption | None, SourceStatus, str]:
        options, status, err = self.search_itinerary_options(
            legs, currency=currency, adults=adults, max_stops=max_stops, construction=construction
        )
        if not options:
            return None, status, err
        return min(options, key=lambda option: option.total_price), status, ""

    def search_itinerary_options(
        self,
        legs: list[tuple[str, str, str]],
        currency: str = "USD",
        adults: int = 1,
        max_stops: int | None = None,
        construction: str = "multi_city",
    ) -> tuple[list[FareOption], SourceStatus, str]:
        if not legs:
            return [], SourceStatus.ERROR, "no legs provided"

        body: dict[str, Any] = {
            "data": {
                "cabin_class": "economy",
                "slices": [
                    {"origin": origin, "destination": destination, "departure_date": date}
                    for origin, destination, date in legs
                ],
                "passengers": [{"type": "adult"} for _ in range(adults)],
            }
        }
        max_connections = _max_connections(max_stops)
        if max_connections is not None:
            body["data"]["max_connections"] = max_connections

        created, status, err = self._request(
            "POST",
            "/air/offer_requests",
            params={"return_offers": "false"},
            json_body=body,
        )
        if not created:
            return [], status, err

        offer_request_id = (created.get("data") or {}).get("id")
        if not offer_request_id:
            return [], SourceStatus.ERROR, "missing offer_request id"

        params: dict[str, Any] = {
            "offer_request_id": offer_request_id,
            "sort": "total_amount",
            "limit": DEFAULT_OFFER_LIMIT,
        }
        if max_connections is not None:
            params["max_connections"] = max_connections

        offers_payload, status, err = self._request(
            "GET",
            "/air/offers",
            params=params,
        )
        if not offers_payload:
            return [], status, err

        offers = offers_payload.get("data") or []
        is_round_trip = (
            len(legs) == 2
            and legs[0][0] == legs[1][1]
            and legs[0][1] == legs[1][0]
        )
        options = self._options_from_offers(
            offers,
            currency,
            construction,
            is_round_trip,
        )
        if not options:
            return [], SourceStatus.NO_RESULTS, "no priced offers"
        for option in options:
            option.source = self.name
        return options, SourceStatus.OK, ""

    def _options_from_offers(
        self,
        offers: list[dict[str, Any]],
        currency: str,
        construction: str,
        is_round_trip: bool,
    ) -> list[FareOption]:
        options: list[FareOption] = []
        for offer in offers:
            total = float(offer.get("total_amount") or 0)
            if total <= 0:
                continue

            slices = offer.get("slices") or []
            all_legs: list[FlightLeg] = []
            airlines: list[str] = []
            stops = 0
            duration = 0
            stops_by_leg: list[int] = []
            layovers_by_leg: list[list[int]] = []
            leg_departure_times: list[str] = []
            leg_arrival_times: list[str] = []

            for slice_data in slices:
                segments = slice_data.get("segments") or []
                leg_stops = max(0, len(segments) - 1)
                stops += leg_stops
                stops_by_leg.append(leg_stops)
                layovers_by_leg.append(_layovers_for_segments(segments))
                if segments:
                    leg_departure_times.append(segments[0].get("departing_at", ""))
                    leg_arrival_times.append(segments[-1].get("arriving_at", ""))
                for segment in segments:
                    carrier = (segment.get("marketing_carrier") or {}).get("iata_code", "")
                    operating = (segment.get("operating_carrier") or {}).get("iata_code", carrier)
                    if carrier and carrier not in airlines:
                        airlines.append(carrier)
                    origin = (segment.get("origin") or {}).get("iata_code", "")
                    destination = (segment.get("destination") or {}).get("iata_code", "")
                    departing_at = segment.get("departing_at", "")
                    arriving_at = segment.get("arriving_at", "")
                    duration += _minutes_between(departing_at, arriving_at)
                    flight_number = segment.get("marketing_carrier_flight_number", "")
                    all_legs.append(
                        FlightLeg(
                            origin=origin,
                            destination=destination,
                            departure_time=departing_at,
                            arrival_time=arriving_at,
                            airline=carrier,
                            operating_airline=operating,
                            marketing_airline=carrier,
                            flight_number=f"{carrier}{flight_number}" if carrier else flight_number,
                            duration_minutes=_minutes_between(departing_at, arriving_at),
                        )
                    )

            cons = "round_trip" if is_round_trip else construction
            options.append(
                FareOption(
                    construction=cons,
                    ticketing="one_ticket",
                    price=total,
                    currency=offer.get("total_currency") or currency,
                    airlines=airlines,
                    stops=stops,
                    duration_minutes=duration,
                    legs=all_legs,
                    stops_by_leg=stops_by_leg,
                    layovers_by_leg=layovers_by_leg,
                    leg_departure_times=leg_departure_times,
                    leg_arrival_times=leg_arrival_times,
                    carry_on_included=_included_bag_status(offer, "carry_on"),
                    checked_bag_included=_included_bag_status(offer, "checked"),
                    raw=offer,
                )
            )
        return options


def _max_connections(max_stops: int | None) -> int | None:
    if max_stops is None:
        return 2
    return max(0, int(max_stops))


def _minutes_between(start: str, end: str) -> int:
    try:
        departure = datetime.fromisoformat(start.replace("Z", "+00:00"))
        arrival = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return max(0, int((arrival - departure).total_seconds() // 60))
    except (TypeError, ValueError):
        return 0


def _layovers_for_segments(segments: list[dict[str, Any]]) -> list[int]:
    layovers: list[int] = []
    for previous, following in zip(segments, segments[1:]):
        layovers.append(
            _minutes_between(previous.get("arriving_at", ""), following.get("departing_at", ""))
        )
    return layovers


def _included_bag_status(offer: dict[str, Any], bag_type: str) -> bool | None:
    quantities: list[int] = []
    for slice_data in offer.get("slices") or []:
        for segment in slice_data.get("segments") or []:
            for passenger in segment.get("passengers") or []:
                for baggage in passenger.get("baggages") or []:
                    if baggage.get("type") == bag_type:
                        quantities.append(int(baggage.get("quantity") or 0))
    if not quantities:
        return None
    return all(quantity > 0 for quantity in quantities)

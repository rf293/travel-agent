"""Amadeus Flight Offers Search provider."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import requests

from scripts.lib.env import load_repo_env
from scripts.lib.models import FareOption, FlightLeg, SourceStatus

AMADEUS_TEST = "https://test.api.amadeus.com"
AMADEUS_PROD = "https://api.amadeus.com"

load_repo_env()


class AmadeusProvider:
    name = "amadeus"

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        env: str | None = None,
    ):
        self.api_key = api_key or os.getenv("AMADEUS_API_KEY", "")
        self.api_secret = api_secret or os.getenv("AMADEUS_API_SECRET", "")
        self.env = env or os.getenv("AMADEUS_ENV", "test")
        self._token: str | None = None

    @property
    def base_url(self) -> str:
        return AMADEUS_PROD if self.env == "production" else AMADEUS_TEST

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _auth(self) -> tuple[str | None, SourceStatus, str]:
        if not self.available:
            return None, SourceStatus.NO_KEY, "AMADEUS_API_KEY/SECRET not set"
        if self._token:
            return self._token, SourceStatus.OK, ""
        try:
            resp = requests.post(
                f"{self.base_url}/v1/security/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.api_key,
                    "client_secret": self.api_secret,
                },
                timeout=30,
            )
            if resp.status_code in (403, 429):
                return None, SourceStatus.BLOCKED, f"HTTP {resp.status_code}"
            resp.raise_for_status()
            self._token = resp.json()["access_token"]
            return self._token, SourceStatus.OK, ""
        except requests.RequestException as exc:
            return None, SourceStatus.ERROR, str(exc)

    def _post_offers(
        self, body: dict[str, Any]
    ) -> tuple[list[dict[str, Any]] | None, SourceStatus, str]:
        token, status, err = self._auth()
        if not token:
            return None, status, err
        try:
            resp = requests.post(
                f"{self.base_url}/v2/shopping/flight-offers",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
                timeout=60,
            )
            if resp.status_code in (403, 429):
                return None, SourceStatus.BLOCKED, f"HTTP {resp.status_code}"
            resp.raise_for_status()
            data = resp.json()
            return data.get("data") or [], SourceStatus.OK, ""
        except requests.RequestException as exc:
            return None, SourceStatus.ERROR, str(exc)

    def search_itinerary(
        self,
        legs: list[tuple[str, str, str]],
        currency: str = "USD",
        adults: int = 1,
        max_stops: int | None = None,
        construction: str = "multi_city",
    ) -> tuple[FareOption | None, SourceStatus, str]:
        origin_destinations = []
        for i, (origin, dest, date) in enumerate(legs, start=1):
            origin_destinations.append(
                {
                    "id": str(i),
                    "originLocationCode": origin,
                    "destinationLocationCode": dest,
                    "departureDateTimeRange": {"date": date},
                }
            )

        body: dict[str, Any] = {
            "currencyCode": currency,
            "originDestinations": origin_destinations,
            "travelers": [{"id": str(i + 1), "travelerType": "ADULT"} for i in range(adults)],
            "sources": ["GDS"],
            "searchCriteria": {"maxFlightOffers": 15},
        }
        if max_stops is not None:
            body["searchCriteria"]["flightFilters"] = {
                "connectionRestriction": {"maxNumberOfConnections": max_stops}
            }

        offers, status, err = self._post_offers(body)
        if offers is None:
            return None, status, err
        if not offers:
            return None, SourceStatus.NO_RESULTS, "no offers"

        best = min(
            self._options_from_offers(
                offers,
                currency,
                construction,
                len(legs) == 2 and legs[0][0] == legs[1][1] and legs[0][1] == legs[1][0],
            ),
            key=lambda option: option.total_price,
            default=None,
        )
        if not best:
            return None, SourceStatus.NO_RESULTS, "no priced offers"
        best.source = self.name
        return best, SourceStatus.OK, ""

    def search_itinerary_options(
        self,
        legs: list[tuple[str, str, str]],
        currency: str = "USD",
        adults: int = 1,
        max_stops: int | None = None,
        construction: str = "multi_city",
    ) -> tuple[list[FareOption], SourceStatus, str]:
        """Return all priced offers so post-fetch rules can choose safely."""
        origin_destinations = [
            {
                "id": str(index),
                "originLocationCode": origin,
                "destinationLocationCode": destination,
                "departureDateTimeRange": {"date": date},
            }
            for index, (origin, destination, date) in enumerate(legs, start=1)
        ]
        body: dict[str, Any] = {
            "currencyCode": currency,
            "originDestinations": origin_destinations,
            "travelers": [{"id": str(index + 1), "travelerType": "ADULT"} for index in range(adults)],
            "sources": ["GDS"],
            "searchCriteria": {"maxFlightOffers": 50},
        }
        if max_stops is not None:
            body["searchCriteria"]["flightFilters"] = {
                "connectionRestriction": {"maxNumberOfConnections": max_stops}
            }
        offers, status, err = self._post_offers(body)
        if offers is None:
            return [], status, err
        options = self._options_from_offers(
            offers,
            currency,
            construction,
            len(legs) == 2 and legs[0][0] == legs[1][1] and legs[0][1] == legs[1][0],
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
            price_info = offer.get("price") or {}
            total = float(price_info.get("grandTotal") or price_info.get("total") or 0)
            if total <= 0:
                continue

            itineraries = offer.get("itineraries") or []
            all_legs: list[FlightLeg] = []
            airlines: list[str] = []
            stops = 0
            duration = 0
            stops_by_leg: list[int] = []
            layovers_by_leg: list[list[int]] = []
            leg_departure_times: list[str] = []
            leg_arrival_times: list[str] = []

            for itin in itineraries:
                segs = itin.get("segments") or []
                leg_stops = max(0, len(segs) - 1)
                stops += leg_stops
                stops_by_leg.append(leg_stops)
                layovers_by_leg.append(_layovers_for_segments(segs))
                duration += _parse_iso_duration(itin.get("duration", "PT0H0M"))
                if segs:
                    leg_departure_times.append((segs[0].get("departure") or {}).get("at", ""))
                    leg_arrival_times.append((segs[-1].get("arrival") or {}).get("at", ""))
                for seg in segs:
                    carrier = seg.get("carrierCode", "")
                    if carrier and carrier not in airlines:
                        airlines.append(carrier)
                    dep = seg.get("departure") or {}
                    arr = seg.get("arrival") or {}
                    all_legs.append(
                        FlightLeg(
                            origin=dep.get("iataCode", ""),
                            destination=arr.get("iataCode", ""),
                            departure_time=dep.get("at", ""),
                            arrival_time=arr.get("at", ""),
                            airline=carrier,
                            operating_airline=seg.get("operating", {}).get("carrierCode", carrier),
                            marketing_airline=carrier,
                            flight_number=f"{carrier}{seg.get('number', '')}",
                            duration_minutes=_parse_iso_duration(seg.get("duration", "PT0H0M")),
                        )
                    )

            cons = "round_trip" if is_round_trip else construction
            opt = FareOption(
                construction=cons,
                ticketing="one_ticket",
                price=total,
                currency=price_info.get("currency") or currency,
                airlines=airlines,
                stops=stops,
                duration_minutes=duration,
                legs=all_legs,
                stops_by_leg=stops_by_leg,
                layovers_by_leg=layovers_by_leg,
                leg_departure_times=leg_departure_times,
                leg_arrival_times=leg_arrival_times,
                carry_on_included=_included_bag_status(offer, "cabin"),
                checked_bag_included=_included_bag_status(offer, "checked"),
                raw=offer,
            )
            options.append(opt)
        return options

    def _best_offer(
        self,
        offers: list[dict[str, Any]],
        currency: str,
        construction: str,
        is_round_trip: bool,
    ) -> FareOption | None:
        return min(
            self._options_from_offers(offers, currency, construction, is_round_trip),
            key=lambda option: option.total_price,
            default=None,
        )


def _parse_iso_duration(value: str) -> int:
    """Parse PT14H35M to minutes."""
    if not value or not value.startswith("PT"):
        return 0
    value = value[2:]
    hours = 0
    minutes = 0
    if "H" in value:
        h, rest = value.split("H", 1)
        hours = int(h) if h else 0
        value = rest
    if "M" in value:
        m = value.replace("M", "")
        minutes = int(m) if m else 0
    return hours * 60 + minutes


def _layovers_for_segments(segments: list[dict[str, Any]]) -> list[int]:
    layovers: list[int] = []
    for previous, following in zip(segments, segments[1:]):
        try:
            arrival = datetime.fromisoformat(previous["arrival"]["at"].replace("Z", "+00:00"))
            departure = datetime.fromisoformat(following["departure"]["at"].replace("Z", "+00:00"))
            layovers.append(max(0, int((departure - arrival).total_seconds() // 60)))
        except (KeyError, TypeError, ValueError):
            layovers.append(0)
    return layovers


def _included_bag_status(offer: dict[str, Any], bag_type: str) -> bool | None:
    values: list[Any] = []
    for pricing in offer.get("travelerPricings") or []:
        for detail in pricing.get("fareDetailsBySegment") or []:
            if bag_type == "checked":
                values.append((detail.get("includedCheckedBags") or {}).get("quantity"))
            else:
                values.append((detail.get("includedCabinBags") or {}).get("quantity"))
    if not values:
        return None
    return all(value is not None and int(value) > 0 for value in values)

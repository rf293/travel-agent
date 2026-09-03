"""Shared models and helpers for flight search."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class QuoteStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


class SourceStatus(str, Enum):
    OK = "OK"
    BLOCKED = "BLOCKED"
    NO_RESULTS = "NO_RESULTS"
    NO_KEY = "NO_KEY"
    ERROR = "ERROR"


class MatchType(str, Enum):
    STRICT = "strict"
    NEAR_MATCH = "near_match"
    REJECTED = "rejected"


ALLIANCES = {
    "STAR_ALLIANCE": {
        "AC", "UA", "LH", "SN", "LX", "OS", "NH", "SQ", "TK", "CA", "AI",
    },
    "SKYTEAM": {"AF", "KL", "DL", "AM", "KE", "CI", "MU", "VS"},
    "ONEWORLD": {"BA", "AA", "QF", "CX", "JL", "IB", "AY", "QR"},
}


@dataclass
class FlightLeg:
    origin: str
    destination: str
    departure_time: str = ""
    arrival_time: str = ""
    airline: str = ""
    operating_airline: str = ""
    marketing_airline: str = ""
    flight_number: str = ""
    duration_minutes: int = 0


@dataclass
class FareOption:
    construction: str  # round_trip | multi_city | one_way_out | one_way_return | two_one_ways
    ticketing: str  # one_ticket | two_tickets
    price: float
    currency: str
    airlines: list[str] = field(default_factory=list)
    stops: int = 0
    duration_minutes: int = 0
    legs: list[FlightLeg] = field(default_factory=list)
    source: str = ""
    source_status: SourceStatus = SourceStatus.OK
    usable: bool = True
    reject_reason: str = ""
    carry_on_included: bool | None = None
    checked_bag_included: bool | None = None
    bag_fees: float = 0.0
    all_in_price: float | None = None
    stops_by_leg: list[int] = field(default_factory=list)
    layovers_by_leg: list[list[int]] = field(default_factory=list)
    leg_departure_times: list[str] = field(default_factory=list)
    leg_arrival_times: list[str] = field(default_factory=list)
    airport_changes: list[str] = field(default_factory=list)
    self_transfer: bool = False
    match_type: MatchType = MatchType.STRICT
    constraint_violations: list[str] = field(default_factory=list)
    near_match_violations: list[str] = field(default_factory=list)
    manual_omissions: list[str] = field(default_factory=list)
    alliance: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.all_in_price is None and self.bag_fees:
            self.all_in_price = self.price + self.bag_fees
        if not self.airport_changes:
            self.airport_changes = [
                f"{previous.destination}->{following.origin}"
                for previous, following in zip(self.legs, self.legs[1:])
                if previous.destination and following.origin and previous.destination != following.origin
            ]

    @property
    def total_price(self) -> float:
        """Return the quoted fare plus any known baggage fees."""
        return self.all_in_price if self.all_in_price is not None else self.price + self.bag_fees

    @property
    def leg_count(self) -> int:
        return max(len(self.stops_by_leg), len(self.layovers_by_leg), 1)

    @property
    def operating_airlines(self) -> list[str]:
        return list(
            dict.fromkeys(
                leg.operating_airline or leg.airline
                for leg in self.legs
                if leg.operating_airline or leg.airline
            )
        )

    @property
    def alliance_label(self) -> str | None:
        if self.alliance:
            return self.alliance
        codes = set(self.airlines)
        for name, members in ALLIANCES.items():
            if codes and codes <= members:
                return name.replace("_", " ").title()
        if len(codes) == 1:
            return "Single carrier"
        if len(codes) > 1:
            return "Multi-carrier"
        return None


@dataclass
class CompareResult:
    quote_status: QuoteStatus
    trip_shape: str
    ticketing: str
    winner: FareOption | None
    runner_up: FareOption | None
    one_ticket: FareOption | None
    two_one_ways: FareOption | None
    alliance_alternative: FareOption | None
    all_options: list[FareOption] = field(default_factory=list)
    deep_links: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    date_combo: dict[str, str] = field(default_factory=dict)
    strict_winner: FareOption | None = None
    near_matches: list[FareOption] = field(default_factory=list)
    omission_audit: list[str] = field(default_factory=list)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def stops_param(rule: str) -> int | None:
    """SerpAPI stops: 0 any, 1 nonstop, 2 max 1 stop, 3 max 2 stops."""
    mapping = {
        "any": 0,
        "nonstop": 1,
        "nonstop_preferred": 1,
        "max_1": 2,
        "max_2": 3,
    }
    return mapping.get(rule)


def parse_duration(text: str | int | float | None) -> int:
    """Parse minutes as int, or strings like '14 hr 35 min' / '10h 31m'."""
    if text is None:
        return 0
    if isinstance(text, (int, float)):
        return int(text)
    if not isinstance(text, str) or not text.strip():
        return 0
    hours = 0
    minutes = 0
    parts = text.lower().replace("hr", "h").replace("min", "m").split()
    i = 0
    while i < len(parts):
        token = parts[i].strip("hm")
        if token.isdigit():
            val = int(token)
            if i + 1 < len(parts) and parts[i + 1].startswith("h"):
                hours = val
                i += 2
            elif "h" in parts[i]:
                hours = val
                i += 1
            elif i > 0 and "h" in parts[i - 1]:
                minutes = val
                i += 1
            else:
                # bare number — guess from context
                if hours == 0 and val < 24:
                    hours = val
                else:
                    minutes = val
                i += 1
        else:
            i += 1
    if hours == 0 and minutes == 0:
        import re

        m = re.search(r"(\d+)\s*h", text, re.I)
        if m:
            hours = int(m.group(1))
        m = re.search(r"(\d+)\s*m", text, re.I)
        if m:
            minutes = int(m.group(1))
    return hours * 60 + minutes


def kayak_rt_link(origin: str, dest: str, out_date: str, ret_date: str) -> str:
    return f"https://www.kayak.com/flights/{origin}-{dest}/{out_date}/{dest}-{origin}/{ret_date}"


def kayak_multi_link(legs: list[tuple[str, str, str]]) -> str:
    """legs: [(origin, dest, date), ...]"""
    parts = []
    for origin, dest, date in legs:
        parts.append(f"{origin}-{dest}/{date}")
    return "https://www.kayak.com/flights/" + "/".join(parts)


def google_rt_link(origin: str, dest: str, out_date: str, ret_date: str, currency: str) -> str:
    q = (
        f"Round trip flights from {origin} to {dest} "
        f"on {out_date} through {ret_date}"
    )
    from urllib.parse import quote

    return (
        f"https://www.google.com/travel/flights?hl=en&curr={currency}&q={quote(q)}"
    )

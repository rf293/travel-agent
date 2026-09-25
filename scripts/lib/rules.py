"""Constraint parsing, usability checks, and near-match ranking."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from scripts.lib.models import FareOption, MatchType

CARRIER_ALIASES = {
    "FLAIR AIRLINES": "F8",
    "FLAIR": "F8",
    "WESTJET": "WS",
    "AIR CANADA": "AC",
}


@dataclass(frozen=True)
class TimeWindow:
    """A local departure or arrival window.

    `earliest` / `latest` are minutes after midnight on the travel day.
    `earliest_at` / `latest_at` are local wall-clock datetimes, for bounds that
    cross midnight (e.g. "arrive by 01:00 the next day").
    """

    earliest: int | None = None
    latest: int | None = None
    near_match_minutes: int = 30
    field: str = "departure"
    policy: str = "strict"
    earliest_at: datetime | None = None
    latest_at: datetime | None = None

    @classmethod
    def from_config(cls, config: dict | None, default_near_match_minutes: int = 30) -> "TimeWindow":
        config = config or {}
        return cls(
            earliest=parse_clock(config.get("earliest")),
            latest=parse_clock(config.get("latest")),
            near_match_minutes=int(config.get("near_match_minutes", default_near_match_minutes)),
            field=str(config.get("field", "departure")),
            policy=str(config.get("policy", "strict")).lower(),
            earliest_at=local_datetime(config.get("earliest_at")),
            latest_at=local_datetime(config.get("latest_at")),
        )

    @property
    def uses_datetime(self) -> bool:
        return self.earliest_at is not None or self.latest_at is not None


def parse_clock(value: str | int | float | None) -> int | None:
    """Parse 24-hour or AM/PM clock text; accept 24:00 as end-of-day."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        minutes = int(value)
        if 0 <= minutes <= 1440:
            return minutes
        raise ValueError(f"clock minutes out of range: {value}")

    text = str(value).strip().upper()
    if text == "24:00":
        return 1440
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?:\s*([AP]M))?", text)
    if not match:
        raise ValueError(f"invalid clock value: {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    meridiem = match.group(3)
    if minute > 59:
        raise ValueError(f"invalid clock value: {value!r}")
    if meridiem:
        if hour < 1 or hour > 12:
            raise ValueError(f"invalid 12-hour clock value: {value!r}")
        hour = hour % 12 + (12 if meridiem == "PM" else 0)
    elif hour > 24 or (hour == 24 and minute != 0):
        raise ValueError(f"invalid 24-hour clock value: {value!r}")
    return hour * 60 + minute


def local_minutes(timestamp: str) -> int | None:
    """Extract local minutes from ISO timestamps or provider display strings."""
    if not timestamp:
        return None
    text = str(timestamp).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.hour * 60 + parsed.minute
    except ValueError:
        pass

    match = re.search(r"\b(\d{1,2}):(\d{2})\s*([AP]M)?\b", text.upper())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    meridiem = match.group(3)
    if meridiem:
        hour = hour % 12 + (12 if meridiem == "PM" else 0)
    return hour * 60 + minute


def local_datetime(value: str | datetime | None) -> datetime | None:
    """Parse an ISO timestamp as local wall-clock time (any UTC offset is dropped)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _distance_from_window(value: int, window: TimeWindow) -> int:
    if window.earliest is not None and value < window.earliest:
        return window.earliest - value
    if window.latest is not None and value > window.latest:
        return value - window.latest
    return 0


def _distance_from_datetime_window(value: datetime, window: TimeWindow) -> int:
    if window.earliest_at is not None and value < window.earliest_at:
        return math.ceil((window.earliest_at - value).total_seconds() / 60)
    if window.latest_at is not None and value > window.latest_at:
        return math.ceil((value - window.latest_at).total_seconds() / 60)
    return 0


def _timestamps_for_option(option: FareOption, field: str) -> list[str]:
    if field == "departure" and option.leg_departure_times:
        return list(option.leg_departure_times)
    if field == "arrival" and option.leg_arrival_times:
        return list(option.leg_arrival_times)
    return [str(getattr(leg, f"{field}_time", "") or "") for leg in option.legs]


def _window_distance(timestamp: str | None, window: TimeWindow) -> int | None:
    """Minutes outside the window, 0 when inside, None when the time is unknown."""
    if not timestamp:
        return None
    distance = 0
    if window.earliest is not None or window.latest is not None:
        minutes = local_minutes(timestamp)
        if minutes is None:
            return None
        distance = _distance_from_window(minutes, window)
    if window.uses_datetime:
        moment = local_datetime(timestamp)
        if moment is None:
            return None
        distance = max(distance, _distance_from_datetime_window(moment, window))
    return distance


def assess_option(
    option: FareOption,
    time_windows: list[TimeWindow | None] | None = None,
    max_stops: int | None = None,
    max_layover_minutes: int | None = None,
) -> FareOption:
    """Classify one option without silently discarding a nearby fare."""
    strict: list[str] = []
    near: list[str] = []

    if option.self_transfer:
        strict.append("self-transfer is not allowed")
    if option.airport_changes:
        strict.append(f"airport change: {', '.join(option.airport_changes)}")

    windows = time_windows or []
    for index, window in enumerate(windows):
        if window is None:
            continue
        if window.policy not in {"strict", "soft"}:
            strict.append(f"leg {index + 1} has invalid time-window policy {window.policy!r}")
            continue
        timestamps = _timestamps_for_option(option, window.field)
        distance = _window_distance(
            timestamps[index] if index < len(timestamps) else None, window
        )
        if distance is None:
            strict.append(f"leg {index + 1} has no {window.field} time")
            continue
        if distance == 0:
            continue
        detail = f"leg {index + 1} {window.field} is {distance} min outside window"
        if window.policy == "soft":
            detail = f"{detail} (soft policy)"
        if distance <= window.near_match_minutes:
            near.append(detail)
        else:
            strict.append(detail)

    stops = option.stops_by_leg or [option.stops]
    if max_stops is not None:
        for index, count in enumerate(stops):
            if count > max_stops:
                strict.append(f"leg {index + 1} has {count} stop(s); max is {max_stops}")

    if max_layover_minutes is not None:
        for leg_index, layovers in enumerate(option.layovers_by_leg):
            for minutes in layovers:
                if minutes >= max_layover_minutes:
                    strict.append(
                        f"leg {leg_index + 1} has a {minutes // 60}h {minutes % 60:02d}m layover"
                    )

    option.constraint_violations = strict
    option.near_match_violations = near
    if strict:
        option.usable = False
        option.match_type = MatchType.REJECTED
        option.reject_reason = "; ".join(strict)
    elif near:
        option.usable = True
        option.match_type = MatchType.NEAR_MATCH
        option.reject_reason = ""
    else:
        option.usable = True
        option.match_type = MatchType.STRICT
        option.reject_reason = ""
    return option


def apply_manual_omissions(
    options: Iterable[FareOption], omitted_carriers: Iterable[str] | None
) -> list[FareOption]:
    """Mark user-requested carrier omissions without making them provider filters."""
    omitted = {carrier.strip().upper() for carrier in (omitted_carriers or [])}
    omitted_codes = {CARRIER_ALIASES.get(carrier, carrier) for carrier in omitted}
    for option in options:
        matches = sorted(
            {
                carrier
                for carrier in option.airlines
                if carrier.upper() in omitted or carrier.upper() in omitted_codes
                or CARRIER_ALIASES.get(carrier.upper()) in omitted
            }
        )
        if matches:
            option.manual_omissions = matches
            option.usable = False
            option.match_type = MatchType.REJECTED
            option.reject_reason = f"manual carrier omission: {', '.join(matches)}"
    return list(options)


def apply_layover_preference(
    options: Iterable[FareOption],
    max_layover_minutes: int = 360,
    similar_price_threshold: float = 50.0,
) -> list[FareOption]:
    """Reject a long connection only when a reasonably priced shorter one exists."""
    candidates = list(options)
    short = [
        option
        for option in candidates
        if option.usable
        and all(
            minutes < max_layover_minutes
            for layovers in option.layovers_by_leg
            for minutes in layovers
        )
    ]
    if not short:
        return candidates
    cheapest_short = min(short, key=lambda option: option.total_price)
    for option in candidates:
        has_long = any(
            minutes >= max_layover_minutes
            for layovers in option.layovers_by_leg
            for minutes in layovers
        )
        if has_long and option.total_price >= cheapest_short.total_price - similar_price_threshold:
            option.usable = False
            option.match_type = MatchType.REJECTED
            option.constraint_violations.append(
                f"layover is at least {max_layover_minutes // 60}h while a shorter "
                f"option is within {similar_price_threshold:g}"
            )
            option.reject_reason = "; ".join(option.constraint_violations)
    return candidates


def rank_options(
    options: Iterable[FareOption],
    near_match_savings_threshold: float = 25.0,
) -> tuple[FareOption | None, FareOption | None, list[FareOption]]:
    """Return strict winner, near-match recommendation, and rejected options."""
    priced = [option for option in options if option.total_price > 0]
    strict = sorted(
        (option for option in priced if option.usable and option.match_type == MatchType.STRICT),
        key=lambda option: option.total_price,
    )
    near = sorted(
        (option for option in priced if option.usable and option.match_type == MatchType.NEAR_MATCH),
        key=lambda option: option.total_price,
    )
    rejected = [option for option in priced if not option.usable]
    strict_winner = strict[0] if strict else None
    near_winner = None
    if near:
        cheapest_near = near[0]
        if strict_winner is None or (
            strict_winner.total_price - cheapest_near.total_price >= near_match_savings_threshold
        ):
            near_winner = cheapest_near
    return strict_winner, near_winner, rejected


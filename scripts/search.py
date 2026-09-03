#!/usr/bin/env python3
"""Travel Agent search — SerpAPI → Duffel → Amadeus waterfall."""

from __future__ import annotations

import argparse
import itertools
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.env import load_repo_env  # noqa: E402
from scripts.lib.models import (  # noqa: E402
    ALLIANCES,
    CompareResult,
    FareOption,
    MatchType,
    QuoteStatus,
    SourceStatus,
    google_rt_link,
    kayak_multi_link,
    kayak_rt_link,
    utc_now_iso,
)
from scripts.lib.rules import (  # noqa: E402
    TimeWindow,
    apply_manual_omissions,
    apply_layover_preference,
    assess_option,
    rank_options,
)
from scripts.providers.amadeus_provider import AmadeusProvider  # noqa: E402
from scripts.providers.duffel_provider import DuffelProvider  # noqa: E402
from scripts.providers.serpapi_provider import SerpApiProvider  # noqa: E402

load_repo_env()

GL_MAP = {"CAD": "ca", "USD": "us", "EUR": "de"}


@dataclass
class CandidateSet:
    strict: list[FareOption]
    near_matches: list[FareOption]
    rejected: list[FareOption]
    notes: list[str]

    @property
    def winner(self) -> FareOption | None:
        return self.strict[0] if self.strict else (self.near_matches[0] if self.near_matches else None)

    def recommended(self, savings_threshold: float = 25.0) -> FareOption | None:
        if not self.near_matches:
            return self.strict[0] if self.strict else None
        if not self.strict:
            return self.near_matches[0]
        if self.strict[0].total_price - self.near_matches[0].total_price >= savings_threshold:
            return self.near_matches[0]
        return self.strict[0]


def load_watch(name: str) -> dict:
    path = ROOT / "watches" / f"{name}.yml"
    if not path.exists():
        raise FileNotFoundError(f"Watch not found: {path}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict):
        raise ValueError(f"Search config must be a YAML mapping: {path}")
    return config


def _time_window(watch: dict, leg_name: str) -> TimeWindow | None:
    config = (watch.get("time_windows") or {}).get(leg_name)
    if config is None:
        return None
    return TimeWindow.from_config(
        config, default_near_match_minutes=int(watch.get("near_match_minutes", 30))
    )


def _time_windows(watch: dict, leg_names: tuple[str, ...] = ("outbound", "return")) -> list[TimeWindow | None]:
    return [_time_window(watch, name) for name in leg_names]


def _manual_omissions(watch: dict) -> list[str]:
    """Carrier omissions are intentionally presentation-time, not API filters."""
    return [str(value) for value in watch.get("manual_omissions", [])]


def _assess_candidates(
    options: list[FareOption],
    time_windows: list[TimeWindow | None] | None,
    max_stops: int | None,
    watch: dict,
) -> CandidateSet:
    max_layover = watch.get("max_layover_minutes")
    assessed = [
        assess_option(
            option,
            time_windows=time_windows,
            max_stops=max_stops,
            max_layover_minutes=int(max_layover) if max_layover is not None else None,
        )
        for option in options
    ]
    apply_manual_omissions(assessed, _manual_omissions(watch))
    apply_layover_preference(
        assessed,
        max_layover_minutes=int(watch.get("layover_preference_minutes", 360)),
        similar_price_threshold=float(watch.get("layover_similarity_threshold", 50)),
    )
    _strict_winner, _near_winner, _rejected = rank_options(
        assessed,
        near_match_savings_threshold=float(watch.get("near_match_savings_threshold", 25)),
    )
    strict_options = sorted(
        [option for option in assessed if option.usable and option.match_type == MatchType.STRICT],
        key=lambda option: option.total_price,
    )
    near_options = sorted(
        [option for option in assessed if option.usable and option.match_type == MatchType.NEAR_MATCH],
        key=lambda option: option.total_price,
    )
    notes: list[str] = []
    for near_option in near_options:
        notes.append(
            f"near match candidate: {near_option.total_price:.0f} "
            f"({'; '.join(near_option.near_match_violations)})"
        )
    for rejected_option in _rejected:
        if rejected_option.manual_omissions:
            notes.append(
                f"manual carrier omission: {', '.join(rejected_option.manual_omissions)} "
                f"({rejected_option.total_price:.0f})"
            )
    return CandidateSet(strict_options, near_options, _rejected, notes)


class SearchWaterfall:
    def __init__(self):
        self.serp = SerpApiProvider()
        self.duffel = DuffelProvider()
        self.amadeus = AmadeusProvider()
        self.log: list[str] = []

    @property
    def has_provider(self) -> bool:
        return self.serp.available or self.duffel.available or self.amadeus.available

    def _note(self, msg: str) -> None:
        self.log.append(msg)

    def rt_options(
        self,
        origin: str,
        dest: str,
        out: str,
        ret: str,
        currency: str,
        stops_rule: str,
        adults: int = 1,
    ) -> tuple[list[FareOption], list[str]]:
        gl = GL_MAP.get(currency, "us")
        notes: list[str] = []
        if self.serp.available:
            options, status, err = self.serp.search_round_trip_options(
                origin, dest, out, ret, currency, adults, stops_rule, gl
            )
            notes.append(f"serpapi RT {origin}-{dest}: {status.value}" + (f" ({err})" if err else ""))
            if options:
                return options, notes
            if status == SourceStatus.BLOCKED:
                self._note(f"SerpAPI blocked for RT {origin}-{dest}")
        if self.duffel.available:
            options, status, err = self.duffel.search_itinerary_options(
                [(origin, dest, out), (dest, origin, ret)],
                currency=currency,
                adults=adults,
                max_stops=_max_stops(stops_rule),
                construction="round_trip",
            )
            notes.append(f"duffel RT {origin}-{dest}: {status.value}" + (f" ({err})" if err else ""))
            if options:
                return options, notes
        if self.amadeus.available:
            options, status, err = self.amadeus.search_itinerary_options(
                [(origin, dest, out), (dest, origin, ret)],
                currency=currency,
                adults=adults,
                max_stops=_max_stops(stops_rule),
                construction="round_trip",
            )
            notes.append(f"amadeus RT {origin}-{dest}: {status.value}" + (f" ({err})" if err else ""))
            if options:
                return options, notes
        return [], notes

    def rt(
        self,
        origin: str,
        dest: str,
        out: str,
        ret: str,
        currency: str,
        stops_rule: str,
        adults: int = 1,
    ) -> tuple[FareOption | None, list[str]]:
        options, notes = self.rt_options(origin, dest, out, ret, currency, stops_rule, adults)
        return min(options, key=lambda option: option.total_price, default=None), notes

    def one_way_options(
        self,
        origin: str,
        dest: str,
        date: str,
        currency: str,
        stops_rule: str,
        label: str,
        adults: int = 1,
    ) -> tuple[list[FareOption], list[str]]:
        gl = GL_MAP.get(currency, "us")
        notes: list[str] = []
        if self.serp.available:
            options, status, err = self.serp.search_one_way_options(
                origin, dest, date, currency, adults, stops_rule, gl, label
            )
            notes.append(f"serpapi OW {label}: {status.value}" + (f" ({err})" if err else ""))
            if options:
                return options, notes
        if self.duffel.available:
            options, status, err = self.duffel.search_itinerary_options(
                [(origin, dest, date)],
                currency=currency,
                adults=adults,
                max_stops=_max_stops(stops_rule),
                construction=label,
            )
            notes.append(f"duffel OW {label}: {status.value}" + (f" ({err})" if err else ""))
            for option in options:
                option.construction = label
                option.ticketing = "two_tickets"
            if options:
                return options, notes
        if self.amadeus.available:
            options, status, err = self.amadeus.search_itinerary_options(
                [(origin, dest, date)],
                currency=currency,
                adults=adults,
                max_stops=_max_stops(stops_rule),
                construction=label,
            )
            notes.append(f"amadeus OW {label}: {status.value}" + (f" ({err})" if err else ""))
            for option in options:
                option.construction = label
                option.ticketing = "two_tickets"
            if options:
                return options, notes
        return [], notes

    def ow_pair_options(
        self,
        origin: str,
        dest: str,
        out: str,
        ret: str,
        currency: str,
        stops_rule: str,
        adults: int = 1,
    ) -> tuple[list[FareOption], list[str]]:
        notes: list[str] = []

        outbound, outbound_notes = self.one_way_options(
            origin, dest, out, currency, stops_rule, "one_way_out", adults
        )
        returning, return_notes = self.one_way_options(
            dest, origin, ret, currency, stops_rule, "one_way_return", adults
        )
        notes.extend(outbound_notes + return_notes)
        combined: list[FareOption] = []
        for out_option, return_option in itertools.product(outbound, returning):
            combined.append(_combine_one_ways(out_option, return_option, currency))
        return sorted(combined, key=lambda option: option.total_price), notes

    def ow_pair(
        self,
        origin: str,
        dest: str,
        out: str,
        ret: str,
        currency: str,
        stops_rule: str,
        adults: int = 1,
    ) -> tuple[FareOption | None, list[str]]:
        options, notes = self.ow_pair_options(
            origin, dest, out, ret, currency, stops_rule, adults
        )
        return min(options, key=lambda option: option.total_price, default=None), notes

    def multi_city_options(
        self,
        legs: list[tuple[str, str, str]],
        currency: str,
        stops_rule: str,
        adults: int = 1,
    ) -> tuple[list[FareOption], list[str]]:
        gl = GL_MAP.get(currency, "us")
        notes: list[str] = []
        if self.serp.available:
            options, status, err = self.serp.search_multi_city_options(
                legs, currency, adults, stops_rule, gl
            )
            notes.append(f"serpapi multi-city: {status.value}" + (f" ({err})" if err else ""))
            if options:
                return options, notes
            if status == SourceStatus.BLOCKED:
                self._note("SerpAPI blocked for multi-city")
        if self.duffel.available:
            options, status, err = self.duffel.search_itinerary_options(
                legs,
                currency=currency,
                adults=adults,
                max_stops=_max_stops(stops_rule),
                construction="multi_city",
            )
            notes.append(f"duffel multi-city: {status.value}" + (f" ({err})" if err else ""))
            if options:
                return options, notes
        if self.amadeus.available:
            options, status, err = self.amadeus.search_itinerary_options(
                legs,
                currency=currency,
                adults=adults,
                max_stops=_max_stops(stops_rule),
                construction="multi_city",
            )
            notes.append(f"amadeus multi-city: {status.value}" + (f" ({err})" if err else ""))
            if options:
                return options, notes
        return [], notes

    def multi_city(
        self,
        legs: list[tuple[str, str, str]],
        currency: str,
        stops_rule: str,
        adults: int = 1,
    ) -> tuple[FareOption | None, list[str]]:
        options, notes = self.multi_city_options(legs, currency, stops_rule, adults)
        return min(options, key=lambda option: option.total_price, default=None), notes


def _max_stops(rule: str) -> int | None:
    if rule in ("nonstop", "nonstop_preferred"):
        return 0
    if rule == "max_1":
        return 1
    if rule == "max_2":
        return 2
    return None


def _combine_bag_status(first: bool | None, second: bool | None) -> bool | None:
    if first is False or second is False:
        return False
    if first is True and second is True:
        return True
    return None


def _combine_one_ways(
    outbound: FareOption, returning: FareOption, currency: str
) -> FareOption:
    return FareOption(
        construction="two_one_ways",
        ticketing="two_tickets",
        price=outbound.price + returning.price,
        currency=currency,
        airlines=list(dict.fromkeys(outbound.airlines + returning.airlines)),
        stops=outbound.stops + returning.stops,
        duration_minutes=outbound.duration_minutes + returning.duration_minutes,
        legs=outbound.legs + returning.legs,
        stops_by_leg=outbound.stops_by_leg + returning.stops_by_leg,
        layovers_by_leg=outbound.layovers_by_leg + returning.layovers_by_leg,
        leg_departure_times=outbound.leg_departure_times + returning.leg_departure_times,
        leg_arrival_times=outbound.leg_arrival_times + returning.leg_arrival_times,
        carry_on_included=_combine_bag_status(
            outbound.carry_on_included, returning.carry_on_included
        ),
        checked_bag_included=_combine_bag_status(
            outbound.checked_bag_included, returning.checked_bag_included
        ),
        bag_fees=outbound.bag_fees + returning.bag_fees,
        source=f"{outbound.source}+{returning.source}",
        raw={"outbound": outbound.raw, "return": returning.raw},
    )


def _baggage_note(option: FareOption) -> str:
    carry = "included" if option.carry_on_included is True else (
        "not included" if option.carry_on_included is False else "unknown"
    )
    checked = "included" if option.checked_bag_included is True else (
        "not included" if option.checked_bag_included is False else "unknown"
    )
    fee = f"; known bag fees {option.currency} {option.bag_fees:.0f}" if option.bag_fees else ""
    return f"carry-on {carry}, checked bag {checked}{fee}"


def _itinerary_lines(option: FareOption) -> list[str]:
    departures = option.leg_departure_times
    arrivals = option.leg_arrival_times
    if not departures:
        departures = [leg.departure_time for leg in option.legs[: option.leg_count]]
    if not arrivals:
        arrivals = [leg.arrival_time for leg in option.legs[: option.leg_count]]
    stops = option.stops_by_leg or [option.stops]
    lines = []
    for index, (departure, arrival) in enumerate(zip(departures, arrivals), start=1):
        lines.append(
            f"Leg {index}: {departure or '?'} → {arrival or '?'} "
            f"({stops[index - 1] if index <= len(stops) else '?'} stop(s))"
        )
    return lines


def _apply_usability(opt: FareOption, max_stops: int | None) -> FareOption:
    stop_counts = opt.stops_by_leg or [opt.stops]
    if max_stops is not None and any(count > max_stops for count in stop_counts):
        opt.usable = False
        opt.match_type = MatchType.REJECTED
        opt.reject_reason = (
            f"stops per leg {stop_counts} exceeds max {max_stops}"
        )
    return opt


def _pick_leg_option(
    options: list[FareOption],
    preference: str = "cheapest",
) -> FareOption | None:
    """Pick one leg from assessed strict + near-match options."""
    usable = [option for option in options if option.usable]
    if not usable:
        return None
    strict = [option for option in usable if option.match_type == MatchType.STRICT]
    pool = strict or usable
    if preference == "latest":
        return max(
            pool,
            key=lambda option: option.leg_departure_times[0]
            if option.leg_departure_times
            else "",
        )
    return min(pool, key=lambda option: option.total_price)


def _pick_schedule_ows(
    wf: SearchWaterfall,
    origin: str,
    dest: str,
    out: str,
    ret: str,
    currency: str,
    stops_rule: str,
    time_windows: list[TimeWindow | None] | None,
    watch: dict,
    return_preference: str = "latest",
) -> tuple[FareOption | None, list[str]]:
    """Build two one-ways with per-leg time windows and schedule preferences."""
    notes: list[str] = []
    out_window = [time_windows[0]] if time_windows else [None]
    ret_window = [time_windows[1]] if time_windows and len(time_windows) > 1 else [None]

    outbound_raw, out_notes = wf.one_way_options(
        origin, dest, out, currency, stops_rule, "one_way_out"
    )
    returning_raw, ret_notes = wf.one_way_options(
        dest, origin, ret, currency, stops_rule, "one_way_return"
    )
    notes.extend(out_notes + ret_notes)

    outbound_rules = _assess_candidates(
        outbound_raw, out_window, _max_stops(stops_rule), watch
    )
    returning_rules = _assess_candidates(
        returning_raw, ret_window, _max_stops(stops_rule), watch
    )
    notes.extend(outbound_rules.notes + returning_rules.notes)

    out_option = _pick_leg_option(
        outbound_rules.strict + outbound_rules.near_matches, "cheapest"
    )
    ret_option = _pick_leg_option(
        returning_rules.strict + returning_rules.near_matches, return_preference
    )
    if not out_option or not ret_option:
        return None, notes

    combined = _combine_one_ways(out_option, ret_option, currency)
    combined.match_type = (
        MatchType.STRICT
        if out_option.match_type == MatchType.STRICT
        and ret_option.match_type == MatchType.STRICT
        else MatchType.NEAR_MATCH
    )
    combined.near_match_violations = (
        out_option.near_match_violations + ret_option.near_match_violations
    )
    notes.append(
        f"schedule two OWs: out {out_option.leg_departure_times[0]} "
        f"+ ret {ret_option.leg_departure_times[0]} ({return_preference} return)"
    )
    return combined, notes


def _pick_rt_vs_ows(
    wf: SearchWaterfall,
    origin: str,
    dest: str,
    out: str,
    ret: str,
    currency: str,
    stops_rule: str,
    stop_fallback: str | None = None,
    nonstop_premium_threshold: float = 50.0,
    time_windows: list[TimeWindow | None] | None = None,
    watch: dict | None = None,
    return_preference: str | None = None,
) -> tuple[FareOption | None, FareOption | None, FareOption | None, list[str]]:
    """Return (winner, rt_option, ow_option, notes)."""
    all_notes: list[str] = []
    watch = watch or {}
    rt_raw: list[FareOption] = []

    if stops_rule == "nonstop_preferred":
        rt_ns, n = wf.rt_options(origin, dest, out, ret, currency, "nonstop")
        all_notes.extend(n)
        rt_raw.extend(rt_ns)
        rt_1, n = wf.rt_options(
            origin, dest, out, ret, currency, stop_fallback or "max_1"
        )
        all_notes.extend(n)
        rt_raw.extend(rt_1)
    else:
        rt_raw, n = wf.rt_options(origin, dest, out, ret, currency, stops_rule)
        all_notes.extend(n)

    rt_candidates = _assess_candidates(
        rt_raw,
        time_windows,
        _max_stops(stop_fallback or stops_rule),
        watch,
    )
    rt_options = rt_candidates.strict + rt_candidates.near_matches
    rt_best = min(rt_options, key=lambda x: x.total_price, default=None)
    if stops_rule == "nonstop_preferred" and rt_best:
        nonstop = [
            option for option in rt_options
            if all(count == 0 for count in (option.stops_by_leg or [option.stops]))
        ]
        if nonstop:
            best_nonstop = min(nonstop, key=lambda option: option.total_price)
            rt_options = [
                option for option in rt_options
                if option is best_nonstop
                or option.total_price <= best_nonstop.total_price - nonstop_premium_threshold
            ]
            rt_best = min(rt_options, key=lambda x: x.total_price, default=None)

    ow_rule = stops_rule if stops_rule != "nonstop_preferred" else (stop_fallback or "max_1")
    if return_preference == "latest":
        ows, n = _pick_schedule_ows(
            wf,
            origin,
            dest,
            out,
            ret,
            currency,
            ow_rule,
            time_windows,
            watch,
            return_preference,
        )
        all_notes.extend(n)
        ow_candidates = CandidateSet([], [], [], [])
    else:
        ow_raw, n = wf.ow_pair_options(origin, dest, out, ret, currency, ow_rule)
        all_notes.extend(n)
        ow_candidates = _assess_candidates(
            ow_raw,
            time_windows,
            _max_stops(ow_rule),
            watch,
        )
        ow_options = ow_candidates.strict + ow_candidates.near_matches
        ows = min(ow_options, key=lambda x: x.total_price, default=None)

    candidates = [c for c in (rt_best, ows) if c and c.usable]
    if not candidates:
        return None, rt_best, ows, all_notes

    if return_preference == "latest" and ows and ows.usable:
        winner = ows
        all_notes.extend(rt_candidates.notes + ow_candidates.notes)
        return winner, rt_best, ows, all_notes

    strict_candidates = [c for c in candidates if c.match_type == MatchType.STRICT]
    near_candidates = [c for c in candidates if c.match_type == MatchType.NEAR_MATCH]
    if strict_candidates:
        strict_winner = min(strict_candidates, key=lambda x: x.total_price)
        near_winner = min(near_candidates, key=lambda x: x.total_price, default=None)
        if (
            near_winner
            and strict_winner.total_price - near_winner.total_price
            >= float(watch.get("near_match_savings_threshold", 25))
        ):
            winner = near_winner
        else:
            winner = strict_winner
    else:
        winner = min(candidates, key=lambda x: x.total_price)
    all_notes.extend(rt_candidates.notes + ow_candidates.notes)
    return winner, rt_best, ows, all_notes


def _alliance_match(opt: FareOption, alliance_key: str) -> bool:
    members = ALLIANCES.get(alliance_key, set())
    return bool(opt.airlines) and set(opt.airlines) <= members


def _find_alliance_alternative(
    options: list[FareOption], winner: FareOption, alliance_key: str | None
) -> FareOption | None:
    if not alliance_key:
        return None
    usable = [o for o in options if o.usable and o.total_price > 0]
    alliance_opts = [o for o in usable if _alliance_match(o, alliance_key)]
    if not alliance_opts:
        return None
    best = min(alliance_opts, key=lambda x: x.total_price)
    if best is winner:
        return None
    if winner.alliance_label and winner.alliance_label.replace(" ", "_").upper() == alliance_key:
        return None
    return best


def run_round_trip(watch: dict) -> str:
    """Run the same contract for an ad-hoc round-trip YAML config."""
    wf = SearchWaterfall()
    dates = watch["dates"]
    origin, dest = watch["origin"], watch["destination"]
    currency = watch.get("currency", "CAD")
    stops = watch.get("stops", "any")
    windows = _time_windows(watch)
    sections = [
        f"## Round-trip search · {utc_now_iso().split()[0]} · {currency}",
        f"Route: {origin} ⇄ {dest} | Dates: {dates['outbound']} / {dates['return']}",
        f"Time windows: {windows[0] or 'none'} / {windows[1] or 'none'}",
        "Manual carrier omissions: "
        + (", ".join(_manual_omissions(watch)) if _manual_omissions(watch) else "none"),
        "",
    ]
    if not wf.has_provider:
        sections.append("Quote status: INCOMPLETE")
        sections.append("ERROR: Set API keys in .env")
        sections.append(f"Book: {kayak_rt_link(origin, dest, dates['outbound'], dates['return'])}")
        return "\n".join(sections)

    rt_raw, rt_notes = wf.rt_options(
        origin, dest, dates["outbound"], dates["return"], currency, stops
    )
    ow_raw, ow_notes = wf.ow_pair_options(
        origin, dest, dates["outbound"], dates["return"], currency, stops
    )
    rt_rules = _assess_candidates(rt_raw, windows, _max_stops(stops), watch)
    ow_rules = _assess_candidates(ow_raw, windows, _max_stops(stops), watch)
    candidates = rt_rules.strict + rt_rules.near_matches + ow_rules.strict + ow_rules.near_matches
    strict = [option for option in candidates if option.match_type == MatchType.STRICT]
    near = [option for option in candidates if option.match_type == MatchType.NEAR_MATCH]
    strict_winner = min(strict, key=lambda option: option.total_price, default=None)
    near_winner = min(near, key=lambda option: option.total_price, default=None)
    winner = strict_winner
    if near_winner and (
        winner is None
        or winner.total_price - near_winner.total_price
        >= float(watch.get("near_match_savings_threshold", 25))
    ):
        winner = near_winner
    if winner is None:
        sections.append("Quote status: INCOMPLETE")
        sections.extend(f"- {note}" for note in rt_notes + ow_notes)
        return "\n".join(sections)

    sections.append("Quote status: COMPLETE")
    sections.append(
        f"Strict winner: {strict_winner.total_price:.0f} {currency} "
        f"({strict_winner.construction})" if strict_winner else "Strict winner: none"
    )
    if near_winner:
        sections.append(
            f"Near match: {near_winner.total_price:.0f} {currency} "
            f"({'; '.join(near_winner.near_match_violations)})"
        )
    sections.append(
        f"Recommendation: {winner.total_price:.0f} {currency} "
        f"({winner.construction}, {', '.join(winner.airlines)}; "
        f"{winner.match_type.value}; {_baggage_note(winner)})"
    )
    sections.extend(f"- {line}" for line in _itinerary_lines(winner))
    sections.append(f"RT: {min((o.total_price for o in rt_rules.strict), default='NOT PRICED')} {currency}")
    sections.append(f"Two one-ways: {min((o.total_price for o in ow_rules.strict), default='NOT PRICED')} {currency}")
    sections.append(f"Book: {google_rt_link(origin, dest, dates['outbound'], dates['return'], currency)}")
    sections.append("Audit:")
    sections.extend(f"- {note}" for note in rt_notes + ow_notes + rt_rules.notes + ow_rules.notes)
    return "\n".join(sections)


def run_hawaii(watch: dict) -> str:
    wf = SearchWaterfall()
    dates = watch["dates"]
    out, ret = dates["outbound"], dates["return"]
    fx = watch.get("fx", {}).get("cad_per_usd", 1.39)
    benchmark = watch.get("benchmark", {})
    benchmark_usd = benchmark.get("combined_usd", 0)
    travelers = {t["id"]: t for t in watch["party"]["travelers"]}
    meet_airports = watch["party"]["meet_airports"]
    watch_label = watch.get("name", "hawaii").replace("_", " ").title()

    best_combo: dict | None = None
    sections: list[str] = []

    sections.append(f"## {watch_label} watch · {utc_now_iso().split()[0]}")
    sections.append(f"Trip shape: {watch['trip_shape']} | Ticketing: {watch['ticketing']}")
    if watch.get("schedule_priority"):
        sections.append(f"Schedule priority: {watch['schedule_priority']}")
    sections.append(
        "Manual carrier omissions: "
        + (", ".join(_manual_omissions(watch)) if _manual_omissions(watch) else "none")
    )
    sections.append("")

    if not wf.has_provider:
        sections.append("Quote status: INCOMPLETE")
        sections.append(
            "ERROR: Set SERPAPI_API_KEY, DUFFEL_API_KEY, and/or "
            "AMADEUS_API_KEY + AMADEUS_API_SECRET in .env"
        )
        sections.append("")
        sections.append("Deep links (verify manually):")
        for ap in meet_airports:
            sections.append(f"- {kayak_rt_link('YVR', ap, out, ret)}")
            sections.append(f"- {kayak_rt_link('CPR', ap, out, ret)}")
        return "\n".join(sections)

    for airport in meet_airports:
        sections.append(f"### Meet: {airport}")
        yvr = travelers["yvr"]
        cpr = travelers["cpr"]

        yvr_winner, yvr_rt, yvr_ow, yvr_notes = _pick_rt_vs_ows(
            wf, yvr["origin"], airport, out, ret,
            yvr["currency"], yvr["stops"], yvr.get("stop_fallback", "max_1"),
            time_windows=_time_windows(yvr if yvr.get("time_windows") else watch),
            watch={**watch, **yvr},
            return_preference=yvr.get("return_preference"),
        )
        cpr_winner, cpr_rt, cpr_ow, cpr_notes = _pick_rt_vs_ows(
            wf, cpr["origin"], airport, out, ret,
            cpr["currency"], cpr["stops"],
            time_windows=_time_windows(cpr if cpr.get("time_windows") else watch),
            watch={**watch, **cpr},
            return_preference=cpr.get("return_preference"),
        )

        for line in yvr_notes + cpr_notes:
            sections.append(f"  _{line}_")

        def fmt(opt: FareOption | None) -> str:
            if not opt:
                return "NOT PRICED"
            tag = "usable" if opt.usable else f"SKIP ({opt.reject_reason})"
            match = f", {opt.match_type.value}" if opt.match_type != MatchType.STRICT else ""
            exception = (
                f"; exception: {', '.join(opt.near_match_violations)}"
                if opt.near_match_violations
                else ""
            )
            return f"{opt.currency} {opt.total_price:.0f} ({opt.construction}, {opt.stops_by_leg or [opt.stops]} stop(s), {tag}{match}{exception}; {_baggage_note(opt)})"

        sections.append(f"YVR: {fmt(yvr_winner)}")
        if yvr_rt and yvr_ow:
            sections.append(f"  Compare RT {fmt(yvr_rt)} vs OW+OW {fmt(yvr_ow)} → winner {yvr_winner.construction if yvr_winner else 'n/a'}")
        sections.append(f"CPR: {fmt(cpr_winner)}")
        if cpr_rt and cpr_ow:
            sections.append(f"  Compare RT {fmt(cpr_rt)} vs OW+OW {fmt(cpr_ow)} → winner {cpr_winner.construction if cpr_winner else 'n/a'}")

        if yvr_winner and cpr_winner and yvr_winner.usable and cpr_winner.usable:
            yvr_usd = yvr_winner.total_price / fx if yvr_winner.currency == "CAD" else yvr_winner.total_price
            cpr_usd = cpr_winner.total_price if cpr_winner.currency == "USD" else cpr_winner.total_price * fx
            combined_usd = yvr_usd + cpr_usd
            combined_cad = combined_usd * fx
            sections.append(f"Combined: USD {combined_usd:.0f} / CAD {combined_cad:.0f}")
            if benchmark_usd:
                delta = benchmark_usd - combined_usd
                sections.append(
                    f"vs benchmark USD {benchmark_usd}: "
                    f"{'down' if delta > 0 else 'up' if delta < 0 else 'same'} "
                    f"ΔUSD {abs(delta):.0f}"
                )
                if abs(delta) >= 50:
                    sections.append(
                        f"**Price alert:** combined moved ≥USD 50 from benchmark "
                        f"({'cheaper' if delta > 0 else 'more expensive'})"
                    )
            yvr_benchmark = benchmark.get("yvr_cad")
            cpr_benchmark = benchmark.get("cpr_usd")
            if yvr_winner and yvr_benchmark:
                yvr_delta = yvr_benchmark - yvr_winner.total_price
                sections.append(
                    f"YVR vs benchmark CAD {yvr_benchmark}: "
                    f"{'down' if yvr_delta > 0 else 'up' if yvr_delta < 0 else 'same'} "
                    f"ΔCAD {abs(yvr_delta):.0f}"
                )
            if cpr_winner and cpr_benchmark:
                cpr_delta = cpr_benchmark - cpr_winner.total_price
                sections.append(
                    f"CPR vs benchmark USD {cpr_benchmark}: "
                    f"{'down' if cpr_delta > 0 else 'up' if cpr_delta < 0 else 'same'} "
                    f"ΔUSD {abs(cpr_delta):.0f}"
                )

            if best_combo is None or combined_usd < best_combo["combined_usd"]:
                best_combo = {
                    "airport": airport,
                    "yvr": yvr_winner,
                    "cpr": cpr_winner,
                    "combined_usd": combined_usd,
                    "combined_cad": combined_cad,
                }
        else:
            sections.append("Combined: INCOMPLETE (missing priced leg)")
        sections.append(f"Book: {google_rt_link('YVR', airport, out, ret, 'CAD')}")
        sections.append(f"Book: {google_rt_link('CPR', airport, out, ret, 'USD')}")
        sections.append("")

    sections.insert(2, f"Quote status: {'COMPLETE' if best_combo else 'INCOMPLETE'}")

    if best_combo:
        sections.append("## Recommendation")
        sections.append(f"Meet: **{best_combo['airport']}**")
        sections.append(f"Trip shape: round-trip | Ticketing: separate per traveler")
        sections.append(f"Total: USD {best_combo['combined_usd']:.0f} / CAD {best_combo['combined_cad']:.0f}")
        y, c = best_combo["yvr"], best_combo["cpr"]
        sections.append(f"YVR: {y.currency} {y.total_price:.0f} {y.construction} ({', '.join(y.airlines)}; {_baggage_note(y)})")
        sections.append(f"CPR: {c.currency} {c.total_price:.0f} {c.construction} ({', '.join(c.airlines)}; {_baggage_note(c)})")

    return "\n".join(sections)


def run_rome_paris(watch: dict) -> str:
    wf = SearchWaterfall()
    currency = watch.get("currency", "CAD")
    legs_cfg = watch["legs"]
    out_dates = legs_cfg[0]["date_options"]
    home_date = legs_cfg[1]["date"]
    origin_out, dest_out = legs_cfg[0]["origin"], legs_cfg[0]["destination"]
    origin_home, dest_home = legs_cfg[1]["origin"], legs_cfg[1]["destination"]
    stops = watch.get("stops", "max_1")
    benchmark = watch.get("benchmark", {})
    alliance_key = watch.get("alliance_preference")

    sections = [
        f"## Rome/Paris watch · {utc_now_iso().split()[0]} · {currency}",
        f"Trip shape: {watch['trip_shape']} | Ticketing: {watch['ticketing']}",
        "Manual carrier omissions: "
        + (", ".join(_manual_omissions(watch)) if _manual_omissions(watch) else "none"),
        "",
    ]

    if not wf.has_provider:
        sections.append("Quote status: INCOMPLETE")
        sections.append("ERROR: Set API keys in .env")
        for d in out_dates:
            sections.append(f"- {kayak_multi_link([(origin_out, dest_out, d), (origin_home, dest_home, home_date)])}")
        return "\n".join(sections)

    best_result: CompareResult | None = None
    all_priced: list[FareOption] = []

    for out_date in out_dates:
        mc_legs = [(origin_out, dest_out, out_date), (origin_home, dest_home, home_date)]
        mc_options, mc_notes = wf.multi_city_options(mc_legs, currency, stops)
        ow_out_raw, out_notes = wf.one_way_options(
            origin_out, dest_out, out_date, currency, stops, "one_way_out"
        )
        ow_home_raw, home_notes = wf.one_way_options(
            origin_home, dest_home, home_date, currency, stops, "one_way_return"
        )
        mc_rules = _assess_candidates(
            mc_options,
            _time_windows(watch),
            _max_stops(stops),
            watch,
        )
        ow_out_rules = _assess_candidates(
            ow_out_raw,
            [_time_window(watch, "outbound")],
            _max_stops(stops),
            watch,
        )
        ow_home_rules = _assess_candidates(
            ow_home_raw,
            [_time_window(watch, "return")],
            _max_stops(stops),
            watch,
        )
        threshold = float(watch.get("near_match_savings_threshold", 25))
        ow_out = ow_out_rules.recommended(threshold)
        ow_home = ow_home_rules.recommended(threshold)
        two_ow_options = (
            [
                _combine_one_ways(out_option, home_option, currency)
                for out_option in ow_out_rules.strict + ow_out_rules.near_matches
                for home_option in ow_home_rules.strict + ow_home_rules.near_matches
            ]
            if ow_out and ow_home
            else []
        )
        two_ow_rules = _assess_candidates(
            two_ow_options,
            _time_windows(watch),
            _max_stops(stops),
            watch,
        )
        mc = mc_rules.recommended(threshold)
        two_ows = two_ow_rules.recommended(threshold)
        all_priced.extend(mc_rules.strict + mc_rules.near_matches)
        all_priced.extend(two_ow_rules.strict + two_ow_rules.near_matches)
        mc_notes.extend(out_notes + home_notes + mc_rules.notes + two_ow_rules.notes)

        candidates = [c for c in (mc, two_ows) if c and c.usable]
        if not candidates:
            candidates = [c for c in (mc, two_ows) if c]
        if not candidates:
            sections.append(f"### Out {out_date}: NOT PRICED")
            for n in mc_notes:
                sections.append(f"  _{n}_")
            continue

        strict_candidates = [c for c in candidates if c.match_type == MatchType.STRICT]
        near_candidates = [c for c in candidates if c.match_type == MatchType.NEAR_MATCH]
        winner = min(strict_candidates, key=lambda x: x.total_price, default=None)
        if near_candidates:
            near_winner = min(near_candidates, key=lambda x: x.total_price)
            if winner is None or winner.total_price - near_winner.total_price >= float(
                watch.get("near_match_savings_threshold", 25)
            ):
                winner = near_winner
        if winner is None:
            winner = min(candidates, key=lambda x: x.total_price)
        runner = max(candidates, key=lambda x: x.total_price) if len(candidates) > 1 else None
        alt = _find_alliance_alternative(all_priced, winner, alliance_key)

        result = CompareResult(
            quote_status=QuoteStatus.COMPLETE if mc_options else QuoteStatus.INCOMPLETE,
            trip_shape="open_jaw",
            ticketing="one_ticket" if winner.construction == "multi_city" else "two_one_ways",
            winner=winner,
            runner_up=runner,
            one_ticket=mc,
            two_one_ways=two_ows,
            alliance_alternative=alt,
            date_combo={"outbound": out_date, "return": home_date},
            deep_links=[kayak_multi_link(mc_legs)],
            strict_winner=min(
                mc_rules.strict + two_ow_rules.strict,
                key=lambda x: x.total_price,
                default=None,
            ),
            near_matches=mc_rules.near_matches + two_ow_rules.near_matches,
            omission_audit=[
                f"{carrier}: {option.total_price:.0f} {currency}"
                for option in mc_rules.rejected + two_ow_rules.rejected
                for carrier in option.manual_omissions
            ],
        )

        if best_result is None or (winner and winner.total_price < (best_result.winner.total_price if best_result.winner else 1e18)):
            best_result = result

        sections.append(f"### Out {out_date}")
        sections.append(f"- Multi-city one ticket: {mc.total_price if mc else 'NOT PRICED'} {currency}")
        if two_ows:
            sections.append(f"- Two one-ways: {ow_out.total_price if ow_out else '?'} + {ow_home.total_price if ow_home else '?'} = {two_ows.total_price} {currency}")
        sections.append(f"- Winner: {winner.construction} at {winner.total_price:.0f} {currency}")
        for near_option in mc_rules.near_matches + two_ow_rules.near_matches:
            sections.append(
                f"- Near match: {near_option.total_price:.0f} {currency} "
                f"({'; '.join(near_option.near_match_violations)})"
            )
        for rejected in mc_rules.rejected + two_ow_rules.rejected:
            if rejected.manual_omissions:
                sections.append(
                    f"- Manual omission: {', '.join(rejected.manual_omissions)} "
                    f"({rejected.total_price:.0f} {currency})"
                )
        if alt:
            prem = alt.total_price - winner.total_price
            sections.append(f"- Alliance alt ({alliance_key}): {alt.total_price:.0f} (+{prem:.0f})")

    status = QuoteStatus.COMPLETE if best_result and best_result.one_ticket else QuoteStatus.INCOMPLETE
    sections.insert(2, f"Quote status: {status.value}")

    if best_result and best_result.winner:
        w = best_result.winner
        sections.append("")
        sections.append("## Recommendation")
        sections.append(f"Dates: out {best_result.date_combo['outbound']}, home {best_result.date_combo['return']}")
        sections.append(f"Construction: {w.construction} | Ticketing: {best_result.ticketing}")
        sections.append(f"Total: {currency} {w.total_price:.0f} ({_baggage_note(w)})")
        if best_result.strict_winner and best_result.strict_winner is not w:
            sections.append(
                f"Strict winner: {currency} {best_result.strict_winner.total_price:.0f} "
                f"({best_result.strict_winner.construction})"
            )
        if best_result.near_matches:
            sections.append(
                "Near-match alternatives: "
                + "; ".join(
                    f"{option.total_price:.0f} ({', '.join(option.near_match_violations)})"
                    for option in best_result.near_matches
                )
            )
        if best_result.runner_up:
            diff = best_result.runner_up.total_price - w.total_price
            sections.append(f"Beat {best_result.runner_up.construction} by: {currency} {diff:.0f}")
        if best_result.alliance_alternative:
            prem = best_result.alliance_alternative.total_price - w.total_price
            sections.append(
                f"Alliance alternative: {currency} {best_result.alliance_alternative.total_price:.0f} (+{prem:.0f})"
            )
        if benchmark.get("amount"):
            delta = benchmark["amount"] - w.total_price
            sections.append(f"vs benchmark {currency} {benchmark['amount']}: {'down' if delta > 0 else 'up' if delta < 0 else 'same'} Δ{abs(delta):.0f}")

    return "\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description="Travel Agent flight search")
    parser.add_argument(
        "--watch",
        choices=["hawaii", "montreal", "rome-paris"],
        help="Run a watch config",
    )
    parser.add_argument("--config", help="Run an ad-hoc YAML config using the same schema")
    parser.add_argument("--save", action="store_true", help="Save output to quotes/")
    args = parser.parse_args()

    if not args.watch and not args.config:
        parser.print_help()
        return 1

    watch = load_config(args.config) if args.config else load_watch(args.watch)
    if args.config:
        if watch.get("trip_shape", "round_trip") != "round_trip":
            parser.error("--config currently supports trip_shape: round_trip only")
        output = run_round_trip(watch)
    elif args.watch in ("hawaii", "montreal"):
        output = run_hawaii(watch)
    else:
        output = run_rome_paris(watch)

    print(output)

    if args.save:
        quotes_dir = ROOT / "quotes"
        quotes_dir.mkdir(exist_ok=True)
        label = args.watch or Path(args.config).stem
        fname = quotes_dir / f"{utc_now_iso().split()[0]}-{label}.md"
        fname.write_text(output, encoding="utf-8")
        print(f"\nSaved to {fname}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

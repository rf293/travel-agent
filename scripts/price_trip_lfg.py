#!/usr/bin/env python3
"""Price a separate-ticket meet-up trip with LetsFG only (e.g. watches/maui-oahu.yml).

For each meet airport × traveler: round-trip vs two one-ways, reported as
cheapest-any-stops and cheapest-nonstop, plus a flag-only list of traveler
pairs that would share a flight.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.env import load_repo_env  # noqa: E402
from scripts.lib.models import (  # noqa: E402
    FareOption,
    FlightLeg,
    MatchType,
    SourceStatus,
    google_rt_link,
    utc_now_iso,
)
from scripts.lib.rules import TimeWindow, assess_option, local_datetime  # noqa: E402
from scripts.providers.letsfg_provider import LetsFGProvider  # noqa: E402
from scripts.search import (  # noqa: E402
    CandidateSet,
    _assess_candidates,
    _combine_one_ways,
    load_config,
    load_watch,
)

load_repo_env()

ISLANDS = {"OGG": "Maui", "HNL": "Oahu", "KOA": "Big Island", "LIH": "Kauai"}
STOP_CLASSES = ("any", "nonstop")
SEARCH_OK = (SourceStatus.OK, SourceStatus.NO_RESULTS)
SHARED_LEG_TOP_N = 10


class SearchPacer:
    """Sliding-window cap on search starts (PFS allows about 10 searches per 10 minutes)."""

    def __init__(
        self,
        max_searches: int,
        window_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_searches = max_searches
        self.window_seconds = window_seconds
        self.sleep = sleep
        self.clock = clock
        self.starts: deque[float] = deque()

    def wait(self) -> None:
        now = self.clock()
        while self.starts and now - self.starts[0] >= self.window_seconds:
            self.starts.popleft()
        if len(self.starts) >= self.max_searches:
            delay = self.window_seconds - (now - self.starts[0])
            print(f"  pacing: waiting {delay:.0f}s for the LetsFG search window", file=sys.stderr)
            self.sleep(delay)
            self.starts.popleft()
        self.starts.append(self.clock())

    def cooldown(self) -> None:
        print(f"  HTTP 429: cooling down {self.window_seconds:.0f}s", file=sys.stderr)
        self.sleep(self.window_seconds)
        self.starts.clear()


class PacedLetsFG(LetsFGProvider):
    """LetsFG with search pacing, one retry after a 429, and an optional raw-response cache."""

    def __init__(self, pacer: SearchPacer, cache_dir: Path | None = None, **kwargs):
        super().__init__(**kwargs)
        self.pacer = pacer
        self.cache_dir = cache_dir
        self.live_searches = 0

    @property
    def connected(self) -> bool:
        return super().available

    @property
    def available(self) -> bool:
        return self.connected or self.cache_dir is not None

    def _cache_file(self, payload: dict) -> Path | None:
        if self.cache_dir is None:
            return None
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
        return self.cache_dir / f"{payload['origin']}-{payload['destination']}-{key}.json"

    def _run_search(self, payload):
        cache_file = self._cache_file(payload)
        if cache_file and cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))["response"], SourceStatus.OK, ""
        if not self.connected:
            return None, SourceStatus.NO_KEY, "LetsFG not connected and no cached response"

        self.pacer.wait()
        self.live_searches += 1
        data, status, err = super()._run_search(payload)
        if status == SourceStatus.BLOCKED and "429" in err:
            self.pacer.cooldown()
            self.pacer.wait()
            self.live_searches += 1
            data, status, err = super()._run_search(payload)
        if cache_file and status == SourceStatus.OK and data:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps({"payload": payload, "response": data}, indent=1), encoding="utf-8"
            )
        return data, status, err


@dataclass
class Lane:
    traveler: dict
    airport: str
    pools: dict[str, dict[str, list[FareOption]]]
    notes: list[str]
    complete: bool


@dataclass
class ClassResult:
    stop_class: str
    winner: FareOption | None
    strict: FareOption | None
    near: FareOption | None
    rt: FareOption | None
    ow: FareOption | None
    ow_parts: tuple[FareOption, FareOption] | None
    offers_seen: dict[str, int]
    full_trips: list[FareOption] = field(default_factory=list)
    skipped: list[FareOption] = field(default_factory=list)


def to_usd(amount: float, currency: str, cad_per_usd: float) -> float:
    if currency == "USD":
        return amount
    if currency == "CAD":
        return amount / cad_per_usd
    raise ValueError(f"no FX for {currency}")


def money(amount: float, currency: str) -> str:
    symbol = {"CAD": "CA$", "USD": "US$"}.get(currency, f"{currency} ")
    return f"{symbol}{amount:,.0f}"


def traveler_windows(traveler: dict, watch: dict) -> list[TimeWindow | None]:
    configs = traveler.get("time_windows") or watch.get("time_windows") or {}
    near = int(watch.get("near_match_minutes", 30))
    return [
        TimeWindow.from_config(configs[name], default_near_match_minutes=near) if configs.get(name) else None
        for name in ("outbound", "return")
    ]


def _is_nonstop(option: FareOption) -> bool:
    return all(count == 0 for count in (option.stops_by_leg or [option.stops]))


def _dedupe(options: list[FareOption]) -> list[FareOption]:
    seen: set[tuple] = set()
    unique: list[FareOption] = []
    for option in options:
        key = (
            round(option.total_price, 2),
            option.currency,
            tuple((leg.origin, leg.destination, leg.departure_time, leg.flight_number) for leg in option.legs),
        )
        if key not in seen:
            seen.add(key)
            unique.append(option)
    return unique


def _normalize_currency(
    options: list[FareOption], currency: str, cad_per_usd: float, notes: list[str]
) -> list[FareOption]:
    kept: list[FareOption] = []
    for option in options:
        if option.currency == currency:
            kept.append(option)
        elif {option.currency, currency} == {"CAD", "USD"}:
            factor = cad_per_usd if currency == "CAD" else 1 / cad_per_usd
            option.price *= factor
            option.bag_fees *= factor
            if option.all_in_price is not None:
                option.all_in_price *= factor
            notes.append(f"converted one {option.currency} offer to {currency} at stated FX")
            option.currency = currency
            kept.append(option)
        else:
            notes.append(f"dropped one offer quoted in {option.currency}")
    return kept


def search_lane(provider: LetsFGProvider, watch: dict, traveler: dict, airport: str, cad_per_usd: float) -> Lane:
    origin, currency = traveler["origin"], traveler["currency"]
    out_date, ret_date = watch["dates"]["outbound"], watch["dates"]["return"]
    adults = int(watch.get("pax_per_traveler", 1))
    raw: dict[str, list[FareOption]] = {}
    statuses: dict[str, SourceStatus] = {}
    notes: list[str] = []

    def run(key: str, fn: Callable, **kwargs) -> None:
        print(f"  letsfg {key} {origin}⇄{airport}…", file=sys.stderr)
        options, status, err = fn(currency=currency, adults=adults, limit=100, **kwargs)
        raw[key] = _normalize_currency(options, currency, cad_per_usd, notes)
        statuses[key] = status
        notes.append(
            f"letsfg {key.replace('_', ' ')} {origin}-{airport}: {status.value}, "
            f"{len(options)} offers" + (f" ({err})" if err else "")
        )

    rt = provider.search_round_trip_options
    ow = provider.search_one_way_options
    run("rt_any", rt, origin=origin, destination=airport, outbound=out_date, return_date=ret_date, stops_rule="any")
    run("rt_nonstop", rt, origin=origin, destination=airport, outbound=out_date, return_date=ret_date, stops_rule="nonstop")
    run("out_any", ow, origin=origin, destination=airport, date=out_date, stops_rule="any", label="one_way_out")
    run("ret_any", ow, origin=airport, destination=origin, date=ret_date, stops_rule="any", label="one_way_return")
    if raw["rt_nonstop"]:
        run("out_nonstop", ow, origin=origin, destination=airport, date=out_date, stops_rule="nonstop", label="one_way_out")
        run("ret_nonstop", ow, origin=airport, destination=origin, date=ret_date, stops_rule="nonstop", label="one_way_return")
    else:
        raw["out_nonstop"], raw["ret_nonstop"] = [], []
        notes.append(
            f"no nonstop {origin}⇄{airport} round trip on LetsFG; skipped nonstop one-way searches "
            "(nonstop class uses 0-stop offers from the any-stop results)"
        )

    pools = {
        "any": {
            part: _dedupe(raw[f"{part}_any"] + raw[f"{part}_nonstop"]) for part in ("rt", "out", "ret")
        },
        "nonstop": {
            part: _dedupe(raw[f"{part}_nonstop"] + [o for o in raw[f"{part}_any"] if _is_nonstop(o)])
            for part in ("rt", "out", "ret")
        },
    }
    complete = all(status in SEARCH_OK for status in statuses.values())
    return Lane(traveler, airport, pools, notes, complete)


def _assess(
    pool: list[FareOption], windows: list[TimeWindow | None], max_stops: int | None, ctx: dict
) -> CandidateSet:
    options = [copy.copy(option) for option in pool]
    assessed = _assess_candidates(options, windows, max_stops, ctx)
    for option in assessed.strict + assessed.near_matches:
        if option.carry_on_included is False:
            option.usable = False
            option.match_type = MatchType.REJECTED
            option.reject_reason = "carry-on not included"
    strict = [o for o in assessed.strict if o.usable]
    near = [o for o in assessed.near_matches if o.usable]
    rejected = sorted((o for o in options if not o.usable), key=lambda o: o.total_price)
    return CandidateSet(strict, near, rejected, assessed.notes)


def _combo(
    out_option: FareOption,
    ret_option: FareOption,
    windows: list[TimeWindow | None],
    max_stops: int | None,
    currency: str,
) -> FareOption:
    combined = _combine_one_ways(out_option, ret_option, currency)
    combined.self_transfer = out_option.self_transfer or ret_option.self_transfer
    return assess_option(combined, time_windows=windows, max_stops=max_stops)


def evaluate_class(
    pools: dict[str, list[FareOption]],
    stop_class: str,
    windows: list[TimeWindow | None],
    ctx: dict,
    currency: str,
) -> ClassResult:
    max_stops = 0 if stop_class == "nonstop" else None
    threshold = float(ctx.get("near_match_savings_threshold", 25))
    rt = _assess(pools["rt"], windows, max_stops, ctx)
    out = _assess(pools["out"], windows[:1], max_stops, ctx)
    ret = _assess(pools["ret"], windows[1:], max_stops, ctx)

    usable_out = sorted(out.strict + out.near_matches, key=lambda o: o.total_price)
    usable_ret = sorted(ret.strict + ret.near_matches, key=lambda o: o.total_price)
    pairs: list[tuple[FareOption, FareOption]] = []
    if out.strict and ret.strict:
        pairs.append((out.strict[0], ret.strict[0]))
    if usable_out and usable_ret and (usable_out[0], usable_ret[0]) not in pairs:
        pairs.append((usable_out[0], usable_ret[0]))
    combos = [(pair, _combo(*pair, windows, max_stops, currency)) for pair in pairs]
    combos = [(pair, c) for pair, c in combos if c.usable]

    rt_best = (rt.strict or rt.near_matches or [None])[0]
    strict_ow = [(p, c) for p, c in combos if c.match_type == MatchType.STRICT]
    ow_pick = (strict_ow or combos or [(None, None)])[0]

    strict_candidates = rt.strict[:1] + [c for _, c in strict_ow]
    near_candidates = rt.near_matches[:1] + [c for _, c in combos if c.match_type == MatchType.NEAR_MATCH]
    strict_best = min(strict_candidates, key=lambda o: o.total_price, default=None)
    near_best = min(near_candidates, key=lambda o: o.total_price, default=None)
    if near_best and strict_best and near_best.total_price >= strict_best.total_price:
        near_best = None
    winner = strict_best
    if near_best and (strict_best is None or strict_best.total_price - near_best.total_price >= threshold):
        winner = near_best

    full_trips = rt.strict + rt.near_matches + [
        combo
        for o in usable_out[:SHARED_LEG_TOP_N]
        for r in usable_ret[:SHARED_LEG_TOP_N]
        if (combo := _combo(o, r, windows, max_stops, currency)).usable
    ]
    return ClassResult(
        stop_class=stop_class,
        winner=winner,
        strict=strict_best,
        near=near_best,
        rt=rt_best,
        ow=ow_pick[1],
        ow_parts=ow_pick[0],
        offers_seen={part: len(pool) for part, pool in pools.items()},
        full_trips=full_trips,
        skipped=sorted(rt.rejected + out.rejected + ret.rejected, key=lambda o: o.total_price)[:3],
    )


def _segment_key(leg: FlightLeg) -> tuple[str, str, str] | None:
    if not (leg.origin and leg.destination and leg.departure_time):
        return None
    return leg.origin, leg.destination, str(leg.departure_time)[:16]


def shared_leg_pairs(
    first: list[FareOption], second: list[FareOption], cad_per_usd: float, limit: int = 3
) -> list[tuple[float, FareOption, FareOption, list[FlightLeg]]]:
    """Cheapest pairs per distinct set of shared flights (same route and departure minute)."""
    best: dict[tuple, tuple[float, FareOption, FareOption, list[FlightLeg]]] = {}
    for a in first:
        legs_a = {key: leg for leg in a.legs if (key := _segment_key(leg))}
        if not legs_a:
            continue
        for b in second:
            shared = sorted({key for leg in b.legs if (key := _segment_key(leg)) in legs_a})
            if not shared:
                continue
            total = to_usd(a.total_price, a.currency, cad_per_usd) + to_usd(b.total_price, b.currency, cad_per_usd)
            signature = tuple(shared)
            if signature not in best or total < best[signature][0]:
                best[signature] = (total, a, b, [legs_a[key] for key in shared])
    return sorted(best.values(), key=lambda item: item[0])[:limit]


def _clock(timestamp: str) -> str:
    moment = local_datetime(timestamp)
    return moment.strftime("%a %-d %b %H:%M") if moment else (timestamp or "?")


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _directions(option: FareOption) -> list[list[FlightLeg]]:
    if not option.stops_by_leg or len(option.stops_by_leg) < 2:
        return [option.legs]
    split = option.stops_by_leg[0] + 1
    return [option.legs[:split], option.legs[split:]]


def _direction_text(legs: list[FlightLeg], layovers: list[int]) -> str:
    if not legs:
        return "no segment detail"
    flights = "+".join(leg.flight_number or leg.airline or "?" for leg in legs)
    route = f"{legs[0].origin} {_clock(legs[0].departure_time)} → {legs[-1].destination} {_clock(legs[-1].arrival_time)}"
    if len(legs) == 1:
        return f"{flights} {route}, nonstop"
    vias = ", ".join(
        f"{leg.destination}" + (f" {_hm(layovers[i])}" if i < len(layovers) and layovers[i] else "")
        for i, leg in enumerate(legs[:-1])
    )
    return f"{flights} {route}, {len(legs) - 1} stop via {vias}"


def _bag_text(option: FareOption) -> str:
    return {True: "carry-on included", False: "no carry-on"}.get(option.carry_on_included, "carry-on unconfirmed")


def describe(option: FareOption) -> str:
    kind = "RT, one ticket" if option.construction == "round_trip" else "two one-ways, two tickets"
    parts = [f"{money(option.total_price, option.currency)} ({kind})", "/".join(option.airlines) or "?"]
    directions = _directions(option)
    layovers = list(option.layovers_by_leg) + [[]] * (len(directions) - len(option.layovers_by_leg))
    for label, legs, leg_layovers in zip(("Out", "Back"), directions, layovers):
        parts.append(f"{label}: {_direction_text(legs, leg_layovers)}")
    parts.append(_bag_text(option))
    if option.match_type == MatchType.NEAR_MATCH:
        parts.append(f"NEAR MATCH: {'; '.join(option.near_match_violations)}")
    return " · ".join(parts)


def _compare_line(result: ClassResult, currency: str) -> str:
    rt_text = money(result.rt.total_price, currency) if result.rt else "none usable"
    if result.rt and result.rt.match_type == MatchType.NEAR_MATCH:
        rt_text += " (near match)"
    if result.ow and result.ow_parts:
        out_part, ret_part = result.ow_parts
        ow_text = (
            f"{money(out_part.total_price, currency)} + {money(ret_part.total_price, currency)} "
            f"= {money(result.ow.total_price, currency)}"
            + (" (near match)" if result.ow.match_type == MatchType.NEAR_MATCH else "")
        )
    else:
        ow_text = "none usable"
    line = f"Compare: RT {rt_text} vs OW+OW {ow_text}"
    if result.rt and result.ow:
        cheaper, dearer = sorted((result.rt, result.ow), key=lambda o: o.total_price)
        label = "RT" if cheaper is result.rt else "two one-ways"
        line += f" → {label} cheaper by {money(dearer.total_price - cheaper.total_price, currency)}"
    return line


def _class_lines(result: ClassResult, currency: str, title: str) -> list[str]:
    if result.winner is None:
        seen = ", ".join(f"{part} {count}" for part, count in result.offers_seen.items())
        return [f"- {title}: none usable (LetsFG offers seen: {seen})"]
    lines = [f"- {title}: {describe(result.winner)}", f"  - {_compare_line(result, currency)}"]
    if result.near and result.near is not result.winner:
        saving = result.strict.total_price - result.near.total_price if result.strict else 0
        lines.append(
            f"  - Near match (not chosen, saves {money(saving, currency)}): {describe(result.near)}"
        )
    if result.winner.match_type == MatchType.NEAR_MATCH and result.strict:
        lines.append(f"  - Strict alternative: {describe(result.strict)}")
    return lines


def _window_text(windows: list[TimeWindow | None]) -> str:
    def one(window: TimeWindow | None, label: str) -> str:
        if window is None:
            return f"{label} any time"
        bounds = []
        if window.earliest is not None:
            bounds.append(f"≥{window.earliest // 60:02d}:{window.earliest % 60:02d}")
        if window.latest is not None:
            bounds.append(f"≤{window.latest // 60:02d}:{window.latest % 60:02d}")
        if window.earliest_at:
            bounds.append(f"≥{window.earliest_at:%a %-d %b %H:%M}")
        if window.latest_at:
            bounds.append(f"≤{window.latest_at:%a %-d %b %H:%M}")
        return f"{label} {window.field} {' '.join(bounds)} (±{window.near_match_minutes} min near match)"

    return f"{one(windows[0], 'out')}; {one(windows[1], 'back')}"


def price_trip(watch: dict, provider: LetsFGProvider, cad_per_usd: float, airports: list[str] | None = None) -> str:
    travelers = watch["party"]["travelers"]
    airports = airports or list(watch["party"]["meet_airports"])
    lanes: dict[tuple[str, str], Lane] = {}
    results: dict[tuple[str, str], dict[str, ClassResult]] = {}
    for airport in airports:
        for traveler in travelers:
            lane = search_lane(provider, watch, traveler, airport, cad_per_usd)
            ctx = {**watch, **traveler}
            windows = traveler_windows(traveler, watch)
            lanes[(airport, traveler["id"])] = lane
            results[(airport, traveler["id"])] = {
                stop_class: evaluate_class(lane.pools[stop_class], stop_class, windows, ctx, traveler["currency"])
                for stop_class in STOP_CLASSES
            }

    complete = all(lane.complete for lane in lanes.values())
    header = [
        f"## {' vs '.join(ISLANDS.get(a, a) for a in airports)} · LetsFG only · quoted {utc_now_iso()}",
        f"Quote status: {'COMPLETE' if complete else 'INCOMPLETE'}",
        "Trip shape: round-trip | Ticketing: separate ticket per traveler | "
        f"Cabin: {watch.get('cabin', 'economy')}, {watch.get('bags', 'bags not specified')}",
        f"Dates: out {watch['dates']['outbound']}, back {watch['dates']['return']} | "
        f"FX: {cad_per_usd} CAD per USD (stated, combined totals only)",
    ]
    for traveler in travelers:
        header.append(
            f"{traveler['label']} ({traveler['origin']}, {traveler['currency']}): "
            f"{_window_text(traveler_windows(traveler, watch))}"
        )

    summary = ["", "| Island | Cheapest any stops, combined | Nonstop where it exists, combined |", "|---|---|---|"]
    detail: list[str] = []
    ranking: list[tuple[float, str, list[FareOption]]] = []
    for airport in airports:
        island = ISLANDS.get(airport, airport)
        detail.append("")
        detail.append(f"### Meet {airport} ({island})")
        for traveler in travelers:
            currency = traveler["currency"]
            classes = results[(airport, traveler["id"])]
            detail.append("")
            detail.append(f"**{traveler['label']} ({traveler['origin']} ⇄ {airport}, {currency})**")
            detail.extend(_class_lines(classes["any"], currency, "Cheapest any stops"))
            detail.extend(_class_lines(classes["nonstop"], currency, "Cheapest nonstop"))
            for skipped in classes["any"].skipped:
                detail.append(
                    f"- Skip: {money(skipped.total_price, currency)} {skipped.construction.replace('_', ' ')} "
                    f"{'/'.join(skipped.airlines)} — {skipped.reject_reason}"
                )

        any_winners = [results[(airport, t["id"])]["any"].winner for t in travelers]
        ns_mix = [
            results[(airport, t["id"])]["nonstop"].winner or results[(airport, t["id"])]["any"].winner
            for t in travelers
        ]
        detail.append("")
        detail.append(f"**Combined ({airport})**")
        any_text = _combined_text(any_winners, cad_per_usd)
        detail.append(f"- Cheapest any stops: {any_text}")
        ns_labels = [
            f"{t['origin']} {'nonstop' if results[(airport, t['id'])]['nonstop'].winner else 'best (no nonstop)'}"
            for t in travelers
        ]
        ns_text = _combined_text(ns_mix, cad_per_usd)
        detail.append(f"- Nonstop where it exists ({' + '.join(ns_labels)}): {ns_text}")
        cheapest_usd = (
            sum(to_usd(o.total_price, o.currency, cad_per_usd) for o in any_winners)
            if all(any_winners)
            else None
        )
        if cheapest_usd is not None:
            ranking.append((cheapest_usd, airport, any_winners))
        summary.append(f"| {island} ({airport}) | {any_text} | {ns_text} |")

        if len(travelers) == 2:
            pairs = shared_leg_pairs(
                results[(airport, travelers[0]["id"])]["any"].full_trips,
                results[(airport, travelers[1]["id"])]["any"].full_trips,
                cad_per_usd,
            )
            detail.append("- Shared-leg options (flag only, not used for the recommendation):")
            if not pairs:
                detail.append("  - none — no usable pair shares a flight")
            for total, a, b, legs in pairs:
                flights = ", ".join(
                    f"{leg.flight_number or leg.airline} {leg.origin}→{leg.destination} {_clock(leg.departure_time)}"
                    for leg in legs
                )
                premium = f" (+US${total - cheapest_usd:,.0f} vs cheapest)" if cheapest_usd is not None else ""
                detail.append(
                    f"  - Share {flights}: {travelers[0]['origin']} {money(a.total_price, a.currency)} "
                    f"+ {travelers[1]['origin']} {money(b.total_price, b.currency)} = US${total:,.0f}{premium}"
                )
        for traveler in travelers:
            detail.append(
                f"- Book/verify {traveler['origin']}: "
                f"{google_rt_link(traveler['origin'], airport, watch['dates']['outbound'], watch['dates']['return'], traveler['currency'])}"
            )

    recommendation = ["", "## Recommendation"]
    if ranking:
        ranking.sort(key=lambda item: item[0])
        total, airport, winners = ranking[0]
        recommendation.append(
            f"Meet {ISLANDS.get(airport, airport)} ({airport}): US${total:,.0f} / CA${total * cad_per_usd:,.0f} combined"
        )
        for traveler, option in zip(travelers, winners):
            recommendation.append(f"- {traveler['label']}: {describe(option)}")
        if len(ranking) > 1:
            runner_total, runner_airport, _ = ranking[1]
            recommendation.append(
                f"Beats {ISLANDS.get(runner_airport, runner_airport)} ({runner_airport}) by US${runner_total - total:,.0f}"
            )
    else:
        recommendation.append("No island has a usable fare for both travelers.")

    audit = ["", "## Audit"]
    audit.append(f"LetsFG live searches this run: {getattr(provider, 'live_searches', 'n/a')}")
    for lane in lanes.values():
        audit.extend(f"- {note}" for note in lane.notes)

    return "\n".join(header + summary + recommendation + detail + audit)


def _combined_text(options: list[FareOption | None], cad_per_usd: float) -> str:
    if not all(options):
        return "INCOMPLETE (a traveler has no usable fare)"
    usd = sum(to_usd(o.total_price, o.currency, cad_per_usd) for o in options)
    parts = " + ".join(money(o.total_price, o.currency) for o in options)
    near = " (includes near match)" if any(o.match_type == MatchType.NEAR_MATCH for o in options) else ""
    return f"{parts} = US${usd:,.0f} / CA${usd * cad_per_usd:,.0f}{near}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--watch", default="maui-oahu", help="Watch name under watches/")
    parser.add_argument("--config", help="Trip YAML path (overrides --watch)")
    parser.add_argument("--airport", action="append", help="Limit to these meet airports (repeatable)")
    parser.add_argument("--fx", type=float, help="CAD per USD for combined totals")
    parser.add_argument("--cache", type=Path, help="Directory for raw LetsFG responses (reused on rerun)")
    parser.add_argument("--save", action="store_true", help="Write quotes/YYYY-MM-DD-<watch>-lfg.md")
    args = parser.parse_args()

    watch = load_config(args.config) if args.config else load_watch(args.watch)
    cad_per_usd = args.fx or float((watch.get("fx") or {}).get("cad_per_usd", 1.39))
    lfg = watch.get("letsfg") or {}
    pacer = SearchPacer(int(lfg.get("max_searches_per_window", 10)), float(lfg.get("window_seconds", 600)))
    provider = PacedLetsFG(pacer, cache_dir=args.cache)
    if not provider.available:
        print(
            "LetsFG is not connected. Run `letsfg auth` (or scripts/letsfg_remote_auth.py start/finish "
            "on a remote machine), then rerun.",
            file=sys.stderr,
        )
        return 2

    output = price_trip(watch, provider, cad_per_usd, args.airport)
    print(output)
    if args.save:
        label = Path(args.config).stem if args.config else args.watch
        path = ROOT / "quotes" / f"{utc_now_iso().split()[0]}-{label}-lfg.md"
        path.write_text(output, encoding="utf-8")
        print(f"\nSaved to {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

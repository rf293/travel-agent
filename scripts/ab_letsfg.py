#!/usr/bin/env python3
"""A/B: current waterfall vs LetsFG on hawaii / montreal watches.

Reports cheapest through-fare, LetsFG split offers, current winner, and source coverage.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.env import load_repo_env  # noqa: E402
from scripts.lib.models import FareOption, MatchType, utc_now_iso  # noqa: E402
from scripts.lib.rules import TimeWindow  # noqa: E402
from scripts.providers.letsfg_provider import LetsFGProvider  # noqa: E402
from scripts.search import (  # noqa: E402
    SearchWaterfall,
    _assess_candidates,
    _max_stops,
    _pick_rt_vs_ows,
    load_watch,
)

load_repo_env()


@dataclass
class LaneResult:
    label: str
    options: list[FareOption]
    notes: list[str]
    winner: FareOption | None
    through_best: FareOption | None
    split_best: FareOption | None
    sources: Counter


def _is_split(option: FareOption) -> bool:
    return bool(option.raw.get("_letsfg_split")) or (
        option.self_transfer and option.ticketing == "two_tickets" and option.source.startswith("letsfg")
    )


def _source_key(option: FareOption) -> str:
    if option.source.startswith("letsfg:"):
        return option.source.split(":", 1)[1] or "letsfg"
    if option.source.startswith("letsfg"):
        return str(option.raw.get("source") or option.raw.get("provider") or "letsfg")
    return option.source.split("+")[0] if option.source else "unknown"


def summarize_lane(label: str, options: list[FareOption], notes: list[str]) -> LaneResult:
    through = [o for o in options if not _is_split(o)]
    splits = [o for o in options if _is_split(o)]
    through_best = min(through, key=lambda o: o.total_price, default=None)
    split_best = min(splits, key=lambda o: o.total_price, default=None)
    winner = min(options, key=lambda o: o.total_price, default=None)
    sources: Counter = Counter(_source_key(o) for o in options)
    return LaneResult(label, options, notes, winner, through_best, split_best, sources)


def _fmt(option: FareOption | None) -> str:
    if not option:
        return "—"
    airlines = "/".join(option.airlines) or "?"
    stops = option.stops_by_leg or [option.stops]
    split = " SPLIT" if _is_split(option) else ""
    times = " | ".join(option.leg_departure_times[:2]) if option.leg_departure_times else ""
    return (
        f"{option.currency} {option.total_price:.0f}{split} "
        f"[{option.source}] {airlines} stops={stops}"
        + (f" dep {times}" if times else "")
    )


def traveler_windows(traveler: dict, watch: dict) -> list[TimeWindow | None]:
    windows = traveler.get("time_windows") or watch.get("time_windows") or {}
    near = int(watch.get("near_match_minutes", 30))
    result: list[TimeWindow | None] = []
    for name in ("outbound", "return"):
        cfg = windows.get(name)
        result.append(TimeWindow.from_config(cfg, default_near_match_minutes=near) if cfg else None)
    return result


def search_traveler_baseline(
    wf: SearchWaterfall,
    watch: dict,
    traveler: dict,
    dest: str,
) -> LaneResult:
    origin = traveler["origin"]
    currency = traveler.get("currency", "USD")
    stops = traveler.get("stops", "any")
    fallback = traveler.get("stop_fallback")
    out = watch["dates"]["outbound"]
    ret = watch["dates"]["return"]
    windows = traveler_windows(traveler, watch)
    return_pref = traveler.get("return_preference")
    winner, rt, ows, notes = _pick_rt_vs_ows(
        wf,
        origin,
        dest,
        out,
        ret,
        currency,
        stops,
        stop_fallback=fallback,
        time_windows=windows,
        watch={**watch, **traveler},
        return_preference=return_pref,
    )
    options = [o for o in (rt, ows, winner) if o is not None]
    # Prefer the assessed winner when present.
    lane = summarize_lane("baseline", options, notes)
    lane.winner = winner
    return lane


def search_traveler_letsfg(
    provider: LetsFGProvider,
    watch: dict,
    traveler: dict,
    dest: str,
    *,
    include_ows: bool = False,
) -> LaneResult:
    origin = traveler["origin"]
    currency = traveler.get("currency", "USD")
    stops = traveler.get("stops", "any")
    if stops == "nonstop_preferred":
        stops = traveler.get("stop_fallback") or "max_1"
    out = watch["dates"]["outbound"]
    ret = watch["dates"]["return"]
    notes: list[str] = []
    options: list[FareOption] = []

    # One RT search surfaces through-fares and late-merged split tickets (wait_for_split).
    rt_opts, status, err = provider.search_round_trip_options(
        origin, dest, out, ret, currency, adults=1, stops_rule=stops
    )
    notes.append(f"letsfg RT {origin}-{dest}: {status.value}" + (f" ({err})" if err else ""))
    options.extend(rt_opts)

    if include_ows:
        ow_out, status, err = provider.search_one_way_options(
            origin, dest, out, currency, adults=1, stops_rule=stops, label="one_way_out"
        )
        notes.append(f"letsfg OW out: {status.value}" + (f" ({err})" if err else ""))
        ow_ret, status, err = provider.search_one_way_options(
            dest, origin, ret, currency, adults=1, stops_rule=stops, label="one_way_return"
        )
        notes.append(f"letsfg OW ret: {status.value}" + (f" ({err})" if err else ""))

        from scripts.search import _combine_one_ways

        if ow_out and ow_ret:
            for left in sorted(ow_out, key=lambda o: o.total_price)[:8]:
                for right in sorted(ow_ret, key=lambda o: o.total_price)[:8]:
                    options.append(_combine_one_ways(left, right, currency))

    windows = traveler_windows(traveler, watch)
    assessed = _assess_candidates(options, windows, _max_stops(stops), {**watch, **traveler})
    usable = assessed.strict + assessed.near_matches
    notes.extend(assessed.notes)
    if not usable and options:
        notes.append(
            "no LFG offers passed time/stop constraints; "
            "coverage stats below are unconstrained"
        )
    lane = summarize_lane("letsfg", usable if usable else [], notes)
    # Attach unconstrained through/split for coverage reporting.
    unconstrained = summarize_lane("letsfg_raw", options, [])
    if not lane.winner and unconstrained.winner:
        lane.through_best = unconstrained.through_best
        lane.split_best = unconstrained.split_best
        lane.sources = unconstrained.sources
        lane.options = unconstrained.options
        notes.append(
            f"unconstrained LFG cheapest: {unconstrained.winner.currency} "
            f"{unconstrained.winner.total_price:.0f} [{unconstrained.winner.source}]"
        )
        lane.notes = notes
    return lane


def run_watch(
    name: str,
    *,
    dests: list[str] | None = None,
    pause_s: float = 8.0,
    include_ows: bool = False,
) -> str:
    watch = load_watch(name)
    meet = list(watch["party"]["meet_airports"])
    # Prefer requested dests that belong to this watch; else primary meet airport
    # (keeps PFS under the 10 searches / 10 min card limit).
    if dests is not None:
        selected = [d for d in dests if d in meet] or meet[:1]
    else:
        selected = meet[:1]
    lines = [
        f"# LetsFG A/B — {name}",
        f"Quoted: {utc_now_iso()}",
        f"Dates: {watch['dates']['outbound']} → {watch['dates']['return']}",
        f"Meet airports searched: {', '.join(selected)}"
        + (f" (watch also lists {', '.join(meet[1:])})" if len(meet) > len(selected) else ""),
        "",
    ]

    baseline_wf = SearchWaterfall(include_letsfg=False)
    letsfg = LetsFGProvider()
    if not letsfg.available:
        lines.append(
            "LetsFG unavailable: run `letsfg auth` once "
            "(card connect at letsfg.co/connect — nothing charged; token auto-refreshes)."
        )
        lines.append("")

    first_lfg = True
    for dest in selected:
        lines.append(f"# Meet: {dest}")
        lines.append("")
        for traveler in watch["party"]["travelers"]:
            label = traveler.get("label") or traveler["id"]
            origin = traveler["origin"]
            lines.append(f"## {label} ({origin} ⇄ {dest})")
            lines.append("")

            base = search_traveler_baseline(baseline_wf, watch, traveler, dest)
            lines.append(f"**Current winner:** {_fmt(base.winner)}")
            lines.append(f"**Baseline through (best non-split):** {_fmt(base.through_best)}")
            lines.append(f"**Baseline sources:** {dict(base.sources) or '{}'}")
            for note in base.notes[:12]:
                lines.append(f"- {note}")
            lines.append("")

            if letsfg.available:
                if not first_lfg and pause_s > 0:
                    import time

                    time.sleep(pause_s)
                first_lfg = False
                lfg = search_traveler_letsfg(
                    letsfg, watch, traveler, dest, include_ows=include_ows
                )
                lines.append(f"**LFG overall cheapest:** {_fmt(lfg.winner)}")
                lines.append(f"**LFG through-fare:** {_fmt(lfg.through_best)}")
                lines.append(f"**LFG split best:** {_fmt(lfg.split_best)}")
                lines.append(f"**LFG source coverage:** {dict(lfg.sources) or '{}'}")
                for note in lfg.notes[:12]:
                    lines.append(f"- {note}")

                if base.winner and lfg.winner and base.winner.currency == lfg.winner.currency:
                    delta = base.winner.total_price - lfg.winner.total_price
                    lines.append(
                        f"**Delta (baseline − LFG):** {base.winner.currency} {delta:+.0f} "
                        f"({'LFG cheaper' if delta > 0 else 'baseline cheaper' if delta < 0 else 'tie'})"
                    )
                elif base.winner and lfg.winner:
                    lines.append(
                        f"**Delta:** currencies differ "
                        f"({base.winner.currency} vs {lfg.winner.currency}) — compare manually"
                    )
            lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="A/B LetsFG vs current waterfall")
    parser.add_argument(
        "--watch",
        choices=["hawaii", "montreal"],
        action="append",
        help="Watch to run (repeatable). Default: both.",
    )
    parser.add_argument(
        "--dest",
        action="append",
        help="Meet airport to price (repeatable). Default: first meet airport only.",
    )
    parser.add_argument(
        "--all-dests",
        action="store_true",
        help="Price every meet airport (uses more LetsFG search quota).",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=8.0,
        help="Seconds between traveler LFG runs (rate-limit pacing). Default 8.",
    )
    parser.add_argument(
        "--include-ows",
        action="store_true",
        help="Also run LetsFG one-ways (3× quota). Default is RT-only for through vs split.",
    )
    parser.add_argument("--save", action="store_true", help="Write quotes/YYYY-MM-DD-ab-letsfg-*.md")
    args = parser.parse_args()
    watches = args.watch or ["hawaii", "montreal"]

    outputs: list[str] = []
    for name in watches:
        print(f"Running A/B for {name}…", file=sys.stderr)
        watch = load_watch(name)
        dests = list(watch["party"]["meet_airports"]) if args.all_dests else args.dest
        text = run_watch(
            name, dests=dests, pause_s=args.pause, include_ows=args.include_ows
        )
        outputs.append(text)
        print(text)
        print("", file=sys.stderr)

    if args.save:
        quotes = ROOT / "quotes"
        quotes.mkdir(exist_ok=True)
        day = utc_now_iso().split()[0]
        for name, text in zip(watches, outputs):
            path = quotes / f"{day}-ab-letsfg-{name}.md"
            path.write_text(text, encoding="utf-8")
            print(f"Saved {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

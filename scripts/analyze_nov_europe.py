#!/usr/bin/env python3
"""Price Nov 2026 Europe trip sequences for YYC + ATL travellers."""

from __future__ import annotations

import itertools
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.models import FareOption, MatchType  # noqa: E402
from scripts.lib.rules import assess_option, apply_layover_preference  # noqa: E402
from scripts.providers.duffel_provider import DuffelProvider  # noqa: E402
from scripts.providers.serpapi_provider import SerpApiProvider  # noqa: E402

CAD_PER_USD = 1.39
EUR_PER_USD = 0.92
MAX_STOPS = 1
EST_CHECKED_BAG_FEE_USD = 70  # per traveller when bag status unknown/not included

CITIES = {
    "HAG": ("Den Haag", 4, "AMS"),
    "STO": ("Stockholm", 10, "ARN"),
    "ATH": ("Athens", 5, "ATH"),
}

TRAVELLERS = {
    "yyc": {"origin": "YYC", "currency": "CAD", "gl": "ca"},
    "atl": {"origin": "ATL", "currency": "USD", "gl": "us"},
}


@dataclass
class TripPlan:
    sequence: str
    start_day: int
    first_airport: str
    last_airport: str
    outbound_date: str
    return_date: str
    segments: list[dict]


@dataclass
class PricedTraveller:
    traveller: str
    construction: str
    source: str
    price: float
    currency: str
    price_usd: float
    airlines: list[str]
    stops_by_leg: list[int]
    checked_bag: str
    bag_adjustment_usd: float
    all_in_usd: float
    usable: bool
    reject_reason: str


@dataclass
class ComboResult:
    plan: TripPlan
    travellers: list[PricedTraveller]
    combined_usd: float
    combined_cad: float


def to_usd(amount: float, currency: str) -> float:
    currency = currency.upper()
    if currency == "USD":
        return amount
    if currency == "CAD":
        return amount / CAD_PER_USD
    if currency == "EUR":
        return amount / EUR_PER_USD
    return amount


def bag_note(option: FareOption) -> str:
    if option.checked_bag_included is True:
        return "included"
    if option.checked_bag_included is False:
        return "not included"
    return "unknown"


def bag_adjustment_usd(option: FareOption) -> float:
    if option.checked_bag_included is True:
        return 0.0
    if option.bag_fees:
        return to_usd(option.bag_fees, option.currency)
    if option.checked_bag_included is False:
        return EST_CHECKED_BAG_FEE_USD
    return EST_CHECKED_BAG_FEE_USD / 2  # unknown: half penalty estimate


def effective_usd(option: FareOption) -> float:
    return to_usd(option.total_price, option.currency) + bag_adjustment_usd(option)


def assess_candidates(options: list[FareOption]) -> list[FareOption]:
    assessed = [assess_option(option, max_stops=MAX_STOPS) for option in options]
    apply_layover_preference(assessed)
    return assessed


def combine_one_ways(outbound: FareOption, returning: FareOption) -> FareOption:
    currency = outbound.currency
    return FareOption(
        construction="two_one_ways",
        ticketing="two_tickets",
        price=outbound.price + returning.price,
        currency=currency,
        airlines=list(dict.fromkeys(outbound.airlines + returning.airlines)),
        stops=outbound.stops + returning.stops,
        duration_minutes=outbound.duration_minutes + returning.duration_minutes,
        legs=outbound.legs + returning.legs,
        stops_by_leg=(outbound.stops_by_leg or [outbound.stops])
        + (returning.stops_by_leg or [returning.stops]),
        layovers_by_leg=(outbound.layovers_by_leg or [[]]) + (returning.layovers_by_leg or [[]]),
        leg_departure_times=outbound.leg_departure_times + returning.leg_departure_times,
        leg_arrival_times=outbound.leg_arrival_times + returning.leg_arrival_times,
        carry_on_included=outbound.carry_on_included
        if outbound.carry_on_included is False or returning.carry_on_included is False
        else outbound.carry_on_included,
        checked_bag_included=outbound.checked_bag_included
        if outbound.checked_bag_included is False or returning.checked_bag_included is False
        else outbound.checked_bag_included,
        bag_fees=outbound.bag_fees + returning.bag_fees,
        source=f"{outbound.source}+{returning.source}",
    )


def build_plan(start_day: int, order: tuple[str, ...]) -> TripPlan:
    current = date(2026, 11, start_day)
    segments: list[dict] = []
    for code in order:
        name, days, airport = CITIES[code]
        arrive = current
        depart = current + timedelta(days=days - 1)
        segments.append(
            {
                "code": code,
                "name": name,
                "airport": airport,
                "arrive": arrive.isoformat(),
                "depart": depart.isoformat(),
                "days": days,
            }
        )
        current = depart + timedelta(days=1)
    return TripPlan(
        sequence="-".join(order),
        start_day=start_day,
        first_airport=segments[0]["airport"],
        last_airport=segments[-1]["airport"],
        outbound_date=segments[0]["arrive"],
        return_date=segments[-1]["depart"],
        segments=segments,
    )


def gather_options(
    serp: SerpApiProvider,
    duffel: DuffelProvider,
    origin: str,
    first_airport: str,
    outbound_date: str,
    last_airport: str,
    return_date: str,
    currency: str,
    gl: str,
) -> list[FareOption]:
    legs = [(origin, first_airport, outbound_date), (last_airport, origin, return_date)]
    candidates: list[FareOption] = []

    serp_mc, _, _ = serp.search_multi_city_options(legs, currency, 1, "max_1", gl)
    candidates.extend(serp_mc)

    duff_mc, _, _ = duffel.search_itinerary_options(legs, currency, 1, MAX_STOPS, "multi_city")
    candidates.extend(duff_mc)

    serp_out, _, _ = serp.search_one_way_options(
        origin, first_airport, outbound_date, currency, 1, "max_1", gl, "one_way_out"
    )
    serp_ret, _, _ = serp.search_one_way_options(
        last_airport, origin, return_date, currency, 1, "max_1", gl, "one_way_return"
    )
    for out_option, ret_option in itertools.product(serp_out, serp_ret):
        combined = combine_one_ways(out_option, ret_option)
        combined.source = f"{out_option.source}+{ret_option.source}"
        candidates.append(combined)

    duff_out, _, _ = duffel.search_itinerary_options(
        [(origin, first_airport, outbound_date)], currency, 1, MAX_STOPS, "one_way_out"
    )
    duff_ret, _, _ = duffel.search_itinerary_options(
        [(last_airport, origin, return_date)], currency, 1, MAX_STOPS, "one_way_return"
    )
    for out_option, ret_option in itertools.product(duff_out, duff_ret):
        combined = combine_one_ways(out_option, ret_option)
        combined.source = f"{out_option.source}+{ret_option.source}"
        candidates.append(combined)

    return candidates


def pick_best(options: list[FareOption]) -> FareOption | None:
    assessed = assess_candidates(options)
    usable = [option for option in assessed if option.usable]
    if not usable:
        return None
    strict = [option for option in usable if option.match_type == MatchType.STRICT]
    pool = strict or usable
    return min(pool, key=effective_usd)


def price_traveller(
    serp: SerpApiProvider,
    duffel: DuffelProvider,
    traveller_id: str,
    plan: TripPlan,
) -> PricedTraveller | None:
    cfg = TRAVELLERS[traveller_id]
    options = gather_options(
        serp,
        duffel,
        cfg["origin"],
        plan.first_airport,
        plan.outbound_date,
        plan.last_airport,
        plan.return_date,
        cfg["currency"],
        cfg["gl"],
    )
    best = pick_best(options)
    if not best:
        return None
    bag_adj = bag_adjustment_usd(best)
    price_usd = to_usd(best.total_price, best.currency)
    return PricedTraveller(
        traveller=traveller_id,
        construction=best.construction,
        source=best.source,
        price=best.total_price,
        currency=best.currency,
        price_usd=price_usd,
        airlines=best.airlines,
        stops_by_leg=best.stops_by_leg or [best.stops],
        checked_bag=bag_note(best),
        bag_adjustment_usd=bag_adj,
        all_in_usd=price_usd + bag_adj,
        usable=best.usable,
        reject_reason=best.reject_reason,
    )


def analyze() -> list[ComboResult]:
    serp = SerpApiProvider()
    duffel = DuffelProvider()
    if not serp.available and not duffel.available:
        raise RuntimeError("No API keys configured")

    results: list[ComboResult] = []
    orders = list(itertools.permutations(["HAG", "STO", "ATH"]))
    total = len(orders) * 7
    count = 0

    for start_day in range(2, 9):
        for order in orders:
            count += 1
            plan = build_plan(start_day, order)
            print(
                f"[{count}/{total}] {plan.sequence} start Nov {start_day} "
                f"({plan.first_airport}->{plan.last_airport})",
                file=sys.stderr,
                flush=True,
            )
            priced: list[PricedTraveller] = []
            for traveller_id in ("yyc", "atl"):
                row = price_traveller(serp, duffel, traveller_id, plan)
                if row:
                    priced.append(row)
            if len(priced) != 2:
                continue
            combined_usd = sum(row.all_in_usd for row in priced)
            results.append(
                ComboResult(
                    plan=plan,
                    travellers=priced,
                    combined_usd=combined_usd,
                    combined_cad=combined_usd * CAD_PER_USD,
                )
            )
    return sorted(results, key=lambda result: result.combined_usd)


def format_report(results: list[ComboResult]) -> str:
    if not results:
        return "Quote status: INCOMPLETE\nNo priced combinations found."

    best = results[0]
    lines = [
        "## Recommendation",
        "Quote status: COMPLETE",
        f"Best sequence: **{best.plan.sequence.replace('HAG', 'Den Haag').replace('STO', 'Stockholm').replace('ATH', 'Athens')}**",
        f"Start date: **{best.plan.outbound_date}** (first destination day)",
        f"Return date: **{best.plan.return_date}**",
        f"Total (all-in est., bags noted): **USD {best.combined_usd:.0f} / CAD {best.combined_cad:.0f}**",
        f"FX: 1 USD = {CAD_PER_USD} CAD; unknown/missing checked bags estimated up to USD {EST_CHECKED_BAG_FEE_USD}",
        "",
        "### Itinerary",
    ]
    for segment in best.plan.segments:
        lines.append(
            f"- {segment['name']} ({segment['airport']}): {segment['arrive']} to {segment['depart']} "
            f"({segment['days']} days)"
        )
    lines.append("")
    for row in best.travellers:
        origin = TRAVELLERS[row.traveller]["origin"]
        lines.append(
            f"- **{origin}**: {row.currency} {row.price:.0f} ({row.construction}, {row.source}; "
            f"{', '.join(row.airlines)}; stops {row.stops_by_leg}; checked bag {row.checked_bag}; "
            f"all-in est. USD {row.all_in_usd:.0f})"
        )
    if len(results) > 1:
        runner = results[1]
        lines.append(
            f"\nBeat next-best ({runner.plan.sequence}, start {runner.plan.outbound_date}) by "
            f"USD {runner.combined_usd - best.combined_usd:.0f}"
        )

    lines.extend(["", "## All sequences (cheapest start date per sequence)"])
    by_sequence: dict[str, ComboResult] = {}
    for result in results:
        key = result.plan.sequence
        if key not in by_sequence or result.combined_usd < by_sequence[key].combined_usd:
            by_sequence[key] = result

    for sequence in sorted(by_sequence):
        row = by_sequence[sequence]
        label = (
            sequence.replace("HAG", "Den Haag")
            .replace("STO", "Stockholm")
            .replace("ATH", "Athens")
        )
        lines.append(
            f"- {label}: start {row.plan.outbound_date}, home from {row.plan.last_airport} "
            f"USD {row.combined_usd:.0f} / CAD {row.combined_cad:.0f}"
        )

    lines.extend(["", "## Top 10 combinations"])
    for index, result in enumerate(results[:10], start=1):
        yyc = next(row for row in result.travellers if row.traveller == "yyc")
        atl = next(row for row in result.travellers if row.traveller == "atl")
        lines.append(
            f"{index}. {result.plan.sequence} · start {result.plan.outbound_date} · "
            f"USD {result.combined_usd:.0f} "
            f"(YYC {yyc.currency} {yyc.price:.0f} {yyc.construction}/{yyc.source}; "
            f"ATL {atl.currency} {atl.price:.0f} {atl.construction}/{atl.source})"
        )
    return "\n".join(lines)


def main() -> int:
    results = analyze()
    report = format_report(results)
    print(report)

    out = ROOT / "quotes" / "nov-2026-europe-analysis.json"
    out.parent.mkdir(exist_ok=True)
    payload = []
    for result in results:
        payload.append(
            {
                "sequence": result.plan.sequence,
                "start_day": result.plan.start_day,
                "outbound_date": result.plan.outbound_date,
                "return_date": result.plan.return_date,
                "first_airport": result.plan.first_airport,
                "last_airport": result.plan.last_airport,
                "combined_usd": round(result.combined_usd, 2),
                "combined_cad": round(result.combined_cad, 2),
                "segments": result.plan.segments,
                "travellers": [asdict(row) for row in result.travellers],
            }
        )
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nSaved raw results to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

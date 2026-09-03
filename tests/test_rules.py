import json
import unittest
from pathlib import Path
from unittest import mock

from scripts.lib.models import FareOption, FlightLeg, MatchType, SourceStatus
from scripts.lib.rules import (
    TimeWindow,
    apply_manual_omissions,
    apply_layover_preference,
    assess_option,
    local_minutes,
    parse_clock,
    rank_options,
)
from scripts.providers.duffel_provider import DuffelProvider
from scripts.providers.serpapi_provider import SerpApiProvider
from scripts.search import _combine_one_ways, load_config


def option(
    price: float,
    departure: str,
    airline: str = "WS",
    stops_by_leg: list[int] | None = None,
    bag_fees: float = 0,
) -> FareOption:
    return FareOption(
        construction="one_way",
        ticketing="two_tickets",
        price=price,
        currency="CAD",
        airlines=[airline],
        stops=sum(stops_by_leg or [0]),
        stops_by_leg=stops_by_leg or [0],
        bag_fees=bag_fees,
        legs=[
            FlightLeg(
                origin="YVR",
                destination="YYC",
                departure_time=departure,
                arrival_time="2026-09-13 08:15",
                airline=airline,
            )
        ],
    )


class RulesTests(unittest.TestCase):
    def test_round_trip_fixture_uses_shared_config_contract(self):
        config = load_config(str(Path(__file__).parent / "fixtures" / "round_trip.yml"))
        self.assertEqual(config["time_windows"]["return"]["earliest"], "07:00")
        self.assertEqual(config["near_match_minutes"], 30)
        self.assertEqual(config["manual_omissions"], ["Flair Airlines"])

    def test_clock_parsing_and_local_time(self):
        self.assertEqual(parse_clock("7:00 AM"), 420)
        self.assertEqual(parse_clock("24:00"), 1440)
        self.assertEqual(local_minutes("2026-09-13T06:45:00-06:00"), 405)

    def test_ws136_is_a_30_minute_near_match(self):
        ws136 = option(84, "2026-09-13 06:45")
        assessed = assess_option(
            ws136,
            [TimeWindow(earliest=420, latest=540, near_match_minutes=30)],
        )
        self.assertEqual(assessed.match_type, MatchType.NEAR_MATCH)
        self.assertEqual(assessed.near_match_violations, ["leg 1 departure is 15 min outside window"])
        self.assertTrue(assessed.usable)

    def test_near_match_can_win_when_materially_cheaper(self):
        strict = assess_option(
            option(427, "2026-09-13 07:30"),
            [TimeWindow(earliest=420, latest=540)],
        )
        near = assess_option(
            option(84, "2026-09-13 06:45"),
            [TimeWindow(earliest=420, latest=540)],
        )
        strict_winner, near_winner, _ = rank_options([strict, near], 25)
        self.assertIs(strict_winner, strict)
        self.assertIs(near_winner, near)

    def test_expected_two_one_way_total_is_preserved(self):
        outbound = FareOption(
            construction="one_way_out",
            ticketing="two_tickets",
            price=217,
            currency="CAD",
            airlines=["WS"],
            stops=1,
            stops_by_leg=[1],
            legs=[
                FlightLeg(
                    origin="YYC",
                    destination="YVR",
                    departure_time="2026-09-11 23:00",
                    arrival_time="2026-09-12 07:05",
                    airline="WS",
                )
            ],
        )
        returning = option(84, "2026-09-13 06:45")
        combined = _combine_one_ways(outbound, returning, "CAD")
        assessed = assess_option(
            combined,
            [
                TimeWindow(earliest=1080, latest=1440),
                TimeWindow(earliest=420, latest=540),
            ],
            max_stops=1,
        )
        self.assertEqual(combined.price, 301)
        self.assertEqual(assessed.match_type, MatchType.NEAR_MATCH)
        self.assertEqual(assessed.stops_by_leg, [1, 0])

    def test_manual_carrier_omission_is_not_a_provider_filter(self):
        flair = option(84, "2026-09-13 06:45", airline="F8")
        kept = option(217, "2026-09-13 23:00")
        options = apply_manual_omissions([flair, kept], ["Flair Airlines"])
        self.assertFalse(options[0].usable)
        self.assertEqual(options[0].reject_reason, "manual carrier omission: F8")
        self.assertTrue(options[1].usable)

    def test_stop_limit_is_per_leg(self):
        valid = FareOption(
            construction="round_trip",
            ticketing="one_ticket",
            price=100,
            currency="CAD",
            stops=2,
            stops_by_leg=[1, 1],
            legs=[
                FlightLeg("YYC", "YVR", "2026-09-11 18:00"),
                FlightLeg("YVR", "YYC", "2026-09-13 07:30"),
            ],
        )
        invalid = FareOption(
            construction="round_trip",
            ticketing="one_ticket",
            price=100,
            currency="CAD",
            stops=3,
            stops_by_leg=[2, 1],
            legs=valid.legs,
        )
        self.assertTrue(assess_option(valid, max_stops=1).usable)
        self.assertFalse(assess_option(invalid, max_stops=1).usable)

    def test_unknown_baggage_is_not_claimed_as_included(self):
        fare = option(100, "2026-09-13 07:30")
        self.assertIsNone(fare.carry_on_included)
        self.assertEqual(fare.total_price, 100)

    def test_known_bag_fee_is_added_to_all_in_price(self):
        fare = option(100, "2026-09-13 07:30", bag_fees=30)
        self.assertEqual(fare.total_price, 130)

    def test_long_layover_loses_when_shorter_option_is_similarly_priced(self):
        short = option(100, "2026-09-13 07:30")
        short.layovers_by_leg = [[]]
        long = option(110, "2026-09-13 07:30")
        long.layovers_by_leg = [[360]]
        assessed = apply_layover_preference([short, long])
        self.assertTrue(assessed[0].usable)
        self.assertFalse(assessed[1].usable)


class DuffelProviderTests(unittest.TestCase):
    def test_round_trip_offer_parses_into_fare_option(self):
        provider = DuffelProvider(api_key="fixture")
        fixture = Path(__file__).parent / "fixtures" / "duffel_yyc_yvr_rt.json"
        offers = json.loads(fixture.read_text(encoding="utf-8"))["data"]
        options = provider._options_from_offers(offers, "CAD", "round_trip", True)
        self.assertEqual(len(options), 1)
        option = options[0]
        self.assertEqual(option.price, 301.0)
        self.assertEqual(option.currency, "CAD")
        self.assertEqual(option.stops_by_leg, [0, 0])
        self.assertEqual(option.leg_departure_times, [
            "2026-09-11T19:00:00",
            "2026-09-13T07:30:00",
        ])
        self.assertTrue(option.carry_on_included)
        self.assertFalse(option.checked_bag_included)


class SerpApiCandidateTests(unittest.TestCase):
    def test_round_trip_resolves_departure_token_into_return_leg(self):
        provider = SerpApiProvider(api_key="fixture")
        outbound = {
            "best_flights": [{
                "price": 100,
                "departure_token": "outbound-token",
                "flights": [{
                    "departure_airport": {"id": "YYC", "time": "2026-09-11 19:00"},
                    "arrival_airport": {"id": "YVR", "time": "2026-09-11 20:30"},
                    "airline": "WS",
                    "flight_number": "WS1",
                    "duration": 90,
                }],
            }]
        }
        returning = {
            "best_flights": [{
                "price": 301,
                "flights": [{
                    "departure_airport": {"id": "YVR", "time": "2026-09-13 07:30"},
                    "arrival_airport": {"id": "YYC", "time": "2026-09-13 09:00"},
                    "airline": "WS",
                    "flight_number": "WS2",
                    "duration": 90,
                }],
            }]
        }
        with mock.patch.object(
            provider, "_get",
            side_effect=[(outbound, SourceStatus.OK, ""), (returning, SourceStatus.OK, "")],
        ):
            options, status, error = provider.search_round_trip_options(
                "YYC", "YVR", "2026-09-11", "2026-09-13", "CAD"
            )
        self.assertEqual(status, SourceStatus.OK)
        self.assertEqual(error, "")
        self.assertEqual(options[0].price, 301)
        self.assertEqual(options[0].leg_departure_times, [
            "2026-09-11 19:00", "2026-09-13 07:30"
        ])

    def test_provider_preserves_cheap_near_match_and_compliant_candidate(self):
        provider = SerpApiProvider(api_key="fixture")
        fixture = Path(__file__).parent / "fixtures" / "serpapi_yvr_yyc_return.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        options = provider._options_from_ow(data, "YVR", "YYC", "CAD", "one_way_return")
        self.assertEqual([candidate.price for candidate in options], [84, 427])
        self.assertEqual(options[0].legs[0].flight_number, "WS136")


if __name__ == "__main__":
    unittest.main()

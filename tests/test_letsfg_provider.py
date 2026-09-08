"""Unit tests for LetsFG offer parsing (no live network)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from scripts.lib.models import SourceStatus
from scripts.providers.letsfg_provider import LetsFGProvider, _is_split


FIXTURE = Path(__file__).parent / "fixtures" / "letsfg_fixture_offers.json"


class LetsFGParseTests(unittest.TestCase):
    def test_fixture_offers_parse(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        provider = LetsFGProvider(bearer_token="test")
        options = [
            provider._option_from_offer(
                offer,
                currency="EUR",
                construction="one_way_out",
                is_round_trip=False,
                search_meta=data,
            )
            for offer in data["offers"]
        ]
        options = [o for o in options if o is not None]
        self.assertGreaterEqual(len(options), 4)
        cheapest = min(options, key=lambda o: o.total_price)
        self.assertEqual(cheapest.price, 97)
        splits = [o for o in options if _is_split(o.raw)]
        self.assertTrue(splits)
        self.assertTrue(any(o.self_transfer for o in splits))

    def test_nested_round_trip_shape(self):
        offer = {
            "id": "off_rt",
            "price": 420,
            "currency": "CAD",
            "airlines": ["AC"],
            "source": "duffel",
            "outbound": {
                "stopovers": 0,
                "total_duration_seconds": 18000,
                "segments": [
                    {
                        "airline": "AC",
                        "flight_no": "AC30",
                        "origin": "YVR",
                        "destination": "HNL",
                        "departure": "2026-10-08T09:00:00",
                        "arrival": "2026-10-08T12:00:00",
                        "duration_seconds": 18000,
                    }
                ],
            },
            "inbound": {
                "stopovers": 0,
                "segments": [
                    {
                        "airline": "AC",
                        "flight_no": "AC31",
                        "origin": "HNL",
                        "destination": "YVR",
                        "departure": "2026-10-12T14:00:00",
                        "arrival": "2026-10-12T22:00:00",
                        "duration_seconds": 18000,
                    }
                ],
            },
        }
        provider = LetsFGProvider(bearer_token="test")
        option = provider._option_from_offer(
            offer,
            currency="CAD",
            construction="round_trip",
            is_round_trip=True,
            search_meta={"search_id": "ws_test"},
        )
        assert option is not None
        self.assertEqual(option.construction, "round_trip")
        self.assertEqual(option.stops_by_leg, [0, 0])
        self.assertEqual(option.leg_departure_times[0], "2026-10-08T09:00:00")
        self.assertEqual(option.source, "letsfg:duffel")

    def test_available_requires_auth(self):
        with mock.patch.object(LetsFGProvider, "_resolve_bearer", return_value=""):
            provider = LetsFGProvider(bearer_token="", api_key="")
            self.assertFalse(provider.available)
            options, status, err = provider.search_one_way_options("YVR", "YUL", "2026-10-08")
            self.assertEqual(status, SourceStatus.NO_KEY)
            self.assertEqual(options, [])


if __name__ == "__main__":
    unittest.main()

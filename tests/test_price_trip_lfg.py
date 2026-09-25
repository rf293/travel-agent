"""LetsFG-only trip pricing (no live network)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import letsfg_remote_auth
from scripts.lib.models import FareOption, FlightLeg, MatchType, SourceStatus
from scripts.lib.rules import TimeWindow, assess_option
from scripts.price_trip_lfg import PacedLetsFG, SearchPacer, price_trip
from scripts.providers.letsfg_provider import LetsFGProvider
from scripts.search import load_watch

OUT, RET = "2026-10-08", "2026-10-12"


def seg(flight: str, origin: str, destination: str, departure: str, arrival: str) -> dict:
    return {
        "airline": flight[:2],
        "flight_no": flight,
        "origin": origin,
        "destination": destination,
        "departure": departure,
        "arrival": arrival,
    }


def offer(price: float, currency: str, out_segs: list[dict], in_segs: list[dict] | None = None, carry_on: bool = True) -> dict:
    body = {
        "id": f"off_{price}_{out_segs[0]['flight_no']}",
        "price": price,
        "currency": currency,
        "airlines": sorted({s["airline"] for s in out_segs + (in_segs or [])}),
        "outbound": {"segments": out_segs},
        "ancillaries": {"cabin_bag": {"included": carry_on}},
    }
    if in_segs:
        body["inbound"] = {"segments": in_segs}
    return body


UA_OUT_DEN_OGG = seg("UA1234", "DEN", "OGG", f"{OUT}T19:05:00", f"{OUT}T22:30:00")
CPR_OUT = [seg("UA5000", "CPR", "DEN", f"{OUT}T15:00:00", f"{OUT}T16:00:00"), UA_OUT_DEN_OGG]
CPR_BACK = [
    seg("UA1236", "OGG", "DEN", f"{RET}T21:00:00", "2026-10-13T06:30:00"),
    seg("UA5003", "DEN", "CPR", "2026-10-13T07:45:00", "2026-10-13T08:50:00"),
]
WS_OUT = [seg("WS1800", "YVR", "OGG", f"{OUT}T15:00:00", f"{OUT}T18:45:00")]
WS_BACK = [seg("WS1801", "OGG", "YVR", f"{RET}T13:50:00", f"{RET}T22:30:00")]

RESPONSES = {
    ("YVR", "OGG", OUT, RET, 2): [
        offer(
            700,
            "CAD",
            [
                seg("AS100", "YVR", "SEA", f"{OUT}T14:10:00", f"{OUT}T15:15:00"),
                seg("AS845", "SEA", "OGG", f"{OUT}T16:30:00", f"{OUT}T20:00:00"),
            ],
            [
                seg("AS846", "OGG", "SEA", f"{RET}T13:00:00", f"{RET}T21:30:00"),
                seg("AS101", "SEA", "YVR", f"{RET}T22:40:00", f"{RET}T23:45:00"),
            ],
        ),
        offer(
            1100,
            "CAD",
            [seg("UA500", "YVR", "DEN", f"{OUT}T14:30:00", f"{OUT}T18:00:00"), UA_OUT_DEN_OGG],
            [
                seg("UA1235", "OGG", "DEN", f"{RET}T12:00:00", f"{RET}T21:30:00"),
                seg("UA501", "DEN", "YVR", f"{RET}T22:10:00", "2026-10-13T00:40:00"),
            ],
        ),
    ],
    ("YVR", "OGG", OUT, RET, 0): [
        offer(900, "CAD", WS_OUT, WS_BACK),
        offer(
            850,
            "CAD",
            [seg("AC1850", "YVR", "OGG", f"{OUT}T17:20:00", f"{OUT}T21:00:00")],
            [seg("AC1851", "OGG", "YVR", f"{RET}T21:50:00", "2026-10-13T06:40:00")],
        ),
        offer(
            880,
            "CAD",
            [seg("WS1802", "YVR", "OGG", f"{OUT}T13:45:00", f"{OUT}T17:30:00")],
            WS_BACK,
        ),
    ],
    ("YVR", "OGG", OUT, None, 2): [offer(480, "CAD", WS_OUT)],
    ("YVR", "OGG", OUT, None, 0): [offer(480, "CAD", WS_OUT)],
    ("OGG", "YVR", RET, None, 2): [offer(450, "CAD", WS_BACK)],
    ("OGG", "YVR", RET, None, 0): [offer(450, "CAD", WS_BACK)],
    ("CPR", "OGG", OUT, RET, 2): [
        offer(800, "USD", CPR_OUT, CPR_BACK),
        offer(600, "USD", CPR_OUT, CPR_BACK, carry_on=False),
    ],
    ("CPR", "OGG", OUT, None, 2): [offer(450, "USD", CPR_OUT)],
    ("OGG", "CPR", RET, None, 2): [offer(420, "USD", CPR_BACK)],
    ("YVR", "HNL", OUT, RET, 2): [
        offer(
            800,
            "CAD",
            [seg("AC1830", "YVR", "HNL", f"{OUT}T16:00:00", f"{OUT}T19:20:00")],
            [seg("AC1831", "HNL", "YVR", f"{RET}T14:00:00", f"{RET}T22:40:00")],
        )
    ],
    ("CPR", "HNL", OUT, RET, 2): [
        offer(
            900,
            "USD",
            [
                seg("UA5010", "CPR", "DEN", f"{OUT}T14:20:00", f"{OUT}T15:20:00"),
                seg("UA1500", "DEN", "HNL", f"{OUT}T16:30:00", f"{OUT}T20:10:00"),
            ],
            [
                seg("UA1501", "HNL", "DEN", f"{RET}T19:00:00", "2026-10-13T05:50:00"),
                seg("UA5011", "DEN", "CPR", "2026-10-13T07:30:00", "2026-10-13T08:35:00"),
            ],
        )
    ],
}
RESPONSES[("YVR", "HNL", OUT, RET, 0)] = RESPONSES[("YVR", "HNL", OUT, RET, 2)]
RESPONSES[("YVR", "HNL", OUT, None, 2)] = [offer(450, "CAD", [seg("AC1830", "YVR", "HNL", f"{OUT}T16:00:00", f"{OUT}T19:20:00")])]
RESPONSES[("YVR", "HNL", OUT, None, 0)] = RESPONSES[("YVR", "HNL", OUT, None, 2)]
RESPONSES[("HNL", "YVR", RET, None, 2)] = [offer(430, "CAD", [seg("AC1831", "HNL", "YVR", f"{RET}T14:00:00", f"{RET}T22:40:00")])]
RESPONSES[("HNL", "YVR", RET, None, 0)] = RESPONSES[("HNL", "YVR", RET, None, 2)]


class FakeLetsFG(LetsFGProvider):
    def __init__(self):
        super().__init__(bearer_token="test")
        self.calls: list[tuple] = []

    def _run_search(self, payload):
        key = (
            payload["origin"],
            payload["destination"],
            payload["date_from"],
            payload.get("return_date"),
            payload.get("max_stopovers"),
        )
        self.calls.append(key)
        return {"offers": RESPONSES.get(key, []), "search_id": "ws_fake"}, SourceStatus.OK, ""


class DatetimeWindowTests(unittest.TestCase):
    def _arrival(self, timestamp: str) -> FareOption:
        return FareOption(
            construction="one_way_return",
            ticketing="two_tickets",
            price=100,
            currency="CAD",
            leg_arrival_times=[timestamp],
            legs=[FlightLeg("OGG", "YVR", arrival_time=timestamp)],
        )

    def test_arrive_by_after_midnight(self):
        window = TimeWindow.from_config({"field": "arrival", "latest_at": "2026-10-13T01:00"})
        self.assertEqual(assess_option(self._arrival("2026-10-12T23:10:00"), [window]).match_type, MatchType.STRICT)
        near = assess_option(self._arrival("2026-10-13T01:20:00"), [window])
        self.assertEqual(near.match_type, MatchType.NEAR_MATCH)
        self.assertIn("20 min outside window", near.near_match_violations[0])
        self.assertEqual(assess_option(self._arrival("2026-10-13T06:40:00"), [window]).match_type, MatchType.REJECTED)


class PriceTripTests(unittest.TestCase):
    def setUp(self):
        self.provider = FakeLetsFG()
        self.report = price_trip(load_watch("maui-oahu"), self.provider, cad_per_usd=1.39)

    def test_searches_skip_nonstop_one_ways_when_no_nonstop_round_trip(self):
        self.assertEqual(len(self.provider.calls), 20)
        self.assertNotIn(("CPR", "OGG", OUT, None, 0), self.provider.calls)

    def test_recommendation_and_island_ranking(self):
        self.assertIn("Quote status: COMPLETE", self.report)
        self.assertIn("Meet Maui (OGG): US$1,304", self.report)
        self.assertIn("Beats Oahu (HNL) by US$172", self.report)

    def test_any_stops_and_nonstop_classes(self):
        self.assertIn("- Cheapest any stops: CA$700 (RT, one ticket) · AS", self.report)
        self.assertIn("- Cheapest nonstop: CA$900 (RT, one ticket) · WS", self.report)
        self.assertIn("RT CA$900 vs OW+OW CA$480 + CA$450 = CA$930 → RT cheaper by CA$30", self.report)
        self.assertIn("Near match (not chosen, saves CA$20)", self.report)
        self.assertIn("Skip: CA$850 round trip AC — leg 2 arrival is 340 min outside window", self.report)

    def test_cpr_has_no_nonstop_and_basic_fare_is_skipped(self):
        cpr_ogg = self.report.split("**Casper (CPR ⇄ OGG, USD)**")[1].split("**Combined (OGG)**")[0]
        self.assertIn("- Cheapest any stops: US$800 (RT, one ticket)", cpr_ogg)
        self.assertIn("- Cheapest nonstop: none usable", cpr_ogg)
        self.assertIn("Skip: US$600 round trip UA — carry-on not included", cpr_ogg)

    def test_shared_leg_is_flagged_not_chosen(self):
        self.assertIn(
            "Share UA1234 DEN→OGG Thu 8 Oct 19:05: YVR CA$1,100 + CPR US$800 = US$1,591 (+US$288 vs cheapest)",
            self.report,
        )


class PacingAndCacheTests(unittest.TestCase):
    def _fake_time(self):
        now = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        return sleep, (lambda: now[0]), sleeps

    def test_pacer_waits_for_window(self):
        sleep, clock, sleeps = self._fake_time()
        pacer = SearchPacer(2, 10, sleep=sleep, clock=clock)
        for _ in range(3):
            pacer.wait()
        self.assertEqual(sleeps, [10])

    def test_cache_reuses_response(self):
        data = {"offers": RESPONSES[("YVR", "OGG", OUT, None, 2)]}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            LetsFGProvider, "_run_search", return_value=(data, SourceStatus.OK, "")
        ) as live:
            provider = PacedLetsFG(SearchPacer(10, 600), cache_dir=Path(tmp), bearer_token="test")
            first = provider.search_one_way_options("YVR", "OGG", OUT, currency="CAD")
            second = provider.search_one_way_options("YVR", "OGG", OUT, currency="CAD")
        live.assert_called_once()
        self.assertEqual(first[0][0].price, second[0][0].price)
        self.assertEqual(provider.live_searches, 1)

    def test_rate_limit_retries_once_after_cooldown(self):
        sleep, clock, sleeps = self._fake_time()
        data = {"offers": RESPONSES[("YVR", "OGG", OUT, None, 2)]}
        with mock.patch.object(
            LetsFGProvider,
            "_run_search",
            side_effect=[(None, SourceStatus.BLOCKED, "HTTP 429: slow down"), (data, SourceStatus.OK, "")],
        ) as live:
            provider = PacedLetsFG(SearchPacer(10, 600, sleep=sleep, clock=clock), bearer_token="test")
            options, status, _ = provider.search_one_way_options("YVR", "OGG", OUT, currency="CAD")
        self.assertEqual(live.call_count, 2)
        self.assertEqual(status, SourceStatus.OK)
        self.assertEqual(sleeps, [600])
        self.assertTrue(options)


class RemoteAuthTests(unittest.TestCase):
    META = {
        "authorization_endpoint": "https://letsfg.co/connect",
        "token_endpoint": "https://letsfg.co/developers/api/oauth/token",
        "registration_endpoint": "https://letsfg.co/developers/api/oauth/register",
    }

    def test_start_then_finish_exchanges_saved_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "pending.json"
            with mock.patch.object(letsfg_remote_auth, "_discover", return_value=self.META), mock.patch.object(
                letsfg_remote_auth, "_post_json_abs", return_value=(201, {"client_id": "cid"})
            ):
                url = letsfg_remote_auth.start(state_path)
            pending = json.loads(state_path.read_text())
            self.assertIn("client_id=cid", url)

            redirect = f"{pending['redirect_uri']}?code=abc&state={pending['state']}"
            with mock.patch.object(
                letsfg_remote_auth,
                "_post_form",
                return_value=(200, {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600}),
            ) as post, mock.patch.object(letsfg_remote_auth, "save_token") as save:
                letsfg_remote_auth.finish(redirect, state_path)

        fields = post.call_args.args[1]
        self.assertEqual(fields["code"], "abc")
        self.assertEqual(fields["code_verifier"], pending["code_verifier"])
        self.assertEqual(fields["redirect_uri"], pending["redirect_uri"])
        self.assertEqual(save.call_args.kwargs, {"refresh_token": "ref", "client_id": "cid"})
        self.assertFalse(state_path.exists())

    def test_finish_rejects_state_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "pending.json"
            state_path.write_text(json.dumps({"state": "expected"}))
            with self.assertRaises(SystemExit):
                letsfg_remote_auth.finish("http://127.0.0.1:50000/callback?code=abc&state=other", state_path)


if __name__ == "__main__":
    unittest.main()

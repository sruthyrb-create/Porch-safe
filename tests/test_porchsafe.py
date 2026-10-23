import asyncio
import json
import os
import tempfile

os.environ["PORCHSAFE_DATA"] = tempfile.mkdtemp()
os.environ["PORCHSAFE_WEATHER"] = "fixture:winter"
os.environ["PORCHSAFE_TICK_S"] = "3600"
os.environ["RING_HMAC_KEY"] = "test-key"
os.environ.pop("RING_ACCESS_TOKEN", None)

import httpx
import pytest
from fastapi.testclient import TestClient

from porchsafe import app as appmod
from porchsafe import profiles
from porchsafe.monitor import Clock, Monitor
from porchsafe.report import render
from porchsafe.ring import RingClient, build_webhook, parse_webhook, sign, verify_signature
from porchsafe.risk import assess
from porchsafe.weather import WeatherClient


class FakeClock(Clock):
    def __init__(self):
        super().__init__(scale=1)
        self.t = 1_800_000_000.0

    def now(self):
        return self.t

    def advance(self, minutes):
        self.t += minutes * 60


def make_monitor(scenario="winter"):
    clock = FakeClock()
    mon = Monitor(data_dir=tempfile.mkdtemp(), ring=RingClient(access_token=None),
                  weather=WeatherClient(0, 0, mode=f"fixture:{scenario}"), clock=clock)
    mon.settings.escalate_after_min = 10
    return mon, clock


# ----------------------------------------------------------------------------- label logic


def test_insulin_below_freezing_is_act_now_immediately():
    a = assess(profiles.get("insulin"), 0, 24)
    assert a.status == "ACT_NOW" and "do not freeze" in a.reasons[0]


def test_epinephrine_excursion_runs_a_budget_then_escalates():
    p = profiles.get("epinephrine")
    a = assess(p, 10, 62)
    assert a.status == "WATCH" and a.minutes_until_act_now == 50
    assert assess(p, 61, 62).status == "ACT_NOW"
    assert assess(p, 0, 50).status == "ACT_NOW"  # below the 59°F excursion floor


def test_albuterol_burst_limit():
    assert assess(profiles.get("albuterol"), 0, 121).status == "ACT_NOW"


def test_food_two_hour_and_one_hour_rules():
    p = profiles.get("perishable_food")
    assert assess(p, 30, 75).status == "WATCH"
    assert assess(p, 125, 75).status == "EXCEEDED"
    assert assess(p, 65, 95).status == "EXCEEDED"  # >90°F: one hour


def test_forecast_warns_before_it_happens():
    a = assess(profiles.get("insulin"), 0, 40, forecast_f=[(45, 33), (90, 31)])
    assert a.status == "OK" and a.minutes_until_act_now == 45 and "forecast" in a.forecast_note.lower()


def test_direct_sun_halves_budget():
    assert assess(profiles.get("epinephrine"), 31, 62, direct_sun=True).status == "ACT_NOW"


def test_missing_weather_never_claims_ok():
    a = assess(profiles.get("insulin"), 5, None)
    assert a.status == "WATCH" and "unavailable" in a.headline


# ----------------------------------------------------------------------------- webhooks


def test_signature_roundtrip_and_tamper():
    body = json.dumps(build_webhook("motion_detected", "dev1", "package_delivery")).encode()
    sig = sign(body, "k")
    assert sig.startswith("sha256=") and verify_signature(body, sig, "k")
    assert not verify_signature(body + b" ", sig, "k")
    assert not verify_signature(body, sig, "other")


def test_parse_accepts_both_subtype_spellings():
    p = build_webhook("motion_detected", "d", "package_delivery")
    del p["data"]["attributes"]["sub_type"]
    assert parse_webhook(json.dumps(p).encode()).is_package_delivery
    p = build_webhook("motion_detected", "d", "package_delivery")
    del p["data"]["subType"]
    assert parse_webhook(json.dumps(p).encode()).is_package_delivery


# ----------------------------------------------------------------------------- engine


def test_end_to_end_insulin_in_winter_escalates_and_reports():
    async def run():
        mon, clock = make_monitor("winter")
        mon.expect("insulin", "Lantus pens (mail-order pharmacy)")
        ev = parse_webhook(json.dumps(build_webhook("motion_detected", "sim-door", "package_delivery")).encode())
        await mon.handle_event(ev)
        pkg = mon.packages[0]
        assert pkg.profile == "insulin" and pkg.status == "ACT_NOW"
        assert [n["to"] for n in pkg.notified] == ["household"]
        clock.advance(11)
        await mon.tick()
        assert [n["to"] for n in pkg.notified] == ["household", "backup"]  # nobody acted: neighbor asked
        clock.advance(25)
        await mon.tick()
        assert pkg.status == "EXCEEDED"
        mon.bring_inside(pkg.id)
        html = render(mon, pkg)
        assert "36 minutes" in html and "24°F" in html and "medlineplus.gov" in html
        # Ring retries the same webhook: idempotent
        assert (await mon.handle_event(ev)) == {"duplicate": True}
        assert len(mon.packages) == 1

    asyncio.run(run())


def test_unexpected_parcel_asks_instead_of_guessing():
    async def run():
        mon, _ = make_monitor("heat")
        await mon.package_arrived("sim-door", source="ring")
        pkg = mon.packages[0]
        assert pkg.profile is None and "Tell PorchSafe" in pkg.headline
        mon.set_profile(pkg.id, "perishable_food")
        await mon.evaluate(pkg)
        assert pkg.status == "WATCH" and "1 hour" in " ".join(pkg.reasons)

    asyncio.run(run())


def test_package_pickup_event_closes_exposure():
    async def run():
        mon, clock = make_monitor("mild")
        mon.expect("epinephrine", "EpiPen 2-pack")
        await mon.handle_event(parse_webhook(json.dumps(build_webhook("motion_detected", "d1", "package_delivery")).encode()))
        clock.advance(5)
        await mon.handle_event(parse_webhook(json.dumps(build_webhook("motion_detected", "d1", "package_pickup")).encode()))
        assert not mon.packages[0].on_porch

    asyncio.run(run())


# ----------------------------------------------------------------------------- HTTP surface


def test_webhook_endpoint_rejects_bad_signature_and_accepts_good():
    with TestClient(appmod.app) as client:
        body = json.dumps(build_webhook("motion_detected", "sim-front-door", "human")).encode()
        assert client.post("/webhooks/ring", content=body, headers={"X-Signature": "sha256=00"}).status_code == 401
        r = client.post("/webhooks/ring", content=body, headers={"X-Signature": sign(body, "test-key")})
        assert r.status_code == 200 and r.json()["ok"]


def test_ring_client_sends_required_headers_and_skips_auth_on_redirect():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/image/download"):
            return httpx.Response(303, headers={"location": "https://media.example/abc.jpg"})
        return httpx.Response(200, json={"data": [{"id": "dev1", "type": "devices", "attributes": {"name": "Front Door"}}]})

    async def run():
        rc = RingClient(access_token="tok", transport=httpx.MockTransport(handler))
        devices = await rc.devices()
        assert devices[0]["name"] == "Front Door"
        req = seen[0]
        assert req.headers["Authorization"] == "Bearer tok" and req.headers["User-Agent"].startswith("PorchSafe")
        assert req.headers["Hardware_Id"]
        await rc.aclose()

    asyncio.run(run())

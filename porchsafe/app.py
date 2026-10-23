"""PorchSafe web app: Ring webhook receiver, Ring token exchange, dashboard API, live stream, simulator.

    python -m porchsafe.app        ->  http://localhost:8090
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import report
from .monitor import Monitor
from .ring import OAUTH, USER_AGENT, RingAuthError, RingEvent, build_webhook, parse_webhook, sign, verify_signature

log = logging.getLogger("porchsafe.app")
STATIC = Path(__file__).parent / "static"
HMAC_KEY = os.getenv("RING_HMAC_KEY", "porchsafe-local-demo-key")  # set the real key from the Ring Developer Portal

monitor = Monitor()


async def _ring_poller(every_s: float) -> None:
    """Fallback for setups without webhooks (e.g. the Ring Developer Playground): poll Event History."""
    seen: set[str] = set()
    while True:
        try:
            for dev in await monitor.ring.devices():
                for e in await monitor.ring.history(dev["id"]):
                    eid, attrs = e.get("id", ""), e.get("attributes", {})
                    etype = str(attrs.get("event_type", ""))
                    if not eid or eid in seen:
                        continue
                    seen.add(eid)
                    if "package" in etype:
                        sub = etype.split(".", 1)[-1]
                        await monitor.handle_event(RingEvent(request_id=f"hist-{eid}", type="motion_detected",
                                                             sub_type=sub, device_id=dev["id"], timestamp_ms=0,
                                                             account_id="", raw=e))
        except RingAuthError as exc:
            log.warning("Ring poll stopped: %s", exc)
            return
        except Exception as exc:
            log.warning("Ring poll failed: %s", exc)
        await asyncio.sleep(every_s)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = [asyncio.create_task(monitor.run(every_s=float(os.getenv("PORCHSAFE_TICK_S", "5"))))]
    if monitor.ring.configured and os.getenv("PORCHSAFE_RING_POLL", "0") == "1":
        tasks.append(asyncio.create_task(_ring_poller(float(os.getenv("PORCHSAFE_RING_POLL_S", "30")))))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="PorchSafe", lifespan=lifespan)


# ----------------------------------------------------------------------------- Ring-facing endpoints


@app.post("/webhooks/ring")
async def ring_webhook(request: Request, background: BackgroundTasks) -> JSONResponse:
    body = await request.body()  # verify the raw bytes, never re-serialized JSON
    if not verify_signature(body, request.headers.get("X-Signature"), HMAC_KEY):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        ev = parse_webhook(body)
    except (ValueError, KeyError):
        raise HTTPException(status_code=400, detail="malformed payload")
    background.add_task(monitor.handle_event, ev)  # answer Ring within its 5-second budget
    return JSONResponse({"ok": True, "request_id": ev.request_id})


@app.post("/oauth/token")
async def ring_token_exchange(request: Request) -> JSONResponse:
    """Token Exchange URL for Ring one-way account linking: exchange the 60-second code, then store tokens."""
    form = await request.form()
    code = form.get("code")
    if not code or not monitor.ring.client_id or not monitor.ring.client_secret:
        raise HTTPException(status_code=400, detail="missing code or client credentials")
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.post(OAUTH, data={"grant_type": "authorization_code", "code": code,
                                         "client_id": monitor.ring.client_id, "client_secret": monitor.ring.client_secret},
                            headers={"User-Agent": USER_AGENT, "Hardware_Id": monitor.ring.hardware_id})
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Ring token exchange failed")
    tok = r.json()
    monitor.ring.access_token, monitor.ring.refresh_token = tok["access_token"], tok.get("refresh_token")
    monitor.ring._expires_at = time.time() + int(tok.get("expires_in", 14400))
    return JSONResponse({"ok": True})


# ----------------------------------------------------------------------------- dashboard API


class ExpectBody(BaseModel):
    profile: str
    description: str = ""


class ProfileBody(BaseModel):
    profile: str


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
async def state() -> dict:
    v = monitor.view()
    try:
        v["weather"] = (await monitor.weather.current()).to_dict()
    except Exception as exc:
        v["weather"] = {"error": f"{type(exc).__name__}"}
    return v


@app.post("/api/expected")
async def add_expected(body: ExpectBody) -> dict:
    try:
        return monitor.expect(body.profile, body.description).__dict__
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/expected/{expected_id}")
async def delete_expected(expected_id: str) -> dict:
    monitor.cancel_expected(expected_id)
    return {"ok": True}


@app.post("/api/packages/{package_id}/inside")
async def inside(package_id: str) -> dict:
    pkg = monitor.bring_inside(package_id, by="confirmed in PorchSafe")
    if pkg is None:
        raise HTTPException(status_code=404)
    return {"ok": True}


@app.post("/api/packages/{package_id}/profile")
async def set_profile(package_id: str, body: ProfileBody) -> dict:
    pkg = monitor.set_profile(package_id, body.profile)
    if pkg is None:
        raise HTTPException(status_code=404)
    await monitor.evaluate(pkg, announce=True)
    return {"ok": True}


@app.get("/report/{package_id}", response_class=HTMLResponse)
async def exposure_report(package_id: str) -> str:
    pkg = next((p for p in monitor.packages if p.id == package_id), None)
    if pkg is None:
        raise HTTPException(status_code=404)
    return report.render(monitor, pkg)


@app.get("/api/ring/devices")
async def ring_devices() -> dict:
    if not monitor.ring.configured:
        return {"configured": False, "devices": []}
    try:
        return {"configured": True, "devices": await monitor.ring.devices()}
    except Exception as exc:
        return {"configured": True, "devices": [], "error": f"{type(exc).__name__}: {exc}"}


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
    unsubscribe = monitor.subscribe(lambda m: queue.put_nowait(m) if not queue.full() else None)

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            unsubscribe()

    return StreamingResponse(gen(), media_type="text/event-stream")


# ----------------------------------------------------------------------------- simulator controls


class SimEvent(BaseModel):
    type: str = "motion_detected"
    sub_type: str = "package_delivery"
    device_id: str = "sim-front-door"


class SimWeather(BaseModel):
    temp_f: float | None = None


class SimSpeed(BaseModel):
    scale: float = 1


@app.post("/api/sim/event")
async def sim_event(body: SimEvent, request: Request) -> dict:
    """Send a signed Ring v1.1 webhook to our own /webhooks/ring - the same path real Ring events take."""
    payload = json.dumps(build_webhook(body.type, body.device_id, body.sub_type)).encode()
    url = str(request.base_url).rstrip("/") + "/webhooks/ring"
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.post(url, content=payload, headers={"Content-Type": "application/json",
                                                           "X-Signature": sign(payload, HMAC_KEY)})
    return {"status": r.status_code, "response": r.json()}


@app.post("/api/sim/weather")
async def sim_weather(body: SimWeather) -> dict:
    monitor.weather.override_f = body.temp_f
    await monitor.tick()
    return {"override_f": body.temp_f}


@app.post("/api/sim/speed")
async def sim_speed(body: SimSpeed) -> dict:
    monitor.clock.set_scale(body.scale)
    return {"scale": monitor.clock.scale}


@app.post("/api/sim/reset")
async def sim_reset() -> dict:
    monitor.reset()
    monitor.weather.override_f = None
    monitor.clock.set_scale(float(os.getenv("PORCHSAFE_TIME_SCALE", "1")))
    return {"ok": True}


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8090")))


if __name__ == "__main__":
    main()

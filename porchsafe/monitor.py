"""The PorchSafe engine: expected deliveries -> Ring sees the drop-off -> porch clock -> alerts -> report."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from . import profiles
from .ring import RingClient, RingEvent
from .risk import RANK, assess
from .weather import WeatherClient

log = logging.getLogger("porchsafe")


# ----------------------------------------------------------------------------- clock


class Clock:
    """Wall clock with an optional speed-up for demos (PORCHSAFE_TIME_SCALE=30 -> 1 s real = 30 s)."""

    def __init__(self, scale: float | None = None):
        self.scale = scale if scale is not None else float(os.getenv("PORCHSAFE_TIME_SCALE", "1"))
        self._t0_real = time.time()
        self._t0_virtual = self._t0_real

    def now(self) -> float:
        return self._t0_virtual + (time.time() - self._t0_real) * self.scale

    def set_scale(self, scale: float) -> None:
        self._t0_virtual, self._t0_real, self.scale = self.now(), time.time(), max(scale, 1.0)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------------- model


@dataclass
class Expected:
    id: str
    profile: str
    description: str  # e.g. "Lantus pens from the mail-order pharmacy"
    created_at: str
    arrived_package_id: str = ""


@dataclass
class Package:
    id: str
    profile: str | None
    description: str
    delivered_at: float
    device_id: str
    source: str  # "ring" | "manual"
    status: str = "OK"  # risk status while on the porch
    headline: str = ""
    reasons: list[str] = field(default_factory=list)
    forecast_note: str = ""
    minutes_until_act_now: int | None = None
    inside_at: float | None = None
    inside_by: str = ""
    act_now_since: float | None = None
    worst_status: str = "OK"
    samples: list[dict] = field(default_factory=list)  # {t, temp_f, status}
    notified: list[dict] = field(default_factory=list)  # {t, to, status, text}
    snapshot_file: str = ""
    vision_note: str = ""

    @property
    def on_porch(self) -> bool:
        return self.inside_at is None


@dataclass
class Settings:
    lat: float = float(os.getenv("PORCH_LAT", "42.3314"))  # Detroit, MI by default
    lon: float = float(os.getenv("PORCH_LON", "-83.0458"))
    direct_sun: bool = os.getenv("PORCH_DIRECT_SUN", "0") == "1"
    household_name: str = os.getenv("PORCHSAFE_HOUSEHOLD", "Home")
    notify_url: str = os.getenv("PORCHSAFE_NOTIFY_URL", "")  # e.g. https://ntfy.sh/<your-topic>
    backup_name: str = os.getenv("PORCHSAFE_BACKUP_NAME", "Neighbor")
    backup_notify_url: str = os.getenv("PORCHSAFE_BACKUP_NOTIFY_URL", "")
    escalate_after_min: int = int(os.getenv("PORCHSAFE_ESCALATE_AFTER_MIN", "10"))
    watched_device_ids: list[str] = field(default_factory=list)  # empty = all Ring devices


# ----------------------------------------------------------------------------- engine


class Monitor:
    def __init__(self, data_dir: str | os.PathLike[str] | None = None, ring: RingClient | None = None,
                 weather: WeatherClient | None = None, clock: Clock | None = None):
        self.dir = Path(data_dir or os.getenv("PORCHSAFE_DATA", Path.home() / ".porchsafe"))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settings = Settings()
        self.clock = clock or Clock()
        self.ring = ring or RingClient()
        self.weather = weather or WeatherClient(self.settings.lat, self.settings.lon)
        self.expected: list[Expected] = []
        self.packages: list[Package] = []
        self.events: list[dict] = []  # recent Ring events for the dashboard
        self._seen_requests: set[str] = set()
        self._listeners: list[Callable[[dict], None]] = []
        self._load()

    # -- persistence ------------------------------------------------------------
    def _path(self) -> Path:
        return self.dir / "state.json"

    def _load(self) -> None:
        if self._path().exists():
            d = json.loads(self._path().read_text(encoding="utf-8"))
            self.expected = [Expected(**e) for e in d.get("expected", [])]
            self.packages = [Package(**p) for p in d.get("packages", [])]

    def save(self) -> None:
        tmp = self._path().with_suffix(".tmp")
        tmp.write_text(json.dumps({"expected": [asdict(e) for e in self.expected],
                                   "packages": [asdict(p) for p in self.packages]}, indent=2), encoding="utf-8")
        tmp.replace(self._path())

    def reset(self) -> None:
        self.expected, self.packages, self.events = [], [], []
        self._seen_requests.clear()
        self.save()

    # -- pub/sub for the dashboard ------------------------------------------------
    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn)

    def _emit(self, kind: str, **data: Any) -> None:
        msg = {"kind": kind, "at": iso(self.clock.now()), **data}
        for fn in list(self._listeners):
            try:
                fn(msg)
            except Exception:
                pass

    # -- expected deliveries --------------------------------------------------------
    def expect(self, profile: str, description: str) -> Expected:
        profiles.get(profile)
        e = Expected(id=uuid.uuid4().hex[:8], profile=profile, description=description.strip() or profiles.get(profile).label,
                     created_at=iso(self.clock.now()))
        self.expected.append(e)
        self.save()
        self._emit("state")
        return e

    def cancel_expected(self, expected_id: str) -> None:
        self.expected = [e for e in self.expected if e.id != expected_id]
        self.save()
        self._emit("state")

    # -- Ring events ------------------------------------------------------------------
    async def handle_event(self, ev: RingEvent) -> dict:
        if ev.request_id in self._seen_requests:  # Ring retries; meta.request_id makes delivery idempotent
            return {"duplicate": True}
        self._seen_requests.add(ev.request_id)
        if self.settings.watched_device_ids and ev.device_id not in self.settings.watched_device_ids:
            return {"ignored": "device not watched"}
        self.events.insert(0, {"t": iso(self.clock.now()), "type": ev.type, "sub_type": ev.sub_type, "device": ev.device_id})
        self.events = self.events[:30]
        result: dict[str, Any] = {"type": ev.type, "sub_type": ev.sub_type}
        if ev.is_package_delivery:
            pkg = await self.package_arrived(ev.device_id, source="ring")
            result["package"] = pkg.id
        elif ev.is_package_pickup:
            for p in self.packages:
                if p.on_porch and p.device_id == ev.device_id:
                    self.bring_inside(p.id, by="Ring saw it picked up")
            result["picked_up"] = True
        self._emit("event", event=self.events[0])
        return result

    async def package_arrived(self, device_id: str, source: str = "manual") -> Package:
        now = self.clock.now()
        exp = next((e for e in self.expected if not e.arrived_package_id), None)
        pkg = Package(id=uuid.uuid4().hex[:8], profile=exp.profile if exp else None,
                      description=exp.description if exp else "Package (not on your expected list)",
                      delivered_at=now, device_id=device_id, source=source)
        if exp:
            exp.arrived_package_id = pkg.id
        self.packages.insert(0, pkg)
        self.save()
        asyncio.create_task(self._grab_snapshot(pkg))
        await self.evaluate(pkg, announce=True)
        return pkg

    async def _grab_snapshot(self, pkg: Package) -> None:
        if not self.ring.configured or not pkg.device_id or pkg.device_id.startswith("sim-"):
            return
        try:
            img = await self.ring.snapshot(pkg.device_id, start_ms=int(pkg.delivered_at * 1000) - 30_000)
        except Exception as exc:
            log.warning("snapshot failed: %s", exc)
            return
        if img:
            f = self.dir / f"snapshot_{pkg.id}.jpg"
            f.write_bytes(img)
            pkg.snapshot_file = f.name
            if pkg.profile is None and os.getenv("PORCHSAFE_BEDROCK") == "1":
                from .vision import classify_parcel

                pkg.vision_note = await asyncio.to_thread(classify_parcel, img)
            self.save()
            self._emit("state")

    # -- assessment loop ------------------------------------------------------------------
    async def evaluate(self, pkg: Package, announce: bool = False) -> None:
        if not pkg.on_porch:
            return
        now = self.clock.now()
        minutes = int((now - pkg.delivered_at) // 60)
        if pkg.profile is None:
            pkg.headline = f"A package was delivered {minutes} min ago. Tell PorchSafe if it is medicine or food."
            if announce:
                await self._notify(pkg, self.settings.notify_url, "household", pkg.headline)
            return
        prof = profiles.get(pkg.profile)
        try:
            w = await self.weather.current()
            temp, forecast, wsrc = w.temp_f, w.forecast, w.source
        except Exception as exc:
            log.warning("weather unavailable: %s", exc)
            temp, forecast, wsrc = None, [], "unavailable"
        act_min = int((now - pkg.act_now_since) // 60) if pkg.act_now_since else 0
        a = assess(prof, minutes, temp, forecast, minutes_in_act_now=act_min, direct_sun=self.settings.direct_sun)
        if a.status in ("ACT_NOW", "EXCEEDED") and pkg.act_now_since is None:
            pkg.act_now_since = now
        prev = pkg.status
        pkg.status, pkg.headline, pkg.reasons = a.status, a.headline, a.reasons
        pkg.forecast_note, pkg.minutes_until_act_now = a.forecast_note, a.minutes_until_act_now
        if RANK[a.status] > RANK[pkg.worst_status]:
            pkg.worst_status = a.status
        if not pkg.samples or now - pkg.samples[-1]["t"] >= 60 or a.status != prev:
            pkg.samples.append({"t": now, "temp_f": temp, "status": a.status, "src": wsrc})
        if announce or RANK[a.status] > RANK[prev]:
            await self._notify(pkg, self.settings.notify_url, "household", a.headline)
        # escalate to the backup contact if nobody has brought it in
        if a.status in ("ACT_NOW", "EXCEEDED") and pkg.act_now_since is not None:
            waited = (now - pkg.act_now_since) / 60
            if waited >= self.settings.escalate_after_min and not any(n["to"] == "backup" for n in pkg.notified):
                text = (f"{self.settings.household_name}: the {prof.short} delivery has been on the porch "
                        f"{minutes} min and needs to come inside ({a.headline.split(': ', 1)[-1].rstrip('.')}). Can you help?")
                await self._notify(pkg, self.settings.backup_notify_url, "backup", text)
        self.save()
        self._emit("state")

    async def tick(self) -> None:
        for p in [p for p in self.packages if p.on_porch]:
            await self.evaluate(p)

    async def run(self, every_s: float = 10) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(every_s)

    # -- people actions -------------------------------------------------------------------
    def bring_inside(self, package_id: str, by: str = "someone at home") -> Package | None:
        pkg = next((p for p in self.packages if p.id == package_id), None)
        if pkg is None or not pkg.on_porch:
            return pkg
        pkg.inside_at, pkg.inside_by = self.clock.now(), by
        pkg.headline = f"Brought inside after {int((pkg.inside_at - pkg.delivered_at) // 60)} min on the porch."
        self.save()
        self._emit("state")
        return pkg

    def set_profile(self, package_id: str, profile: str) -> Package | None:
        profiles.get(profile)
        pkg = next((p for p in self.packages if p.id == package_id), None)
        if pkg:
            pkg.profile, pkg.description = profile, profiles.get(profile).label
            self.save()
        return pkg

    async def _notify(self, pkg: Package, url: str, to: str, text: str) -> None:
        pkg.notified.append({"t": self.clock.now(), "to": to, "status": pkg.status, "text": text})
        self._emit("alert", to=to, package=pkg.id, status=pkg.status, text=text)
        if not url:
            return
        try:
            async with httpx.AsyncClient(timeout=8) as http:
                if "ntfy" in url:  # ntfy.sh topic -> phone push notification
                    await http.post(url, content=text.encode(), headers={
                        "Title": f"PorchSafe: {pkg.status.replace('_', ' ').title()}",
                        "Priority": "urgent" if pkg.status in ("ACT_NOW", "EXCEEDED") else "default",
                        "Tags": "package,thermometer"})
                else:
                    await http.post(url, json={"to": to, "status": pkg.status, "text": text, "package": pkg.id})
        except httpx.HTTPError as exc:
            log.warning("notify %s failed: %s", to, exc)

    # -- views --------------------------------------------------------------------------------
    def view(self) -> dict:
        now = self.clock.now()

        def pkg_view(p: Package) -> dict:
            d = asdict(p)
            end = p.inside_at or now
            d["minutes_on_porch"] = int((end - p.delivered_at) // 60)
            d["delivered_at_iso"] = iso(p.delivered_at)
            d["inside_at_iso"] = iso(p.inside_at) if p.inside_at else None
            d["profile_info"] = profiles.get(p.profile).public() if p.profile else None
            temps = [s["temp_f"] for s in p.samples if s["temp_f"] is not None]
            d["temp_range"] = [min(temps), max(temps)] if temps else None
            d["has_report"] = p.profile is not None and (p.inside_at is not None or p.worst_status != "OK")
            return d

        return {
            "now": iso(now),
            "time_scale": self.clock.scale,
            "settings": asdict(self.settings),
            "expected": [asdict(e) for e in self.expected if not e.arrived_package_id],
            "packages": [pkg_view(p) for p in self.packages[:20]],
            "events": self.events[:12],
            "profiles": {k: {"label": v.label} for k, v in profiles.PROFILES.items()},
            "ring_configured": self.ring.configured,
            "weather_mode": self.weather.mode,
        }

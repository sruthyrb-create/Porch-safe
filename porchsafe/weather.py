"""National Weather Service (api.weather.gov) client: current porch temperature + hourly forecast.

Free and keyless; NWS asks for a descriptive User-Agent. Observations come from the nearest
NWS station to the configured porch coordinates.

For demos and tests, ``PORCHSAFE_WEATHER=fixture:<name>`` serves a demo scenario from
``fixtures/weather_<name>.json`` (and the simulator can override the temperature live).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

NWS = "https://api.weather.gov"
UA = os.getenv("PORCHSAFE_UA", "PorchSafe/1.0 (porch temperature monitor; contact: see README)")
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@dataclass
class Weather:
    temp_f: float | None
    observed_at: str
    station: str
    forecast: list[tuple[int, float]] = field(default_factory=list)  # (minutes from now, °F)
    source: str = "NWS"

    def to_dict(self) -> dict:
        return {"temp_f": self.temp_f, "observed_at": self.observed_at, "station": self.station,
                "forecast": [{"in_min": m, "temp_f": t} for m, t in self.forecast], "source": self.source}


def c_to_f(c: float | None) -> float | None:
    return None if c is None else round(c * 9 / 5 + 32, 1)


class WeatherClient:
    def __init__(self, lat: float, lon: float, mode: str | None = None, ttl: float = 300):
        self.lat, self.lon = lat, lon
        self.mode = mode if mode is not None else os.getenv("PORCHSAFE_WEATHER", "live")
        self.ttl = ttl
        self._cached: tuple[float, Weather] | None = None
        self._points: dict | None = None
        self.override_f: float | None = None  # simulator "what if it were 28°F" control

    async def current(self) -> Weather:
        if self.override_f is not None:
            base = await self._fetch_cached()
            return Weather(self.override_f, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           base.station, [], source="simulator override")
        return await self._fetch_cached()

    async def _fetch_cached(self) -> Weather:
        if self._cached and time.monotonic() - self._cached[0] < self.ttl:
            return self._cached[1]
        w = self._fixture() if self.mode.startswith("fixture:") else await self._live()
        self._cached = (time.monotonic(), w)
        return w

    def _fixture(self) -> Weather:
        name = self.mode.split(":", 1)[1]
        d = json.loads((FIXTURES / f"weather_{name}.json").read_text(encoding="utf-8"))
        return Weather(d["temp_f"], d.get("observed_at", ""), d.get("station", "fixture"),
                       [(f["in_min"], f["temp_f"]) for f in d.get("forecast", [])], source=f"demo scenario '{name}'")

    async def _live(self) -> Weather:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": UA, "Accept": "application/geo+json"}) as http:
            if self._points is None:
                r = await http.get(f"{NWS}/points/{self.lat:.4f},{self.lon:.4f}")
                r.raise_for_status()
                props = r.json()["properties"]
                st = await http.get(props["observationStations"])
                st.raise_for_status()
                station = st.json()["features"][0]["properties"]["stationIdentifier"]
                self._points = {"hourly": props["forecastHourly"], "station": station}
            obs = await http.get(f"{NWS}/stations/{self._points['station']}/observations/latest")
            obs.raise_for_status()
            o = obs.json()["properties"]
            temp_f = c_to_f((o.get("temperature") or {}).get("value"))
            fc = await http.get(self._points["hourly"])
            forecast: list[tuple[int, float]] = []
            if fc.status_code == 200:
                now = datetime.now(timezone.utc)
                for period in fc.json()["properties"]["periods"][:8]:
                    start = datetime.fromisoformat(period["startTime"])
                    mins = int((start - now).total_seconds() // 60)
                    t = float(period["temperature"])
                    if period.get("temperatureUnit") == "C":
                        t = c_to_f(t) or t
                    if mins > 0:
                        forecast.append((mins, t))
            return Weather(temp_f, o.get("timestamp", ""), self._points["station"], forecast)

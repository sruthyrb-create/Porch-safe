"""Check live connections before a demo:  python -m porchsafe.doctor

* National Weather Service: current temperature + forecast for PORCH_LAT/PORCH_LON
* Ring Partner API: lists your devices if RING_ACCESS_TOKEN (Developer Playground) or a refresh token is set
* Amazon Bedrock: optional, if PORCHSAFE_BEDROCK=1
"""

from __future__ import annotations

import asyncio
import os

from .monitor import Settings
from .ring import RingClient
from .weather import WeatherClient


async def run() -> None:
    s = Settings()
    print(f"Porch location: {s.lat}, {s.lon}")
    try:
        w = await WeatherClient(s.lat, s.lon, mode="live").current()
        print(f"[ok] NWS station {w.station}: {w.temp_f}°F (observed {w.observed_at}); "
              f"next hours: {[round(t) for _, t in w.forecast[:4]]}")
    except Exception as exc:
        print(f"[fail] National Weather Service: {type(exc).__name__}: {exc}")

    ring = RingClient()
    if not ring.configured:
        print("[skip] Ring: set RING_ACCESS_TOKEN (Developer Playground) or RING_REFRESH_TOKEN + RING_CLIENT_ID/SECRET")
    else:
        try:
            devices = await ring.devices()
            print(f"[ok] Ring: {len(devices)} device(s): " + ", ".join(f"{d['name']} ({d['id']})" for d in devices))
            if devices:
                hist = await ring.history(devices[0]["id"])
                print(f"[ok] Ring Event History for {devices[0]['name']}: {len(hist)} event(s)")
        except Exception as exc:
            print(f"[fail] Ring: {type(exc).__name__}: {exc}")
        finally:
            await ring.aclose()

    if os.getenv("PORCHSAFE_BEDROCK") == "1":
        try:
            import boto3

            boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
            print("[ok] Bedrock client created (model access is checked on first image)")
        except Exception as exc:
            print(f"[fail] Bedrock: {exc}")


if __name__ == "__main__":
    asyncio.run(run())

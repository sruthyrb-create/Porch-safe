"""Ring event simulator: send signed Ring webhook v1.1 events to a PorchSafe server.

    python -m porchsafe.simulate delivery                # package_delivery motion event
    python -m porchsafe.simulate pickup
    python -m porchsafe.simulate human --url https://your-host/webhooks/ring --key "$RING_HMAC_KEY"

Payloads follow Ring's documented v1.1 shape and are signed exactly like Ring signs them
(X-Signature: sha256=<hex HMAC-SHA256 of the raw body>), so the server's real verification
path is exercised. The Ring Developer Playground does not deliver webhooks, which is why this exists.
"""

from __future__ import annotations

import argparse
import json
import os

import httpx

from .ring import build_webhook, sign

KINDS = {
    "delivery": ("motion_detected", "package_delivery"),
    "pickup": ("motion_detected", "package_pickup"),
    "human": ("motion_detected", "human"),
    "ding": ("button_press", ""),
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=KINDS)
    ap.add_argument("--url", default=os.getenv("PORCHSAFE_WEBHOOK_URL", "http://localhost:8090/webhooks/ring"))
    ap.add_argument("--key", default=os.getenv("RING_HMAC_KEY", "porchsafe-local-demo-key"))
    ap.add_argument("--device", default="sim-front-door")
    args = ap.parse_args()
    etype, sub = KINDS[args.kind]
    body = json.dumps(build_webhook(etype, args.device, sub)).encode()
    r = httpx.post(args.url, content=body, headers={"Content-Type": "application/json", "X-Signature": sign(body, args.key)})
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()

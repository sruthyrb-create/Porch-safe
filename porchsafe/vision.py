"""Optional: Amazon Bedrock (Converse API, image input) looks at the Ring snapshot of an
*unexpected* parcel and says whether it looks like a pharmacy or cold-chain shipment
(insulated cooler, "Rx", "Keep refrigerated" / "Perishable" labels). PorchSafe only uses this to
*ask* the household ("this looks like a pharmacy package, is it medicine?"); it never sets a
profile on its own. Enabled with PORCHSAFE_BEDROCK=1.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)
MODEL_ID = os.getenv("BEDROCK_VISION_MODEL_ID", "us.amazon.nova-lite-v1:0")

PROMPT = """This is a doorbell camera image taken when a parcel was delivered.
Answer ONLY with JSON: {"parcel_visible": bool, "looks_like": "pharmacy" | "cold_chain" | "groceries" | "ordinary" | "unknown",
"evidence": "<short phrase naming what you can actually see, e.g. 'white bag with Rx label'>"}.
Do not describe people. If unsure, use "unknown"."""


def classify_parcel(jpeg: bytes) -> str:
    try:
        import boto3

        client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
        resp = client.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [
                {"image": {"format": "jpeg", "source": {"bytes": jpeg}}},
                {"text": PROMPT}]}],
            inferenceConfig={"maxTokens": 120, "temperature": 0},
        )
        text = resp["output"]["message"]["content"][0]["text"]
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception as exc:
        log.warning("Bedrock vision unavailable: %s", exc)
        return ""
    kind = data.get("looks_like", "unknown")
    if not data.get("parcel_visible") or kind in ("ordinary", "unknown"):
        return ""
    names = {"pharmacy": "a pharmacy package", "cold_chain": "an insulated / keep-cold shipment", "groceries": "groceries"}
    return f"This looks like {names.get(kind, kind)} ({data.get('evidence', '')}). Is it temperature-sensitive?"

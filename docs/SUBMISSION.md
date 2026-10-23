# Devpost submission kit: PorchSafe

Copy each section into the matching Devpost field. Edit anything that doesn't match your own experience; the friction-log entries describe problems actually hit while building this repo.

---

## Tracks

- **Primary track:** Ring
- **Mini challenge:** AWS Builder (Amazon Bedrock)

## Project description

**PorchSafe: your Ring doorbell sees the insulin arrive, and PorchSafe makes sure it doesn't freeze, or cook, on the porch.**

Millions of Americans get prescriptions by mail. A University of Utah study put temperature loggers in 48 mailed packages, and every one went outside the recommended range in transit. The *New York Times* has reported insulin freezing during a delivery and a transplant drug arriving after a ride in 102°F heat. FDA storage rules stop at the pharmacy door.

The one part of the trip a family controls is how long the package sits outside, and a Ring doorbell already sees the moment it lands. PorchSafe connects that moment to two other facts: the temperature on the porch, and what the medicine's label allows.

**How it works**
1. The family adds an expected delivery: insulin, an EpiPen, an inhaler, a refrigerated biologic, or groceries. Each type maps to its labeled storage range and permitted excursions, with the source cited (MedlinePlus, prescribing information, USDA).
2. A signed **Ring webhook** (`motion_detected`, package sub-type) starts a porch clock.
3. PorchSafe reads the live **National Weather Service** observation and hourly forecast for the address and grades the porch: within range, bring inside soon (an excursion with a time budget), bring inside now, or exposed beyond the label. It warns ahead of time when the forecast is about to cross a limit.
4. Alerts escalate: first a phone push to the household, then a backup neighbor if nobody responds.
5. An exposure report records minutes on the porch, temperatures, the label, and the Ring snapshot, so the patient can ask the pharmacy for a replacement with evidence.
6. For parcels nobody logged, **Amazon Bedrock (Nova, image input)** looks at the Ring snapshot and flags a likely pharmacy or keep-cold shipment. It asks the family rather than guessing.

**Trust by design:** every threshold cites its source. The app never says a medicine is "fine" or "ruined." It says when the label was exceeded and to ask a pharmacist. Missing weather data means "bring it in," never "OK."

**Ring APIs used in code:** signed webhooks v1.1 (HMAC-SHA256 checked on the raw body, de-duplicated by `request_id`, answered within 5 s), device discovery, Event History (a polling fallback for the Developer Playground), image snapshots (the 303 pre-signed redirect is fetched without the bearer token), OAuth token exchange and refresh, and the required `User-Agent` and `Hardware_Id` headers. A built-in simulator sends Ring-format webhooks with valid signatures through the same verification path.

## How we built it

Python, FastAPI and httpx. A risk engine (`risk.py`) applies the label ranges and time budgets. A monitor (`monitor.py`) runs the porch clocks, escalation and reports. The dashboard is a single page updated over Server-Sent Events. NWS (`api.weather.gov`) supplies observations and the hourly forecast. ntfy.sh sends phone pushes, and Amazon Bedrock's Converse API handles vision. There are 14 tests covering the label rules, signature verification and tampering, sub-type parsing, end-to-end escalation and reporting, idempotency, and the Ring client's headers and redirect handling.

## AWS Builder: services and how

- **Amazon Bedrock Runtime, Converse API with image content** (`porchsafe/vision.py`, model `us.amazon.nova-lite-v1:0`): classifies the Ring snapshot of an unexpected parcel and returns the visible evidence as JSON. It is used only to prompt the household and never to set a temperature profile. The prompt forbids describing people.
- **Container** (`Dockerfile`) to deploy on AWS App Runner or ECS, which provide the public HTTPS endpoint Ring webhooks require.

## Product feedback

**Ring Partner API.** *Used for* webhooks, device discovery, Event History, snapshots and OAuth. *Worked well:* the HMAC webhook signing is simple and well explained, and the "why one-way account linking is designed this way" section is unusually clear. *Needs work:* the snapshot request body isn't in the main API reference (we found `latest_in_range` / `start_timestamp` in a community forum thread). Motion sub-type values aren't enumerated, so it's unclear which strings mean "package." The sample payload uses `data.subType`, while other material uses `attributes.sub_type`. *Onboarding:* the Developer Playground is great for exploring, but its 30-minute tokens and lack of webhook delivery mean event-driven apps need their own simulator. *Would build again:* yes.

**Ring sensors (early access).** We wanted Ring's indoor temperature sensor for enclosed porches, but no payload or field documentation was available, so PorchSafe uses outdoor air temperature as a stand-in.

**Amazon Bedrock.** *Used for* image classification with Nova Lite via the Converse API. *Worked well:* one API shape for text and images. *Needs work:* model access has to be turned on per region before the first call, which is easy to miss.

**National Weather Service API.** Keyless and reliable. It does require a descriptive User-Agent, and it takes two lookups to get from coordinates to an observation station.

## Friction log (up to a 10% judging bonus)

| Task | Steps | Expected | Actual | Severity | Workaround | Suggestion |
|---|---|---|---|---|---|---|
| Download a delivery snapshot | Read the API reference's Image Snapshots entry | A request body schema | The table only says "pass a `components` array"; no body fields or response flow | High | Found the body (`type: latest_in_range`, `start_timestamp`) in a community forum thread; found the 303 behavior in a GitHub issue | Put a full request/response example for `/media/image/download` in the reference, including the 303 and `403 TIME_RANGE_NOT_AUTHORIZED` cases |
| Detect package deliveries | Searched the docs for motion sub-type values | A list of `sub_type` values | Only `human` given as an example | Medium | Matched `package_delivery` / `package` / `package_pickup` defensively | Enumerate the sub-types (and Event History `event_type` values) |
| Parse the webhook | Followed the sample payload | One field name for the sub-type | The sample uses `data.subType`; other references use `attributes.sub_type` | Medium | Accept both | Use one spelling in the docs, or document both |
| Test webhooks end to end | Used the Developer Playground | Simulated events reach my webhook URL | The Playground doesn't deliver webhooks | Medium | Wrote a signed-webhook simulator | Add "send test webhook" to the Playground or the Developer Portal |
| Read sensor data | Looked for temperature sensor fields | A payload example | Sensors are listed but have no field-level docs (early access) | Low | Used NWS outdoor temperature | Publish sensor attribute names and units |
| Read the docs with AI tooling | Fetched the single-page API reference | The full reference | The page is about 120k characters, and tools truncate it before the Sensors, Event History and Media sections | Low | Read sections through other sources | Split the reference into per-section pages, or make the Ring docs MCP server easier to authorize |

## Feature requests

- **Webhook test sender in the Developer Portal**: critical for event-driven apps.
- **Package-removed (pickup) event** as a documented sub-type, so apps can stop timers automatically: important.
- **Sensor readings (temperature, humidity) via webhooks**, so an enclosed-porch or drop-box sensor can measure the package directly: important.

## Demo video script (under 3 minutes)

1. **0:00–0:20, the problem.** "Every one of 48 test packages went outside its safe temperature range in the mail. The last part of the trip is your porch, and your Ring doorbell can see it." Show the headline stats on screen.
2. **0:20–0:40, setup.** Add an expected delivery: Insulin, "Lantus pens." Point out the cited label range.
3. **0:40–1:20, a Ring delivery.** Click *Package dropped off*. The signed webhook arrives, the porch clock starts, and the label band shows the live NWS temperature. Set 24°F: **Bring inside now**. Show the phone push (ntfy).
4. **1:20–1:50, escalation.** Clock at 120×. Nobody responds, so the neighbor is asked. Click *I brought it inside*.
5. **1:50–2:20, the report.** Open the exposure report: delivery time from Ring, minutes outside, temperature log, label source. "Send this to the pharmacy and ask for a replacement."
6. **2:20–2:45, under the hood.** Terminal: `pytest` passing, `python -m porchsafe.simulate delivery` with a wrong key → 401. Mention Bedrock vision for parcels nobody logged.
7. **2:45–3:00, close.** "PorchSafe: the last 50 feet of the cold chain."

# IPAC router — a routing layer between Vobiz and Sarvam

A control-plane service that sits on the Vobiz answer URL and decides, per
call, whether the caller goes to a Sarvam AI agent, to a human agent, into a
hold queue, or is rejected with their number captured.

```
caller → Vobiz ──answer_url──> IPAC ──+── <Stream> → Sarvam   (media: Vobiz ↔ Sarvam)
                                      +── <Dial>   → human agent
                                      +── hold, re-decide when a channel frees
                                      +── reject unanswered, capture the number
```

**Media never passes through this service.** Audio still flows directly
between Vobiz and Sarvam. This adds one HTTP round trip before the call is
answered — no added latency in the conversation itself.

Wiring it in is one field: point the Answer URL of the Vobiz application at
`{PUBLIC_URL}/answer` instead of at Sarvam.

---

## Run it

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env          # edit: PUBLIC_URL, AGENT_NUMBERS, CALLER_ID
./run.sh                      # listens on :8090
open http://127.0.0.1:8090/   # live console
```

`run.sh` reclaims the port before starting. Without that, a second
`python app.py` fails to bind, exits, and leaves the **old** process serving —
so your changes appear to do nothing.

For a public URL:

```bash
cloudflared tunnel --url http://localhost:8090
# put the https URL in .env as PUBLIC_URL, then restart
```

---

## Test it, in this order

**1. Prove the logic with no telephony at all.** Every branch is reachable
over HTTP, so the thresholds and the repeat-caller logic can be verified in
seconds rather than by dialling a hundred times.

```bash
./.venv/bin/python verify.py          # 11 assertions across every branch
./.venv/bin/python sim.py all         # the same paths, narrated
```

Useful individually:

```bash
python sim.py identity                # can the router tell who is calling?
python sim.py fill --calls 95         # where each threshold trips
python sim.py repeat                  # first-time vs repeat routing
python sim.py repeat --no-diversion   # the same, with a carrier that strips Diversion
python sim.py queue                   # a held caller promoted when a slot frees
python sim.py escalate-dry            # AI → human mid-call
```

Set small caps in `.env` while testing (`AI_CAPACITY=5`, `HUMAN_CAPACITY=3`,
`QUEUE_CAPACITY=2`) so the cascade is readable.

**2. Prove the XML on one real call.**

```bash
python call.py --to +9198XXXXXXXX
python call.py --to +9198XXXXXXXX --forwarded-from 919812345678   # fake a forward
```

**3. Prove it inbound.** Set the Answer URL of the Vobiz application to
`{PUBLIC_URL}/answer` and the Hangup URL to `{PUBLIC_URL}/hangup`, then dial
the DID. Watch the console.

---

## Policy

The two source documents describe **different** policies, so it is a switch
rather than an assumption:

| `ROUTE_POLICY` | Behaviour | Comes from |
| --- | --- | --- |
| `ai_first` | try AI, overflow to human | the email thread ("70% reserved for AI") |
| `human_first` | try human, overflow to AI | the capacity flowchart (human first preference) |

Capacities are explicit. For the thread's "70% of channels reserved for AI",
with 100 total channels that is `AI_CAPACITY=70`, `HUMAN_CAPACITY=30`.

**This needs deciding before go-live** — it changes who answers most calls.

---

## Caller identity, and the SIM

`INBOUND_MODE` models the actual bottleneck:

- **`sim_forward`** — calls arrive via a forwarding SIM, so `From` is the SIM,
  not the citizen. The citizen is knowable only via `ForwardedFrom`, which
  Vobiz populates **solely from the SIP `Diversion` header** — not
  `History-Info`, not RDNIS. That is why Airtel passes it and Jio/Vi likely do
  not: they use a header Vobiz does not read.
- **`direct_did`** — callers dial the Vobiz DID directly, so `From` *is* the
  caller. Deterministic, carrier-independent.

Run this to see it rather than argue it:

```bash
python sim.py repeat                 # repeat caller recognised
python sim.py repeat --no-diversion  # same citizen, unidentifiable
```

Every decision records which source identified the caller and whether it was
trusted, visible in the console and at `/decisions`. **Scenario 1 (AI/human
overflow) works today. Scenario 2 (repeat caller) is gated on the DID.**

---

## The AI side

`AI_MODE` decides how the AI branch is fulfilled:

| Mode | What it returns | When |
| --- | --- | --- |
| `stub` | `<Speak>` describing the decision | **default** — proves routing with no Sarvam dependency |
| `stream` | `<Stream>` to `SARVAM_STREAM_WS` | Sarvam gives you a websocket |
| `proxy` | XML fetched from `SARVAM_ANSWER_URL` | Sarvam gives you an answer URL |

Start on `stub`. It means the routing layer can be demonstrated and signed off
before Sarvam is wired up at all.

Caller context reaches Sarvam as query parameters on the websocket URL
(`stream`) or as extra `Ipac*` POST fields (`proxy`): pool, caller number,
whether the identity is trusted, prior call count, complaint ID. Custom SIP
headers are **not** usable for this — `X-VH-*` reaches the dialling
application's own callbacks, not a streaming endpoint.

`proxy` mode fails gracefully: if Sarvam times out (`SARVAM_TIMEOUT_S`,
default 2s) the caller hears an apology rather than dead air, and the failure
is recorded in the decision trail.

---

## Endpoints

| Path | Purpose |
| --- | --- |
| `POST /answer` | the Vobiz answer URL — the routing decision |
| `POST /hangup` | the Vobiz hangup URL — **releases the slot** |
| `ANY /queue/{uuid}` | a held caller returns here to be re-decided |
| `POST /dial-action` | human leg result; a no-answer returns to the queue |
| `POST /escalate` | move a live AI call to a human (REST transfer) |
| `ANY /transfer-target` | XML the escalated leg executes |
| `GET/POST /agents` | register agents, set online/busy |
| `POST /complaint` | Sarvam writes the complaint ID back for next time |
| `GET /state` | live capacity, agents, history stats |
| `GET /decisions` | last 200 decisions with full reasoning |
| `GET /` | live console |
| `POST /reset` | clear state (`?wipe_history=true` also clears SQLite) |

---

## Things that will bite

These are the platform behaviours the code already works around. Changing the
code without knowing them will reintroduce the bug.

- **`<Dial>` needs an answered leg.** An inbound call reaches the answer URL
  while still ringing. Every branch that dials is preceded by `<Speak>`.
  Without it: `DialStatus=failed`, `ORIGINATOR_CANCEL`, and no useful error.
- **`callerId` must be a number the account owns.** Unset, Vobiz derives it
  from the inbound leg, which on a forwarded call is not owned, and the dial
  silently never originates. Set `CALLER_ID`; the router warns in the decision
  trail when it is missing.
- **`sipHeaders` accepts only `[A-Za-z0-9]`.** A `+` or a space makes Vobiz
  reject its own INVITE. Values are stripped through `sip_safe()`.
- **There is no queue verb.** Holding a caller is hold audio plus a
  `<Redirect>` back to `/queue/{uuid}` — the loop is ours, not the platform's.
- **Slots leak if `hangup_url` is not set.** Occupancy is counted here, not by
  Vobiz. A missed hangup webhook would reserve a slot forever, so every
  reservation carries a TTL (`MAX_CALL_SECONDS`) and is swept every 60s. The
  console shows reclaimed slots — a non-zero count means hangup webhooks are
  being missed, which is worth investigating.
- **Identity is resolved once, at `/answer`, and cached** for the life of the
  call. It is not established that Vobiz replays `ForwardedFrom` on a
  `<Redirect>`, and a caller must not lose their identity by being put on
  hold.
- **The answer URL is in the ringing critical path.** The history lookup has a
  hard 500 ms timeout and falls through to "unknown caller". Any router error
  fails open to AI — a router that 500s takes the whole helpline down.
- **`sys.exit()` must never appear in a request handler.** It raises
  `SystemExit`, which most `except Exception` guards miss. `vobiz.py` raises
  `VobizError` instead.

---

## Not covered here

- **CPS, not just concurrency.** Tens of thousands of calls/day with campaign
  bursts hits calls-per-second limits before channel count. Confirm the
  inbound CPS limit with Vobiz, and note that `429` is a documented response.
- **One process only.** `Slots` and `AgentPool` are in-memory, which is
  correct for a single router — a restart means the calls it was tracking are
  gone too. For more than one instance this becomes Redis; the interface is
  deliberately three methods wide.
- **Agent presence is manual** (`POST /agents`). Real deployments drive this
  from the agent desktop or ACD.
- **SMS on a captured number** is a webhook call-out (`SMS_WEBHOOK_URL`); no
  provider is wired in. The numbers are stored in the `missed` table either
  way.

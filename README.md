# Vobiz XML Call Router

A routing layer for Vobiz XML — and for the Plivo-compatible dialects it
shares. It sits on your voice application's **answer URL** and decides, per
call, where that call should go: an AI agent, a human, a SIP endpoint, a hold
queue, or a polite rejection with the caller's number captured.

```
caller → platform ──answer_url──> router ──┬── <Stream> to an AI backend
                                           ├── <Dial>   to a human / SIP endpoint
                                           ├── hold, re-decide when a channel frees
                                           └── reject unanswered, capture the number
```

**Media never passes through this service.** Audio continues to flow directly
between the platform and whatever answers. The router adds one HTTP round trip
*before* the call is answered — no added latency in the conversation itself.

---

## Contents

- [Why this exists](#why-this-exists)
- [Run it locally](#run-it-locally)
- [Inserting the router into an existing setup](#inserting-the-router-into-an-existing-setup)
- [How routing decisions are made](#how-routing-decisions-are-made)
- [Caller identity](#caller-identity)
- [Connecting an AI backend](#connecting-an-ai-backend)
- [Connecting humans and SIP endpoints](#connecting-humans-and-sip-endpoints)
- [Testing](#testing)
- [HTTP endpoints](#http-endpoints)
- [Configuration](#configuration)
- [Platform behaviours that will bite you](#platform-behaviours-that-will-bite-you)
- [Architecture](#architecture)

---

## Why this exists

On a programmable voice platform, an inbound number is bound to an
*application*, and that application has one answer URL. Whatever sits at that
URL owns the call. Most teams point it straight at a single destination — an AI
agent vendor, say — which works until you need any of this:

- **Overflow.** AI capacity is finite and conversations can run long. When it's
  saturated, calls should reach humans rather than fail.
- **Returning callers.** Someone who called yesterday should reach an agent
  that already knows their history, not start from scratch.
- **A queue.** Neither pool free? Hold the caller briefly instead of dropping.
- **Never losing a caller.** If nothing can take the call, capture the number
  and follow up.
- **Knowing why.** "Why did this call go to a human?" should be answerable
  after the fact.

Pointing the answer URL at this service gives you all of that, and it is a
**one-field change** on the platform side. Nothing else about your setup moves.

---

## Run it locally

```bash
git clone https://github.com/vobiz-ai/vobiz-xml-call-router.git
cd vobiz-xml-call-router
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env          # set PUBLIC_URL; everything else has defaults
./run.sh                      # listens on :8090
```

Open **http://127.0.0.1:8090/** for the live console — capacity per pool,
every recent decision, and the reasoning behind each one.

> Use `./run.sh`, not `python app.py`. It reclaims the port first. Without
> that, a second `python app.py` fails to bind, exits, and leaves the **old**
> process serving — so your changes appear to do nothing.

### Exposing it to the platform

The platform must reach this service over **HTTPS**. For local development:

```bash
cloudflared tunnel --url http://localhost:8090
# or: ngrok http 8090
```

Put the resulting URL in `.env` as `PUBLIC_URL` and restart. Verify:

```bash
curl https://your-tunnel.example.com/health
```

> Quick tunnels get a new hostname every restart. If the hostname changes and
> you don't update the application, every call silently takes your fallback
> URL — and it looks like the router isn't being called at all.

You now have a working router with **no backend dependency**: `AI_MODE=stub`
answers with a spoken description of the decision it made. That is deliberate —
it lets you prove the routing layer before wiring anything else up.

---

## Inserting the router into an existing setup

This is the part most people are here for. You have a number, it's bound to an
application, and that application points at something that works. The goal is
to slide the router in front **without breaking what already works**.

### Step 1 — Create a *separate* application

Do not repoint your existing application. It may serve several numbers, and you
want a clean rollback.

```bash
curl -X POST "https://api.<platform>/api/v1/Account/$AUTH_ID/Application/" \
  -H "X-Auth-ID: $AUTH_ID" -H "X-Auth-Token: $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "app_name": "XML-Call-Router",
    "answer_url":  "'"$PUBLIC_URL"'/answer",  "answer_method": "POST",
    "hangup_url":  "'"$PUBLIC_URL"'/hangup",  "hangup_method": "POST",
    "fallback_answer_url": "<your current answer URL>",
    "fallback_method": "POST"
  }'
```

**Set `fallback_answer_url` to whatever the number points at today.** If the
router is unreachable or returns invalid XML, the platform falls back to your
existing behaviour instead of dropping the call. With no fallback configured,
an answer-URL failure drops the call.

### Step 2 — Move one number onto it

```bash
curl -X POST ".../Account/$AUTH_ID/numbers/%2B<E164>/application" \
  -H "X-Auth-ID: $AUTH_ID" -H "X-Auth-Token: $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"application_id": "<new app id>"}'     # 204 on success
```

Note the path is lowercase `numbers`, and `+` must be encoded as `%2B`.

### Step 3 — Call it

With `AI_MODE=stub` you'll hear the router describe its own decision. Watch the
console. Once that works, configure your real backend (next section) and call
again.

### Step 4 — Roll back if needed

Re-attach the number to its original application. One API call, instant.

### Where the router fits relative to your backend

The router does not replace your backend — it decides *whether* the call
reaches it. Your backend keeps its own integration, and the router passes the
call through with the caller's identity attached.

---

## How routing decisions are made

Three pools, each with its own capacity. A call is offered to them in an order
set by `ROUTE_POLICY`:

| Policy | Order |
|---|---|
| `ai_first` | AI → human → queue → reject |
| `human_first` | human → AI → queue → reject |

```
AI free?     → <Stream> or the backend's own XML
human free?  → <Dial> to an agent or SIP endpoint
queue free?  → hold audio, then re-decide when it finishes
otherwise    → ring briefly, reject unanswered, record the number
```

**Capacity is counted here, not by the platform.** The platform has no idea
which of your channels are AI and which are human, and will never tell you a
pool is full. The router reserves a slot at `/answer` and releases it at
`/hangup`.

Because a missed hangup webhook would reserve a slot forever and silently
shrink the pool, **every reservation carries a TTL** (`MAX_CALL_SECONDS`) and is
swept every 60 seconds. The console shows reclaimed slots — a non-zero count
means hangup webhooks are going missing and is worth investigating.

A human slot needs both a free channel *and* a logged-in agent. Capacity alone
is not availability; routing to a channel with nobody behind it just rings out.

### The queue

There is no queue verb in these XML dialects. Holding a caller means playing
them something and redirecting back to `/queue/{uuid}` when it finishes, where
they are decided again. After `QUEUE_MAX_CYCLES` the router gives up, says so,
and records the number.

---

## Caller identity

Set `INBOUND_MODE` to describe how calls reach you:

| Mode | Meaning |
|---|---|
| `direct_did` | callers dial your number directly, so `From` **is** the caller |
| `sim_forward` | calls arrive via a forwarding line, so `From` is that line |

Under `sim_forward` the real caller is only knowable from `ForwardedFrom`,
which these platforms populate from the SIP **`Diversion`** header. Not every
carrier sends one — and some send the redirecting number via `History-Info` or
RDNIS instead, which may not be read at all. When it's absent, every caller
presents as the same forwarding number.

The router makes this explicit rather than silently wrong. Each decision
records which source identified the caller and whether it was trusted:

```
· repeat-caller lookup skipped: From is the forwarding line, not the caller;
  carrier sent no Diversion header
```

You can see both cases without placing a call:

```bash
python sim.py repeat                 # caller recognised
python sim.py repeat --no-diversion  # same caller, unidentifiable
```

**Identity is resolved once, at `/answer`, and cached for the life of the
call.** It is not guaranteed that a platform replays `ForwardedFrom` on a
`<Redirect>`, and a caller must not lose their identity by being put on hold.

### Returning callers

When a caller is identified and known, they route to the `ai_repeat` pool
instead of `ai_new`. History lives in SQLite and survives restarts. Your
backend writes back at the end of a conversation:

```bash
curl -X POST $PUBLIC_URL/reference \
  -d "number=+14155550100&reference=CASE-2291&summary=Billing query, unresolved"
```

That reference then travels with the caller's next call.

---

## Connecting an AI backend

Nothing here is vendor-specific. Pick the mode that matches how your backend
expects to be integrated.

### `AI_MODE=stub` — no backend

Answers with a spoken description of the routing decision. Use it to prove the
routing layer, and as a safe default.

### `AI_MODE=proxy` — your backend issues its own XML

The router POSTs the call to `AI_ANSWER_URL` and returns the XML it replies
with, unchanged. Use this when your backend needs to create a session per call.

It receives everything the platform sent, plus:

| Field | Meaning |
|---|---|
| `From` | the **resolved** caller (see below) |
| `OriginalFrom` | exactly what the platform sent |
| `RouterPool` | `ai_new` or `ai_repeat` |
| `RouterIdentified` | whether the caller identity is trustworthy |
| `RouterIdentitySource` | `ForwardedFrom`, `From`, or `unknown` |
| `RouterRepeatCaller`, `RouterPriorCalls`, `RouterReference` | history |

`From` is overwritten with the resolved caller so **your backend needs no code
change** — it reads `From` as it always did and now sees the real caller rather
than the forwarding line. The original is preserved as `OriginalFrom`. Disable
with `FORWARD_RESOLVED_FROM=false`.

If the backend times out (`AI_TIMEOUT_S`, default 2s) the caller hears
`AI_FALLBACK_TEXT` rather than dead air, and the failure is recorded in the
decision trail.

> **Agent selection.** Backends commonly choose which agent answers from the
> number the call came in on. `PROXY_REWRITE_TO=true` rewrites `To` to
> `AI_TARGET_NEW` / `AI_TARGET_REPEAT` so the router picks the agent instead —
> which is how you give returning callers a different agent. It is **off by
> default**, because pointing at an identifier with no live agent behind it can
> make a backend accept the connection and drop it with no error, which looks
> exactly like a dropped call.

### `AI_MODE=stream` — your backend takes a media socket

The router returns a `<Stream>` to a websocket it builds from `AI_STREAM_URL`.
The caller travels as a query parameter — and since no second call leg is
created, nothing in the path can strip it.

Canonical fields are `caller`, `called`, `target`, `call_id`, `direction`. Map
them onto whatever your backend calls them:

```bash
STREAM_PARAM_MAP=caller:user_phone_number,target:agent_phone_number,call_id:call_sid
```

Router context is added under `CONTEXT_PREFIX` (default `router_`), namespaced
so it cannot collide with your backend's own parameters.

The router always places a spoken fallback after the `<Stream>`. A bare
`<Hangup/>` there means any socket failure ends the call in about a second with
nothing to hear — indistinguishable from a dropped call.

### Seeing exactly what your backend receives

```bash
python mock_backend.py               # listens on :8091, prints every field
# set AI_MODE=proxy, AI_ANSWER_URL=http://127.0.0.1:8091/answer
```

---

## Connecting humans and SIP endpoints

`AGENT_NUMBERS` takes a comma-separated list. Entries are dialled according to
their shape:

```bash
AGENT_NUMBERS=+14155550100,sip:agent@pbx.example.com
```

- a PSTN number → `<Dial><Number>`
- anything starting with `sip:` → `<Dial><User>`

Routing context reaches the agent leg as `X-VH-*` SIP headers (`Pool`,
`Caller`, `Repeat`, `Reference`).

> Header values accept only `[A-Za-z0-9]`. A `+` or a space makes the platform
> reject its own INVITE and the leg never rings. Values are stripped
> automatically; if your endpoint needs E.164, send it split
> (`CC=1,Caller=4155550100`) and reassemble on your side.

Agents can also register at runtime, which is how you'd drive this from an
agent desktop or ACD:

```bash
curl -X POST $PUBLIC_URL/agents -d "number=+14155550100&register=true&online=true"
curl -X POST $PUBLIC_URL/agents -d "number=+14155550100&busy=true"
```

### Escalating a live AI call to a human

```bash
curl -X POST $PUBLIC_URL/escalate -d "call_uuid=<uuid>"
```

The transfer API is a *redirect*, not a bridge: the leg abandons its current
XML and executes what `/transfer-target` returns. If the transfer fails, the
agent and slot are rolled back rather than left marked busy.

---

## Testing

Every branch is reachable over HTTP, so the routing logic can be proven in
seconds rather than by dialling repeatedly.

```bash
python verify.py         # 11 assertions across every branch; exits non-zero on failure
python sim.py all        # the same paths, narrated
```

Individually:

```bash
python sim.py identity                # can the router tell who is calling?
python sim.py fill --calls 95         # where each capacity threshold trips
python sim.py repeat                  # first-time vs returning caller
python sim.py repeat --no-diversion   # the same, with no Diversion header
python sim.py queue                   # a held caller promoted when a slot frees
python sim.py escalate-dry            # AI → human mid-call
```

Set small capacities while testing (`AI_CAPACITY=5`, `HUMAN_CAPACITY=3`,
`QUEUE_CAPACITY=2`) so the cascade is readable.

`ALLOW_FORCE_ROUTE=true` enables `?force=human|ai` on the answer URL, pinning a
branch so you can exercise one path without arranging capacity to trip it. It
is off by default so it cannot be used against a production answer URL.

To place a real call through the router:

```bash
python call.py --to +14155550100
python call.py --to +14155550100 --forwarded-from +14155550199   # fake a forward
```

---

## HTTP endpoints

| Path | Purpose |
|---|---|
| `POST /answer` | the platform's answer URL — the routing decision |
| `POST /hangup` | the platform's hangup URL — **releases the slot** |
| `ANY /queue/{uuid}` | a held caller returns here to be re-decided |
| `POST /dial-action` | human leg result; a no-answer returns to the queue |
| `POST /stream-status` | stream lifecycle events, logged in full |
| `POST /escalate` | move a live AI call to a human |
| `ANY /transfer-target` | XML the escalated leg executes |
| `GET/POST /agents` | register agents, set online/busy |
| `POST /reference` | backend writes a reference back for next time |
| `GET /state` | live capacity, agents, history stats |
| `GET /decisions` | last 200 decisions with full reasoning |
| `GET /health` | readiness and effective config |
| `GET /` | live console |
| `POST /reset` | clear state (`?wipe_history=true` also clears SQLite) |

---

## Configuration

Every option lives in `.env`; see `.env.example` for the annotated list. The
ones you'll actually touch:

| Variable | Default | Purpose |
|---|---|---|
| `PUBLIC_URL` | — | HTTPS URL the platform reaches you on |
| `ROUTE_POLICY` | `ai_first` | `ai_first` or `human_first` |
| `AI_CAPACITY` / `HUMAN_CAPACITY` / `QUEUE_CAPACITY` | 50 / 30 / 5 | pool sizes |
| `INBOUND_MODE` | `direct_did` | `direct_did` or `sim_forward` |
| `AI_MODE` | `stub` | `stub`, `proxy`, or `stream` |
| `AI_ANSWER_URL` / `AI_STREAM_URL` | — | where the backend lives |
| `STREAM_PARAM_MAP` | — | canonical → backend parameter names |
| `AGENT_NUMBERS` | — | PSTN numbers and/or `sip:` URIs |
| `CALLER_ID` | — | **must be a number your account owns** |
| `MAX_CALL_SECONDS` | 3600 | slot TTL, guards against missed hangups |

---

## Platform behaviours that will bite you

These are worked around in the code. Changing it without knowing them will
reintroduce the bug.

- **`<Dial>` needs an answered leg.** An inbound call reaches the answer URL
  while still ringing. Every branch that dials is preceded by `<Speak>`.
  Without it: `DialStatus=failed`, `ORIGINATOR_CANCEL`, no useful error.
- **`callerId` must be a number the account owns.** Unset, the platform derives
  it from the inbound leg, which on a forwarded call is not owned, and the dial
  silently never originates.
- **`sipHeaders` accepts only `[A-Za-z0-9]`.** A `+` or space makes the
  platform reject its own INVITE.
- **Slots leak without a hangup URL.** Occupancy is counted here. Set
  `hangup_url`, and watch the reclaimed-slot count.
- **The answer URL is in the ringing critical path.** The history lookup has a
  hard 500 ms timeout and falls through to "unknown caller". Any router error
  fails open to AI — a router that 500s takes the whole line down.
- **`sys.exit()` must never appear in a request handler.** It raises
  `SystemExit`, which most `except Exception` guards miss.
- **Form parsing.** The platform posts `application/x-www-form-urlencoded`,
  parsed here with `parse_qsl` rather than `request.form()`, which needs
  `python-multipart` installed and raises if it is missing — a failure that
  looks exactly like "the platform sent no parameters".

---

## Architecture

```
router.py    pure decision engine — no I/O, no globals, no framework
state.py     capacity accounting (TTL-swept), caller history, decision log
app.py       platform webhooks and the XML for each destination
vobiz.py     platform REST calls (outbound call, live transfer)
console.html live view
sim.py       narrated scenarios     verify.py  11 end-to-end assertions
```

The split matters: `router.py` takes identity, history, capacity and config,
and returns a decision with the reasoning trail attached. It touches nothing
external, so every branch is testable without placing a call. `app.py` turns
that decision into XML, which keeps the hard part testable and the XML part
boring.

**Porting to another platform** means changing the XML builders in `app.py` and
the REST calls in `vobiz.py`. Written against Vobiz's dialect, which Plivo
shares and Twilio closely resembles. The decision engine is independent of all
of it.

**Scaling past one process:** `Slots` and `AgentPool` are in-memory, which is
correct for a single instance — a restart means the calls it was tracking are
gone too, so persisting them would restore phantom occupancy. For more than one
instance this becomes Redis; the interface is deliberately three methods wide.

---

## Licence

MIT. See [LICENSE](LICENSE).

Copyright (c) 2026 Ilaimitado Private Limited.

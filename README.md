# Vobiz XML Call Router

A routing layer for Vobiz XML. It sits on your voice application's **answer
URL** and decides, per call, where that call goes: a Sarvam AI agent, a human,
a SIP endpoint, a hold queue, or a rejection with the caller's number captured.

```
caller → Vobiz ──answer_url──> router ──┬── <Stream> to Sarvam
                                        ├── <Dial>   to a human / SIP endpoint
                                        ├── hold, re-decide when a channel frees
                                        └── reject unanswered, capture the number
```

**Media never passes through it.** Audio still flows directly between Vobiz and
whatever answers. The router adds one HTTP round trip *before* the call is
answered — no latency in the conversation itself.

Pointing your answer URL here is a **one-field change**. Nothing else moves.

---

## Quick start

```bash
git clone https://github.com/vobiz-ai/vobiz-xml-call-router-sarvam.git
cd vobiz-xml-call-router-sarvam
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env          # set PUBLIC_URL
./run.sh                      # :8090, console at http://127.0.0.1:8090/
```

Expose it — Vobiz needs HTTPS:

```bash
cloudflared tunnel --url http://localhost:8090   # put the URL in PUBLIC_URL
```

`AI_MODE=stub` is the default, so it works with **no backend at all** — it
answers with a spoken description of the decision it just made. Prove the
routing first, wire Sarvam in after.

> Use `./run.sh`, not `python app.py`. It reclaims the port first; otherwise a
> second `python app.py` fails to bind and leaves the **old** process serving,
> so your changes appear to do nothing.

---

## Putting it in front of an existing setup

Don't repoint your existing application — it may serve several numbers, and you
want a clean rollback.

**1. New application, with a fallback to what works today:**

```bash
curl -X POST "https://api.vobiz.ai/api/v1/Account/$AUTH_ID/Application/" \
  -H "X-Auth-ID: $AUTH_ID" -H "X-Auth-Token: $AUTH_TOKEN" \
  -H "Content-Type: application/json" -d '{
    "app_name": "XML-Call-Router",
    "answer_url": "'"$PUBLIC_URL"'/answer", "answer_method": "POST",
    "hangup_url": "'"$PUBLIC_URL"'/hangup", "hangup_method": "POST",
    "fallback_answer_url": "https://apps.sarvam.ai/api/app-runtime/v1/channels/vobiz",
    "fallback_method": "POST"
  }'
```

`fallback_answer_url` matters: if the router is unreachable or returns bad XML,
Vobiz falls back to your current behaviour instead of dropping the call. With
no fallback set, an answer-URL failure **drops the call**.

**2. Move one number** (lowercase `numbers`, `+` encoded as `%2B`):

```bash
curl -X POST ".../Account/$AUTH_ID/numbers/%2B<E164>/application" \
  -H "X-Auth-ID: $AUTH_ID" -H "X-Auth-Token: $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"application_id": "<new app id>"}'      # 204 on success
```

**3. Call it.** **4. Roll back** by re-attaching the number to the old app.

---

## Deploying for a customer

Every customer has their own Sarvam URL, their own DID, and their own agents.
**Run one router per customer** — one `.env`, one port, one tunnel or host.
The router holds live capacity counters and caller history in that process, so
keeping customers in separate instances keeps their state separate too.

Everything customer-specific is in `.env`:

```bash
# ── the customer's Sarvam app ────────────────────────────────────────────
AI_MODE=proxy
AI_ANSWER_URL=https://apps.sarvam.ai/api/app-runtime/v1/channels/vobiz
#              ^ the customer's own app-runtime URL, if it differs

# ── the customer's Vobiz account ─────────────────────────────────────────
VOBIZ_AUTH_ID=MA_XXXXXXXX
VOBIZ_AUTH_TOKEN=...
CALLER_ID=+91XXXXXXXXXX          # a number THIS account owns
FROM_NUMBER=+91XXXXXXXXXX

# ── this instance ────────────────────────────────────────────────────────
PUBLIC_URL=https://customer-a.yourdomain.com
PORT=8090                        # a different port per instance on one host

# ── the customer's routing policy ────────────────────────────────────────
ROUTE_POLICY=ai_first
AI_CAPACITY=50
HUMAN_CAPACITY=30
AGENT_NUMBERS=+91XXXXXXXXXX,sip:agent@customer-pbx.example.com
INBOUND_MODE=direct_did          # sim_forward if a SIM forwards to their DID
```

**You do not configure the DID here.** The customer's inbound number is
attached to their Sarvam agent on Sarvam's side, and to the router's
application on Vobiz's side. The router reads the dialled number from the call
and passes it straight through as `To`, which is how Sarvam knows which agent
should answer. That is also why `PROXY_REWRITE_TO` stays off unless you
deliberately want a different agent — see [Sarvam](#sarvam).

So per customer:

1. Copy `.env.example` to `.env`, fill in the block above.
2. Start the instance, give it a public HTTPS URL.
3. Create a Vobiz application pointing at that URL, with
   `fallback_answer_url` set to the customer's Sarvam URL.
4. Attach the customer's DID to that application.
5. Call it.

Nothing about the customer lives in code — a new customer is a new `.env` and
a new application.

---

## Sarvam

Sarvam exposes an answer URL and replies with its own XML, so use proxy mode:

```bash
AI_MODE=proxy
AI_ANSWER_URL=https://apps.sarvam.ai/api/app-runtime/v1/channels/vobiz
```

The router POSTs the call to Sarvam, and returns Sarvam's XML — which looks
like this:

```xml
<Record fileFormat="mp3" recordSession="true" maxLength="9999" redirect="false"/>
<Stream bidirectional="true" contentType="audio/x-l16;rate=8000"
        keepCallAlive="true" maxRetries="0">
  wss://apps.sarvam.ai/…?agent_phone_number=<the DID>&user_phone_number=<caller>
      &call_sid=<CallUUID>&call_direction=inbound
</Stream><Hangup/>
```

Four things worth knowing, all learned the hard way:

- **Sarvam picks the agent from `To`.** It derives `agent_phone_number` from
  the number dialled. So the DID must have a live Sarvam agent bound to it.
- **Don't rewrite `To` casually.** `PROXY_REWRITE_TO=true` re-points the call at
  `AI_TARGET_NEW` / `AI_TARGET_REPEAT`, which is how you give returning callers
  a different agent — but if that number has no live agent, Sarvam accepts the
  websocket and closes it with **no error**. The call dies in ~1s and looks
  exactly like a dropped call. Off by default.
- **`maxRetries="0"` plus a bare `<Hangup/>`** means any socket failure ends the
  call in about a second with nothing to hear.
- **Sarvam sets no `statusCallbackUrl`**, so Vobiz posts stream events to the
  literal string `no-stream-status-callback-url` and gets `400`. The router
  injects its own, so stream failures land in your log instead of vanishing.

The router also rewrites `From` to the resolved caller, so **Sarvam needs no
code change** — it reads `From` as always and now sees the real caller rather
than a forwarding line. The original is kept as `OriginalFrom`.

Other backends: `AI_MODE=stream` returns a `<Stream>` the router builds itself,
with `STREAM_PARAM_MAP` mapping its canonical fields (`caller`, `called`,
`target`, `call_id`, `direction`) onto whatever that backend calls them.

---

## How it decides

Three pools with their own capacities, tried in an order set by `ROUTE_POLICY`
(`ai_first` or `human_first`):

```
AI free?     → Sarvam
human free?  → <Dial> to an agent or SIP endpoint
queue free?  → hold audio, re-decide when it finishes
otherwise    → ring briefly, reject unanswered, record the number
```

**Capacity is counted here, not by Vobiz** — the platform has no idea which
channels are AI and which are human. A slot is reserved at `/answer` and
released at `/hangup`. Because a missed hangup webhook would reserve a slot
forever, every reservation has a TTL (`MAX_CALL_SECONDS`) and is swept every
60s. A non-zero reclaimed count in the console means hangup webhooks are going
missing.

There is no queue verb in Vobiz XML: holding means playing audio and
redirecting back to `/queue/{uuid}`, where the caller is decided again.

---

## Caller identity

| `INBOUND_MODE` | Meaning |
|---|---|
| `direct_did` | callers dial your number directly — `From` **is** the caller |
| `sim_forward` | calls arrive via a forwarding line — `From` is that line |

Under `sim_forward` the real caller is only knowable from `ForwardedFrom`,
which Vobiz populates from the SIP **`Diversion`** header alone — not
`History-Info`, not RDNIS. Many carriers send none, and then every caller
presents as the same forwarding number.

The router makes that explicit rather than silently wrong — each decision
records which source identified the caller and whether it was trusted. See it
without dialling:

```bash
python sim.py repeat                 # caller recognised
python sim.py repeat --no-diversion  # same caller, unidentifiable
```

Identity is resolved once at `/answer` and cached for the call: it isn't
established that Vobiz replays `ForwardedFrom` on a `<Redirect>`, and a caller
must not lose their identity by being put on hold.

**Returning callers** route to `ai_repeat` instead of `ai_new`. History is
SQLite and survives restarts. Your backend writes back at the end of a call:

```bash
curl -X POST $PUBLIC_URL/reference -d "number=<E164>&reference=CASE-2291"
```

---

## Humans and SIP endpoints

```bash
AGENT_NUMBERS=+14155550100,sip:agent@pbx.example.com
```

A PSTN number is dialled with `<Number>`; anything starting with `sip:` with
`<User>`. Context reaches the agent leg as `X-VH-*` SIP headers.

> Header values accept only `[A-Za-z0-9]`. A `+` or space makes Vobiz reject
> its own INVITE and the leg never rings. Values are stripped automatically.

Agents register at runtime, so this can be driven from an agent desktop:

```bash
curl -X POST $PUBLIC_URL/agents -d "number=+14155550100&register=true&online=true"
```

Escalate a live AI call: `curl -X POST $PUBLIC_URL/escalate -d "call_uuid=<uuid>"`.
The transfer API is a redirect, not a bridge — the leg abandons its current XML
and runs what `/transfer-target` returns.

---

## Testing

```bash
python verify.py       # 11 assertions across every branch; non-zero on failure
python sim.py all      # the same paths, narrated
```

`sim.py` drives the router with synthetic calls so you can watch decisions
without dialling: `identity`, `fill`, `repeat`, `queue`. Use small capacities
(`AI_CAPACITY=5`, `HUMAN_CAPACITY=3`, `QUEUE_CAPACITY=2`) so the cascade is
readable.

`ALLOW_FORCE_ROUTE=true` enables `?force=human|ai` to pin a branch. Off by
default so it can't be used against a production answer URL.

`python mock_backend.py` stands in for a backend on `:8091` and prints every
field the router forwards.

---

## Endpoints

| Path | Purpose |
|---|---|
| `POST /answer` | the Vobiz answer URL — the routing decision |
| `POST /hangup` | the Vobiz hangup URL — **releases the slot** |
| `ANY /queue/{uuid}` | a held caller returns here to be re-decided |
| `POST /dial-action` | human leg result; no-answer returns to the queue |
| `POST /stream-status` | stream lifecycle events, logged in full |
| `POST /escalate` | move a live AI call to a human |
| `GET/POST /agents` | register agents, set online/busy |
| `POST /reference` | backend writes a reference back for next time |
| `GET /state` · `GET /decisions` | live capacity · last 200 decisions with reasoning |
| `GET /` | live console |
| `POST /reset` | clear state (`?wipe_history=true` also clears SQLite) |

Full configuration is in `.env.example`.

---

## Gotchas

Worked around in the code — reintroduce them at your peril.

- **`<Dial>` needs an answered leg.** Inbound calls reach the answer URL while
  still ringing, so every dialling branch is preceded by `<Speak>`. Without it:
  `DialStatus=failed`, `ORIGINATOR_CANCEL`, no useful error.
- **`callerId` must be a number the account owns**, or the dial silently never
  originates.
- **The answer URL is in the ringing critical path.** The history lookup has a
  hard 500ms timeout; any router error fails open to AI.
- **`sys.exit()` in a request handler** raises `SystemExit`, which most
  `except Exception` guards miss.
- **Vobiz posts `application/x-www-form-urlencoded`**, parsed with `parse_qsl`
  rather than `request.form()`, which needs `python-multipart` and raises
  without it — a failure that looks exactly like "Vobiz sent no parameters".

---

## Architecture

```
router.py    pure decision engine — no I/O, no globals, no framework
state.py     capacity accounting (TTL-swept), caller history, decision log
app.py       Vobiz webhooks and the XML for each destination
vobiz.py     REST calls (outbound call, live transfer)
sim.py       narrated scenarios     verify.py   11 end-to-end assertions
```

`router.py` takes identity, history, capacity and config and returns a decision
with its reasoning attached. It touches nothing external, so every branch is
testable without placing a call. `app.py` turns decisions into XML — which
keeps the hard part testable and the XML part boring.

`Slots` and `AgentPool` are in-memory, correct for one instance: a restart
means the calls it tracked are gone too, so persisting them would restore
phantom occupancy. For more than one instance this becomes Redis; the interface
is three methods wide.

---

## Licence

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Ilaimitado Private Limited.

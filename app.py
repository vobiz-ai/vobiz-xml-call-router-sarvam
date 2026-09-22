"""
app.py — a routing layer for programmable voice
================================================
Sits between a programmable-voice platform and whatever should handle the
call. The platform posts every inbound call here; this service decides where
it goes and returns the XML that sends it there.

    caller → platform --answer_url--> router --+--> <Stream> to an AI backend
                                               +--> <Dial> to a human or SIP endpoint
                                               +--> hold, re-decide when a channel frees
                                               +--> reject unanswered, capture the number

Media never passes through this service. It is control plane only: one HTTP
round trip before the call is answered, and no added audio latency.

Written against Vobiz's XML dialect, which Plivo shares and Twilio closely
resembles. Swapping platform means changing the XML builders near the top and
the REST calls in vobiz.py; the decision engine in router.py is independent
of all of it.

Run:
    pip install -r requirements.txt
    cp .env.example .env         # set PUBLIC_URL
    ./run.sh                     # listens on :8090
    open http://127.0.0.1:8090/  # live console

Then point your voice application's Answer URL at {PUBLIC_URL}/answer and its
Hangup URL at {PUBLIC_URL}/hangup.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode
from xml.sax.saxutils import escape

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import vobiz
from router import (CallerHistory, Config, Identity, decide, normalise,
                    resolve_identity)
from state import AgentPool, DecisionLog, History, Slots

load_dotenv(Path(__file__).parent / ".env", override=True)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("router")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# --- Configuration ---------------------------------------------------------

PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").rstrip("/")
PORT = _int("PORT", 8090)

CONFIG = Config(
    policy=os.getenv("ROUTE_POLICY", "ai_first"),
    ai_capacity=_int("AI_CAPACITY", 50),
    human_capacity=_int("HUMAN_CAPACITY", 30),
    queue_capacity=_int("QUEUE_CAPACITY", 5),
    queue_max_cycles=_int("QUEUE_MAX_CYCLES", 3),
    hold_seconds=_int("HOLD_SECONDS", 15),
    inbound_mode=os.getenv("INBOUND_MODE", "sim_forward"),
    sim_number=os.getenv("SIM_NUMBER", ""),
    repeat_caller_routing=os.getenv("REPEAT_CALLER_ROUTING", "true").lower() == "true",
    repeat_window_days=_int("REPEAT_WINDOW_DAYS", 30),
)

# How the AI branch is fulfilled. None of this names a particular vendor:
#   stub   — <Speak> describing the decision. No backend needed at all, which
#            is what lets the routing layer be proven before one is wired up.
#   proxy  — POST the call to the backend's own answer URL and return the XML
#            it replies with. Use when the backend issues its own XML and
#            expects to create a session per call.
#   stream — return a <Stream> to a websocket this service builds. Use when
#            the backend takes a raw media socket.
AI_MODE = os.getenv("AI_MODE", "stub")

# proxy mode
AI_ANSWER_URL = os.getenv("AI_ANSWER_URL", "")
# Backends commonly select which agent answers from the number the call came
# in on. Rewriting `To` therefore re-points the call at a different agent —
# powerful, but if nothing is listening on that identifier the backend may
# accept the connection and drop it with no error. Off by default: the dialled
# number is normally already bound to the right agent.
PROXY_REWRITE_TO = os.getenv("PROXY_REWRITE_TO", "false").lower() == "true"

# stream mode
AI_STREAM_URL = os.getenv("AI_STREAM_URL", "")
AI_CONTENT_TYPE = os.getenv("AI_CONTENT_TYPE", "audio/x-mulaw;rate=8000")

# Optional identifier naming which agent should answer, per pool. Leave empty
# to let the backend decide from the dialled number.
AI_TARGET_NEW = os.getenv("AI_TARGET_NEW", "")
AI_TARGET_REPEAT = os.getenv("AI_TARGET_REPEAT", "") or AI_TARGET_NEW

# Every backend spells its query parameters differently. Rather than hardcode
# one vendor's names, map our canonical fields onto theirs:
#   STREAM_PARAM_MAP=caller:user_phone_number,target:agent_phone_number,call_id:call_sid
# Unmapped fields keep their canonical name.
def _parse_map(raw: str) -> dict:
    out = {}
    for pair in raw.split(","):
        if ":" in pair:
            k, _, v = pair.partition(":")
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


STREAM_PARAM_MAP = _parse_map(os.getenv("STREAM_PARAM_MAP", ""))

# Prefix for the context fields this service adds alongside the backend's own.
CONTEXT_PREFIX = os.getenv("CONTEXT_PREFIX", "router_")

AI_TIMEOUT_S = float(os.getenv("AI_TIMEOUT_S", "2.0"))
AI_FALLBACK_TEXT = os.getenv(
    "AI_FALLBACK_TEXT",
    "We could not connect you to an agent. Please try again shortly.")

# Hand the backend the caller we resolved, in the field it already reads. Once
# this service is in front, the backend no longer receives the call from the
# platform — it receives what we forward, so this is the only place that can
# give it a usable caller. The untouched original is kept as OriginalFrom.
FORWARD_RESOLVED_FROM = os.getenv("FORWARD_RESOLVED_FROM", "true").lower() == "true"

CALLER_ID = os.getenv("CALLER_ID", "") or os.getenv("FROM_NUMBER", "")
AGENT_NUMBERS = [n.strip() for n in os.getenv("AGENT_NUMBERS", "").split(",") if n.strip()]
DIAL_TIMEOUT = _int("DIAL_TIMEOUT", 30)
HOLD_MUSIC_URL = os.getenv("HOLD_MUSIC_URL", "")
SMS_WEBHOOK_URL = os.getenv("SMS_WEBHOOK_URL", "")
MAX_CALL_SECONDS = _int("MAX_CALL_SECONDS", 3600)
ALLOW_FORCE_ROUTE = os.getenv("ALLOW_FORCE_ROUTE", "false").lower() == "true"

SPEAK = 'voice="{v}" language="{l}"'.format(
    v=os.getenv("SPEAK_VOICE", "WOMAN"), l=os.getenv("SPEAK_LANGUAGE", "en-IN")
)

# --- Shared state ----------------------------------------------------------

slots = Slots(ttl_seconds=MAX_CALL_SECONDS)
agents = AgentPool(AGENT_NUMBERS)
history = History()
decisions = DecisionLog()
queue_cycles: dict[str, int] = {}

# Identity and history resolved once, at /answer, and reused for the rest of
# the call. Re-deriving them on a <Redirect> would be both slower and wrong:
# it is not established that Vobiz replays ForwardedFrom on a redirect, and a
# caller must not lose their identity just because they were put on hold.
call_context: dict[str, tuple[Identity, CallerHistory]] = {}

app = FastAPI(title="Call router")


# --- Helpers ---------------------------------------------------------------


def xml(body: str) -> Response:
    doc = f'<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n{body}\n</Response>'
    return Response(content=doc, media_type="application/xml")


def base_url(request: Request) -> str:
    """Prefer PUBLIC_URL; fall back to the request's own host so a fresh
    tunnel works without editing .env first."""
    if PUBLIC_URL:
        return PUBLIC_URL
    host = request.headers.get("host", "")
    # Vobiz requires HTTPS, but a bare local run has no tunnel in front of it
    # and emitting https://127.0.0.1 in the XML is just confusing to read.
    default = "http" if host.startswith(("127.0.0.1", "localhost")) else "https"
    proto = request.headers.get("x-forwarded-proto", default)
    return f"{proto}://{host}"


async def call_params(request: Request) -> dict:
    """Read call parameters from wherever they are.

    Vobiz posts application/x-www-form-urlencoded, and answer_method can also
    be GET. That body is parsed here with parse_qsl rather than
    `request.form()`, which needs python-multipart installed and raises if it
    is missing — a failure that looks exactly like "Vobiz sent no parameters"
    and is miserable to track down. JSON is accepted too so curl and sim.py
    can drive the same endpoints.
    """
    params = dict(request.query_params)
    if request.method != "POST":
        return params

    ctype = request.headers.get("content-type", "").split(";")[0].strip()
    raw = await request.body()
    if not raw:
        return params

    if ctype == "application/json":
        import json as _json

        try:
            body = _json.loads(raw)
            if isinstance(body, dict):
                params.update({k: str(v) for k, v in body.items()})
        except ValueError:
            log.warning("answer webhook: body claimed JSON but did not parse")
    elif ctype == "multipart/form-data":
        try:
            form = await request.form()
            params.update({k: str(v) for k, v in form.items()})
        except Exception as exc:                      # needs python-multipart
            log.warning("answer webhook: multipart parse failed: %r", exc)
    else:
        # urlencoded, and anything unlabelled — Vobiz's normal shape.
        params.update(
            {k: v for k, v in parse_qsl(raw.decode("utf-8", "replace"),
                                        keep_blank_values=True)}
        )
    return params


def sip_safe(value: str) -> str:
    """sipHeaders keys and values accept only [A-Za-z0-9]. A '+' or a space
    makes Vobiz reject its own INVITE, and the leg silently never rings."""
    return re.sub(r"[^A-Za-z0-9]", "", value or "")


def stream_url(decision, params: dict) -> str:
    """Build the backend's websocket URL, carrying who is calling.

    The caller travels as a query parameter because no second call leg exists
    to carry it any other way — and that is the point: nothing in the path can
    strip an identity that never leaves the original leg.

    Field names are mapped through STREAM_PARAM_MAP so this works against a
    backend that spells them differently, without changing code.
    """
    if not AI_STREAM_URL:
        return ""

    target = AI_TARGET_REPEAT if decision.pool == "ai_repeat" else AI_TARGET_NEW

    canonical = {
        "caller": decision.identity.best() or params.get("From", ""),
        "called": params.get("To", ""),
        "call_id": params.get("CallUUID", ""),
        "direction": params.get("Direction", "inbound"),
    }
    if target:
        canonical["target"] = target

    ctx = {STREAM_PARAM_MAP.get(k, k): v for k, v in canonical.items() if v}

    # Our own context, namespaced so it cannot collide with the backend's.
    ctx[f"{CONTEXT_PREFIX}pool"] = decision.pool
    ctx[f"{CONTEXT_PREFIX}identified"] = str(decision.identity.confident).lower()
    ctx[f"{CONTEXT_PREFIX}identity_source"] = decision.identity.source
    if decision.history.known:
        ctx[f"{CONTEXT_PREFIX}repeat_caller"] = "true"
        ctx[f"{CONTEXT_PREFIX}prior_calls"] = str(decision.history.call_count)
        if decision.history.reference:
            ctx[f"{CONTEXT_PREFIX}reference"] = decision.history.reference

    sep = "&" if "?" in AI_STREAM_URL else "?"
    return escape(f"{AI_STREAM_URL}{sep}{urlencode(ctx)}")


async def notify_sms(number: str, reason: str):
    """The 'number captured, SMS sent' branch. Fire-and-forget: a missed call
    is already recorded in SQLite, so a failed SMS must not also fail the XML."""
    if not (SMS_WEBHOOK_URL and number):
        return
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            await client.post(SMS_WEBHOOK_URL, json={"number": number, "reason": reason})
    except Exception:
        pass


# --- XML for each destination ---------------------------------------------


def xml_ai(decision, params: dict, request: Request) -> Response:
    """The AI branch, in whichever mode is configured."""
    base = base_url(request)

    if AI_MODE == "stream" and AI_STREAM_URL:
        # A bare <Hangup/> after <Stream> means any socket failure ends the
        # call in about a second with nothing to hear. A spoken fallback makes
        # a backend outage audible instead of looking like a dropped call.
        return xml(
            f"""    <Stream bidirectional="true"
            keepCallAlive="true"
            contentType="{AI_CONTENT_TYPE}"
            statusCallbackUrl="{base}/stream-status"
            statusCallbackMethod="POST">
        {stream_url(decision, params)}
    </Stream>
    <Speak {SPEAK}>{escape(AI_FALLBACK_TEXT)}</Speak>
    <Hangup/>"""
        )

    # stub, or a mode with nothing configured to reach
    label = "repeat caller" if decision.pool == "ai_repeat" else "new caller"
    greeting = (
        f"Welcome back. You have called {decision.history.call_count} times before."
        if decision.history.known
        else "Thank you for calling. How can I help you today?"
    )
    return xml(
        f"""    <Speak {SPEAK}>{escape(greeting)}</Speak>
    <Speak {SPEAK}>Routing test. This call was sent to the A I pool as a {label}.</Speak>
    <Wait length="20"/>
    <Hangup/>"""
    )


def backend_payload(decision, params: dict) -> dict:
    """What the backend receives. Everything the platform sent, plus who is calling.

    `From` is overwritten with the resolved caller so the backend's existing
    handling works unchanged; `OriginalFrom` keeps whatever Vobiz actually sent
    (the SIM, on a forwarded call) so nothing is lost.
    """
    payload = dict(params)
    resolved = decision.identity.best()      # keeps the country code

    # Backends commonly select the agent from `To`, so rewriting it re-points
    # a different agent. Default is to pass `To` straight through: the DID the
    # citizen dialled is already bound to the right agent, and rewriting it to
    # the call. A number with no live agent behind it makes a backend accept the
    # websocket and close it immediately, which reads as a dropped call.
    #
    # Only set AI_TARGET_* when you specifically want this service to choose a
    # different agent than the dialled number implies (a repeat-caller agent,
    # say) — and only to identifiers with a live agent behind them.
    agent_number = AI_TARGET_REPEAT if decision.pool == "ai_repeat" else AI_TARGET_NEW
    if PROXY_REWRITE_TO and agent_number and agent_number != params.get("To", ""):
        payload["OriginalTo"] = params.get("To", "")
        payload["To"] = agent_number
        decision.considered.append(
            f"backend target re-pointed to {agent_number} (To rewritten)")

    if FORWARD_RESOLVED_FROM and resolved and decision.identity.confident:
        payload["OriginalFrom"] = params.get("From", "")
        payload["From"] = resolved
        payload["RouterFromRewritten"] = "true"
    else:
        payload["OriginalFrom"] = params.get("From", "")
        payload["RouterFromRewritten"] = "false"

    payload.update(
        {
            "RouterPool": decision.pool,
            "RouterCaller": resolved,
            "RouterCallerNormalised": decision.identity.number,
            "RouterIdentitySource": decision.identity.source,
            "RouterIdentified": str(decision.identity.confident).lower(),
            "RouterIdentityNote": decision.identity.note,
            "RouterRepeatCaller": str(decision.history.known).lower(),
            "RouterPriorCalls": str(decision.history.call_count),
            "RouterReference": decision.history.reference,
            "RouterSummary": decision.history.summary,
        }
    )
    return payload


def instrument_stream(backend_xml: str, request: Request) -> str:
    """Add our statusCallbackUrl to a <Stream> that has none.

    A backend's XML often sets no status callback, so the platform posts stream events to the
    literal string "no-stream-status-callback-url" and they are lost. Adding
    ours changes nothing about the call and makes stream failures visible here
    instead of only in the platform's own logs.
    """
    try:
        root = ET.fromstring(backend_xml)
    except ET.ParseError:
        return backend_xml          # not ours to fix; pass it through untouched

    base = base_url(request)
    changed = False
    for stream in root.iter("Stream"):
        if not stream.get("statusCallbackUrl"):
            stream.set("statusCallbackUrl", f"{base}/stream-status")
            stream.set("statusCallbackMethod", "POST")
            changed = True
    if not changed:
        return backend_xml
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode")


async def xml_ai_proxy(decision, params: dict, request: Request) -> Response:
    """Ask the backend's own answer URL for the XML and pass it straight through.

    Used when the backend exposes an answer URL rather than a stream endpoint.
    The routing context travels as extra POST fields, so the backend can see which
    agent it is being asked to be.
    """
    payload = backend_payload(decision, params)
    try:
        async with httpx.AsyncClient(timeout=AI_TIMEOUT_S) as client:
            r = await client.post(AI_ANSWER_URL, data=payload)
        if r.status_code == 200 and "<Response" in r.text:
            return Response(content=instrument_stream(r.text, request),
                            media_type="application/xml")
        reason = f"backend returned {r.status_code}"
    except Exception as exc:
        reason = f"backend unreachable: {type(exc).__name__}"

    # Fail loudly in the log, gracefully on the call.
    decision.considered.append(f"AI proxy failed ({reason}) -> falling back")
    return xml(
        f"""    <Speak {SPEAK}>We are unable to connect you right now. Please try again shortly.</Speak>
    <Hangup/>"""
    )


def xml_human(decision, params: dict, request: Request, agent: dict) -> Response:
    base = base_url(request)
    uuid = params.get("CallUUID", "")

    # Context for the agent's own callbacks. Vobiz prefixes these X-VH-.
    headers = {
        "Pool": sip_safe(decision.pool),
        "Caller": sip_safe(decision.identity.number),
        "Repeat": "true" if decision.history.known else "false",
    }
    if decision.history.reference:
        headers["Complaint"] = sip_safe(decision.history.reference)
    sip_headers = ",".join(f"{k}={v}" for k, v in headers.items() if v)

    caller_id_attr = f'callerId="{escape(CALLER_ID)}"\n          ' if CALLER_ID else ""
    if not CALLER_ID:
        decision.considered.append(
            "WARNING: no CALLER_ID set; Vobiz will derive it from the inbound leg, "
            "which on a forwarded call is not an owned number and the dial will fail"
        )

    # The <Speak> is not decoration. An inbound leg arrives at the answer URL
    # in a ringing state, and <Dial> needs an answered leg to bridge onto —
    # without something that answers first, the dial fails with
    # DialStatus=failed / ORIGINATOR_CANCEL and no useful error.
    # A registered SIP endpoint is dialled with <User>, a PSTN number with
    # <Number>. sipHeaders sit on the child element for <User> so they reach
    # the SIP B-leg as X-VH-* values.
    target = agent["number"]
    is_sip = target.lower().startswith("sip:")
    if is_sip:
        noun = f'<User sipHeaders="{escape(sip_headers)}">{escape(target)}</User>'
    else:
        noun = f"<Number>{escape(target)}</Number>"
    # Carried on the child for <User>, on the parent for <Number>. Setting both
    # would attach the same X-VH-* values twice.
    parent_headers = "" if is_sip else f'sipHeaders="{escape(sip_headers)}"\n          '

    return xml(
        f"""    <Speak {SPEAK}>Connecting you to an agent. Please hold.</Speak>
    <Dial {caller_id_attr}action="{base}/dial-action?call_uuid={quote(uuid)}"
          method="POST"
          callbackUrl="{base}/dial-callback?call_uuid={quote(uuid)}"
          callbackMethod="POST"
          {parent_headers}timeout="{DIAL_TIMEOUT}"
          timeLimit="{MAX_CALL_SECONDS}"
          redirect="false">
        {noun}
    </Dial>
    <Redirect>{base}/queue/{quote(uuid)}</Redirect>"""
    )


def xml_queue(decision, params: dict, request: Request) -> Response:
    base = base_url(request)
    uuid = params.get("CallUUID", "")
    cycle = decision.queue_cycle

    # Vobiz has no queue verb. Holding a caller means playing them something
    # and redirecting back here to be re-decided when it finishes — the hold
    # loop is this service's, not the platform's.
    hold = (
        f'    <Play loop="1">{escape(HOLD_MUSIC_URL)}</Play>'
        if HOLD_MUSIC_URL
        else f'    <Wait length="{CONFIG.hold_seconds}"/>'
    )
    intro = (
        f"    <Speak {SPEAK}>All our agents are currently busy. "
        f"Please stay on the line.</Speak>\n"
        if cycle == 0
        else ""
    )
    return xml(f"{intro}{hold}\n    <Redirect>{base}/queue/{quote(uuid)}</Redirect>")


def xml_reject(decision, answered: bool) -> Response:
    if answered:
        # Already answered (a queue timeout), so there is nothing to reject —
        # say goodbye and end it.
        return xml(
            f"""    <Speak {SPEAK}>We are sorry, all our agents are still busy. """
            f"""We have noted your number and will call you back.</Speak>
    <Hangup/>"""
        )
    # Never answered: ring briefly, then reject. Wait as the first element
    # holds the call ringing without answering, so this is not billed and the
    # caller's number is still captured from the CDR.
    return xml(
        """    <Wait length="3"/>
    <Hangup reason="rejected"/>"""
    )


# ===========================================================================
#  The answer URL
# ===========================================================================


@app.api_route("/answer", methods=["GET", "POST"])
async def answer(request: Request):
    started = time.perf_counter()
    params = await call_params(request)
    uuid = params.get("CallUUID", "") or f"sim-{int(time.time()*1000)}"

    try:
        identity = resolve_identity(params, CONFIG)

        # This lookup is now in the ringing critical path. Bound it hard: a
        # slow database must not become ring time the caller hears, so a
        # timeout falls through to "unknown caller" rather than failing.
        try:
            record = await asyncio.wait_for(
                asyncio.to_thread(history.lookup, identity.number, CONFIG), timeout=0.5
            )
        except (asyncio.TimeoutError, Exception):
            record = CallerHistory()

        call_context[uuid] = (identity, record)
        capacity = slots.snapshot(len(agents.available()))
        decision = decide(identity, record, capacity, CONFIG, queue_cycle=0)

        # Test-only: ?force=human|ai on the answer URL pins the branch, so a
        # specific path can be exercised without arranging capacity to trip it.
        # Ignored unless ALLOW_FORCE_ROUTE is on, so it cannot be used against
        # a production answer URL.
        forced = (params.get("force") or "").lower()
        if ALLOW_FORCE_ROUTE and forced in ("human", "ai"):
            decision.considered.append(f"FORCED to {forced} by ?force= (test mode)")
            decision.route = forced
            if forced == "ai":
                decision.pool = "ai_repeat" if decision.history.known else "ai_new"
            else:
                decision.pool = "human"

    except Exception as exc:
        # Fail open. A router that 500s takes the whole helpline down; a
        # router that falls back to AI degrades one feature.
        decision = decide(
            Identity("", "unknown", False, f"router error: {type(exc).__name__}"),
            CallerHistory(),
            slots.snapshot(len(agents.available())),
            CONFIG,
        )
        decision.considered.insert(0, f"ROUTER ERROR, failed open: {exc!r}")

    return await _fulfil(decision, params, request, uuid, started, answered=False)


@app.api_route("/queue/{call_uuid}", methods=["GET", "POST"])
async def queue_tick(call_uuid: str, request: Request):
    """A held caller comes back here each time the hold audio finishes."""
    started = time.perf_counter()
    params = await call_params(request)
    params.setdefault("CallUUID", call_uuid)

    cycle = queue_cycles.get(call_uuid, 0) + 1
    queue_cycles[call_uuid] = cycle

    # Reuse what /answer worked out for this call. Only fall back to
    # re-deriving it if this process never saw the original answer webhook.
    cached = call_context.get(call_uuid)
    if cached:
        identity, record = cached
    else:
        identity = resolve_identity(params, CONFIG)
        record = history.lookup(identity.number, CONFIG)

    capacity = slots.snapshot(len(agents.available()))
    decision = decide(identity, record, capacity, CONFIG, queue_cycle=cycle)

    # The caller is already holding a slot; re-pool it rather than reserving
    # a second one.
    return await _fulfil(decision, params, request, call_uuid, started,
                         answered=True, already_reserved=True)


async def _fulfil(decision, params: dict, request: Request, uuid: str, started: float,
                  answered: bool, already_reserved: bool = False):
    """Turn a Decision into XML, and make the bookkeeping match it."""
    route = decision.route

    if route == "ai":
        if already_reserved:
            slots.move(uuid, decision.pool)
            queue_cycles.pop(uuid, None)
        else:
            slots.reserve(uuid, decision.pool, decision.identity.number)
        history.record_call(decision.identity.number, uuid, decision.identity.source,
                            route, decision.pool, decision.reason)
        if AI_MODE == "proxy" and AI_ANSWER_URL:
            response = await xml_ai_proxy(decision, params, request)
        else:
            response = xml_ai(decision, params, request)
        # Logged after the proxy call so a backend failure shows in the trail.
        decisions.add(uuid, decision, (time.perf_counter() - started) * 1000, params)
        return response

    if route == "human":
        agent = agents.next_available()
        if not agent:
            # Lost the race between deciding and picking. Re-decide without
            # the human branch rather than dialling nobody.
            decision.considered.append("agent disappeared between decide and dial")
            decision.route, decision.pool = "queue", "queue"
            return await _fulfil(decision, params, request, uuid, started, answered,
                                 already_reserved)
        agents.set_status(agent["number"], busy=True)
        if already_reserved:
            slots.move(uuid, "human")
            queue_cycles.pop(uuid, None)
        else:
            slots.reserve(uuid, "human", decision.identity.number)
        history.record_call(decision.identity.number, uuid, decision.identity.source,
                            route, "human", decision.reason)
        decisions.add(uuid, decision, (time.perf_counter() - started) * 1000, params)
        return xml_human(decision, params, request, agent)

    if route == "queue":
        if not already_reserved:
            slots.reserve(uuid, "queue", decision.identity.number)
        decisions.add(uuid, decision, (time.perf_counter() - started) * 1000, params)
        return xml_queue(decision, params, request)

    # reject
    slots.release(uuid)
    queue_cycles.pop(uuid, None)
    missed_number = decision.identity.number or params.get("From", "")
    history.record_missed(missed_number, decision.reason)
    asyncio.create_task(notify_sms(missed_number, decision.reason))
    decisions.add(uuid, decision, (time.perf_counter() - started) * 1000, params)
    return xml_reject(decision, answered)


# ===========================================================================
#  Callbacks
# ===========================================================================


@app.api_route("/hangup", methods=["GET", "POST"])
async def hangup(request: Request):
    """Release the slot. Without this the pool silently shrinks to nothing."""
    params = await call_params(request)
    uuid = params.get("CallUUID", "")
    # Logged in full: when a call never reaches /answer, this webhook is the
    # only evidence of why.
    log.info("hangup %s", {k: v for k, v in params.items() if k in (
        "CallUUID", "From", "To", "Direction", "CallStatus", "Duration",
        "HangupCause", "HangupCauseName", "EndTime")} or params)
    slot = slots.release(uuid)
    queue_cycles.pop(uuid, None)
    call_context.pop(uuid, None)
    history.close_call(uuid)
    if slot and slot.pool == "human":
        for a in agents.all():
            agents.set_status(a["number"], busy=False)
    return JSONResponse({"ok": True, "released": bool(slot),
                         "pool": slot.pool if slot else None})


@app.api_route("/dial-action", methods=["GET", "POST"])
async def dial_action(request: Request):
    """Final result of the human leg. Elements after <Dial> only run when no
    bridge was established, so a no-answer lands back in the queue."""
    params = await call_params(request)
    uuid = request.query_params.get("call_uuid", "") or params.get("CallUUID", "")
    status = params.get("DialStatus", "")
    if status != "completed":
        for a in agents.all():
            agents.set_status(a["number"], busy=False)
        slots.move(uuid, "queue")
    return JSONResponse({"ok": True, "dial_status": status})


@app.api_route("/dial-callback", methods=["GET", "POST"])
async def dial_callback(request: Request):
    await call_params(request)
    return JSONResponse({"ok": True})


@app.api_route("/stream-status", methods=["GET", "POST"])
async def stream_status(request: Request):
    """Logged in full. When a <Stream> fails, this is the only place that says
    why — the call itself just ends and the hangup webhook blames the XML."""
    params = await call_params(request)
    log.info("stream-status %s", params)
    return JSONResponse({"ok": True})


# ===========================================================================
#  Mid-call escalation: AI -> human
# ===========================================================================


@app.post("/escalate")
async def escalate(request: Request):
    """Move a live AI call to a human.

    The transfer API is a redirect, not a bridge: the leg abandons the backend's
    XML and starts executing whatever /transfer-target returns.
    """
    body = await call_params(request)
    uuid = body.get("call_uuid") or body.get("CallUUID", "")
    if not uuid:
        return JSONResponse({"ok": False, "error": "call_uuid required"}, status_code=400)

    agent = agents.next_available()
    if not agent:
        return JSONResponse({"ok": False, "error": "no agent available"}, status_code=409)

    agents.set_status(agent["number"], busy=True)
    slots.move(uuid, "human")
    base = base_url(request)
    target = f"{base}/transfer-target?agent={quote(agent['number'])}"
    try:
        result = vobiz.transfer_call(uuid, target)
    except Exception as exc:
        # The transfer did not happen, so undo the bookkeeping that assumed it
        # would. Leaving the agent marked busy would quietly shrink the pool.
        agents.set_status(agent["number"], busy=False)
        slots.move(uuid, "ai_new")
        log.warning("escalation failed for %s: %r", uuid, exc)
        return JSONResponse(
            {"ok": False, "error": str(exc), "target": target}, status_code=502
        )
    return JSONResponse({"ok": True, "agent": agent["number"], "vobiz": result})


@app.api_route("/transfer-target", methods=["GET", "POST"])
async def transfer_target(request: Request):
    params = await call_params(request)
    agent = request.query_params.get("agent", "") or (
        AGENT_NUMBERS[0] if AGENT_NUMBERS else ""
    )
    base = base_url(request)
    uuid = params.get("CallUUID", "")
    caller_id_attr = f'callerId="{escape(CALLER_ID)}"\n          ' if CALLER_ID else ""
    return xml(
        f"""    <Speak {SPEAK}>Transferring you to a human agent now.</Speak>
    <Dial {caller_id_attr}action="{base}/dial-action?call_uuid={quote(uuid)}"
          method="POST"
          timeout="{DIAL_TIMEOUT}"
          redirect="false">
        <Number>{escape(agent)}</Number>
    </Dial>
    <Speak {SPEAK}>The transfer could not be completed.</Speak>
    <Hangup/>"""
    )


# ===========================================================================
#  Operations
# ===========================================================================


@app.api_route("/agents", methods=["GET", "POST"])
async def agents_endpoint(request: Request):
    if request.method == "POST":
        body = await call_params(request)
        number = body.get("number", "")
        if not number:
            return JSONResponse({"ok": False, "error": "number required"}, status_code=400)
        if body.get("register", "").lower() == "true" or number not in [
            a["number"] for a in agents.all()
        ]:
            agents.register(number, body.get("name", ""))
        online = body.get("online")
        busy = body.get("busy")
        agents.set_status(
            number,
            online=None if online is None else str(online).lower() == "true",
            busy=None if busy is None else str(busy).lower() == "true",
        )
    return JSONResponse({"agents": agents.all(), "available": len(agents.available())})


@app.post("/reference")
async def reference(request: Request):
    """Where the backend writes back at the end of a conversation, so the next call
    from this number can be routed as a repeat caller with context."""
    body = await call_params(request)
    number = normalise(body.get("number", ""))
    if not number:
        return JSONResponse({"ok": False, "error": "number required"}, status_code=400)
    history.set_reference(number, body.get("reference", ""), body.get("summary", ""))
    return JSONResponse({"ok": True, "number": number})


@app.get("/state")
async def state():
    slots.sweep()
    cap = slots.snapshot(len(agents.available()))
    return JSONResponse(
        {
            "policy": CONFIG.policy,
            "inbound_mode": CONFIG.inbound_mode,
            "ai_mode": AI_MODE,
            "capacity": {
                "ai": {"in_use": cap.ai_in_use, "cap": CONFIG.ai_capacity,
                       "free": cap.ai_free(CONFIG)},
                "human": {"in_use": cap.human_in_use, "cap": CONFIG.human_capacity,
                          "free": cap.human_free(CONFIG),
                          "agents_free": cap.human_agents_free,
                          "agents_registered": len(agents.all())},
                "queue": {"in_use": cap.queued, "cap": CONFIG.queue_capacity,
                          "free": cap.queue_free(CONFIG)},
            },
            "leaked_slots_reclaimed": slots.leaked,
            "live_calls": slots.live(),
            "agents": agents.all(),
            "history": history.stats(),
        }
    )


@app.get("/decisions")
async def decisions_endpoint(n: int = 50):
    return JSONResponse({"decisions": decisions.recent(n)})


@app.api_route("/probe", methods=["GET", "POST"])
async def probe(request: Request):
    """A-leg holder for the self-test in call.py --self-test.

    Calling one of your own DIDs makes that DID run its inbound application.
    This keeps the originating leg alive and silent while that happens, so the
    inbound routing decision is the only thing under test.
    """
    params = await call_params(request)
    log.info("probe leg: %s", {k: params.get(k) for k in
                               ("CallUUID", "From", "To", "Direction", "CallStatus")})
    return xml('    <Wait length="45"/>\n    <Hangup/>')


@app.get("/health")
async def health():
    return {
        "ok": True,
        "public_url": PUBLIC_URL or None,
        "answer_url": f"{PUBLIC_URL}/answer" if PUBLIC_URL else None,
        "hangup_url": f"{PUBLIC_URL}/hangup" if PUBLIC_URL else None,
        "ai_mode": AI_MODE,
        "caller_id_set": bool(CALLER_ID),
        "agents": len(agents.all()),
    }


@app.post("/reset")
async def reset(wipe_history: bool = False):
    for s in slots.live():
        slots.release(s["call_uuid"])
    queue_cycles.clear()
    decisions.clear()
    for a in agents.all():
        agents.set_status(a["number"], online=True, busy=False)
    if wipe_history:
        history.reset()
    return {"ok": True, "history_wiped": wipe_history}


@app.get("/", response_class=HTMLResponse)
async def console():
    return (Path(__file__).parent / "console.html").read_text()


@app.on_event("startup")
async def _sweeper():
    async def loop():
        while True:
            await asyncio.sleep(60)
            slots.sweep()
            # Context for calls that are no longer live would otherwise leak.
            live = {c["call_uuid"] for c in slots.live()}
            for uuid in [u for u in call_context if u not in live]:
                call_context.pop(uuid, None)

    asyncio.create_task(loop())


if __name__ == "__main__":
    import uvicorn

    print(f"  answer URL   {PUBLIC_URL or 'http://127.0.0.1:' + str(PORT)}/answer")
    print(f"  hangup URL   {PUBLIC_URL or 'http://127.0.0.1:' + str(PORT)}/hangup")
    print(f"  console      http://127.0.0.1:{PORT}/")
    print(f"  policy       {CONFIG.policy}  |  AI mode {AI_MODE}  |  "
          f"caps ai={CONFIG.ai_capacity} human={CONFIG.human_capacity} "
          f"queue={CONFIG.queue_capacity}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)

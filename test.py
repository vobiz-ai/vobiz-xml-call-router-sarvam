"""
test.py — exercise the router without a phone
==============================================
Every branch is reachable over HTTP, so routing can be proven in seconds rather
than by dialling repeatedly.

    python test.py verify           assert every branch; exits non-zero on failure
    python test.py sim all          the same paths, narrated
    python test.py sim identity     how caller identity resolves
    python test.py sim fill         where each capacity threshold trips
    python test.py sim repeat       first-time vs returning caller
    python test.py sim queue        a held caller promoted when a slot frees
    python test.py mock             stand in for a backend on :8091
    python test.py call --to +91…   place a real call through the router

    --base http://127.0.0.1:8090    the running router
    --no-diversion                  simulate a carrier that strips Diversion
"""

from __future__ import annotations

import argparse
import itertools
import os
import sys
import xml.etree.ElementTree as ET

import requests

BASE = "http://127.0.0.1:8090"
COUNTER = itertools.count(1)

# Synthetic numbers. Real subscriber numbers have no place in a repository.
FORWARDER = "919999900001"      # the line that forwards, under sim_forward
DIALLED = "918888800001"        # the number being called
CITIZEN = "917777700001"        # a caller
# Sections that must see a first-time caller use their own number: history
# is SQLite and survives reset() unless wipe_history is passed, so reusing
# CITIZEN after the returning-caller section would route to ai_repeat.
FRESH_CALLER = "917777700002"

G, Y, R, B, DIM, OFF = "\033[32m", "\033[33m", "\033[31m", "\033[34m", "\033[2m", "\033[0m"
COLOUR = {"ai": G, "human": B, "queue": Y, "reject": R}


# ---------------------------------------------------------------------------
# Talking to the router
# ---------------------------------------------------------------------------


def state() -> dict:
    try:
        return requests.get(f"{BASE}/state", timeout=5).json()
    except requests.RequestException as exc:
        sys.exit(f"{R}Cannot reach the router at {BASE} — is it running? ({exc}){OFF}")


def capacity() -> dict:
    return state()["capacity"]


def reset(wipe_history: bool = False):
    requests.post(f"{BASE}/reset", params={"wipe_history": str(wipe_history).lower()},
                  timeout=5)


def answer(uuid: str, citizen: str = "", diversion: bool = True, **extra):
    payload = {"CallUUID": uuid, "From": FORWARDER, "To": DIALLED,
               "Direction": "inbound", "CallStatus": "ringing", "Event": "StartApp"}
    # ForwardedFrom is present only when the carrier emitted a SIP Diversion
    # header. It is omitted entirely when absent — never sent empty.
    if citizen and diversion:
        payload["ForwardedFrom"] = citizen
    payload.update(extra)
    return requests.post(f"{BASE}/answer", data=payload, timeout=10)


def last_decision() -> dict:
    return requests.get(f"{BASE}/decisions", params={"n": 1}, timeout=5).json()["decisions"][0]


def hangup(uuid: str):
    requests.post(f"{BASE}/hangup", data={"CallUUID": uuid}, timeout=5)


def call(citizen: str, diversion: bool = True, uuid: str = "") -> dict:
    cuid = uuid or f"sim-{next(COUNTER):04d}"
    answer(cuid, citizen, diversion)
    d = last_decision()
    d["_uuid"] = cuid
    return d


def fill(pool: str, prefix: str):
    """Saturate one pool, reading capacity back rather than assuming it."""
    guard = 0
    while capacity()[pool]["free"] > 0 and guard < 500:
        guard += 1
        answer(f"{prefix}-{guard}", f"9177{guard:08d}")


# ---------------------------------------------------------------------------
# verify — assertions
# ---------------------------------------------------------------------------


def ai_shape() -> list[str] | None:
    """The AI branch emits different verbs per AI_MODE. Read the mode rather
    than hardcoding one shape and reporting a false failure in the others. In
    proxy mode the XML is the backend's, so its verbs are not ours to assert."""
    return {
        "stream": ["Stream", "Speak", "Hangup"],
        "proxy": None,
    }.get(state()["ai_mode"], ["Speak", "Speak", "Wait", "Hangup"])


def cmd_verify() -> int:
    failures: list[str] = []
    n = 0

    def expect(label: str, r, route: str, pool: str, shape):
        nonlocal n
        n += 1
        d = last_decision()
        try:
            got = [c.tag for c in ET.fromstring(r.text)]
        except ET.ParseError as exc:
            failures.append(f"{label}: XML did not parse — {exc}")
            print(f"  {R}FAIL{OFF}  {label}  (malformed XML)")
            return
        problems = []
        if d["route"] != route:
            problems.append(f"route {d['route']!r} != {route!r}")
        if d["pool"] != pool:
            problems.append(f"pool {d['pool']!r} != {pool!r}")
        if shape is not None and got != shape:
            problems.append(f"verbs {got} != {shape}")
        if problems:
            failures.append(f"{label}: " + "; ".join(problems))
            print(f"  {R}FAIL{OFF}  {label:<28} {'; '.join(problems)}")
        else:
            print(f"  {G}ok{OFF}    {label:<28} {DIM}{route}/{pool}  {' '.join(got)}{OFF}")

    print("\nIdentity")
    reset(wipe_history=True)
    expect("carrier sends Diversion", answer("id-1", CITIZEN), "ai", "ai_new", ai_shape())
    if last_decision()["identity"]["source"] != "ForwardedFrom":
        failures.append("expected ForwardedFrom to identify the caller")

    expect("carrier strips Diversion", answer("id-2"), "ai", "ai_new", ai_shape())
    d = last_decision()
    n += 1
    if d["identity"]["confident"]:
        failures.append("identity should not be trusted without ForwardedFrom")
        print(f"  {R}FAIL{OFF}  identity wrongly trusted")
    else:
        print(f"  {G}ok{OFF}    {'identity correctly untrusted':<28} "
              f"{DIM}{d['identity']['note'][:40]}…{OFF}")

    print("\nReturning caller")
    reset(wipe_history=True)
    answer("rc-1", CITIZEN)
    requests.post(f"{BASE}/reference", data={"number": CITIZEN, "reference": "REF-2291"},
                  timeout=5)
    hangup("rc-1")
    expect("second call -> repeat agent", answer("rc-2", CITIZEN), "ai", "ai_repeat",
           ai_shape())

    print("\nCapacity cascade")
    reset()
    fill("ai", "cap-ai")
    expect("AI full -> human", answer("ov-1", CITIZEN), "human", "human",
           ["Speak", "Dial", "Redirect"])
    fill("human", "cap-hu")
    expect("human full -> queue", answer("ov-2", CITIZEN), "queue", "queue",
           ["Speak", "Wait", "Redirect"])
    expect("later hold cycle (no Speak)",
           requests.post(f"{BASE}/queue/ov-2", data={"CallUUID": "ov-2"}, timeout=10),
           "queue", "queue", ["Wait", "Redirect"])
    fill("queue", "cap-q")
    expect("queue full -> reject", answer("ov-3", CITIZEN), "reject", "none",
           ["Wait", "Hangup"])

    print("\nQueue behaviour")
    reset()
    fill("ai", "q-ai")
    fill("human", "q-hu")
    answer("held", FRESH_CALLER)
    hangup("q-ai-1")                      # free one AI slot
    expect("promoted when slot frees",
           requests.post(f"{BASE}/queue/held", data={"CallUUID": "held"}, timeout=10),
           "ai", "ai_new", ai_shape())
    n += 1
    if last_decision()["identity"]["source"] != "ForwardedFrom":
        failures.append("identity was lost across the hold")
        print(f"  {R}FAIL{OFF}  identity lost across the hold")
    else:
        print(f"  {G}ok{OFF}    {'identity survives the hold':<28} "
              f"{DIM}cached at /answer{OFF}")

    print("\nQueue timeout")
    reset()
    fill("ai", "t-ai")
    fill("human", "t-hu")
    answer("timeout", CITIZEN)
    for _ in range(3):
        r = requests.post(f"{BASE}/queue/timeout", data={"CallUUID": "timeout"}, timeout=10)
    expect("gives up after max cycles", r, "reject", "none", ["Speak", "Hangup"])

    print("\nSlot accounting")
    reset()
    before = capacity()["ai"]["in_use"]
    answer("acct-1", CITIZEN)
    mid = capacity()["ai"]["in_use"]
    hangup("acct-1")
    after = capacity()["ai"]["in_use"]
    n += 1
    if (before, mid, after) == (0, 1, 0):
        print(f"  {G}ok{OFF}    {'reserve then release':<28} {DIM}0 -> 1 -> 0{OFF}")
    else:
        failures.append(f"slot accounting: {before} -> {mid} -> {after}, expected 0 -> 1 -> 0")
        print(f"  {R}FAIL{OFF}  slot accounting {before} -> {mid} -> {after}")

    print("\nEscalation rolls back on failure")
    reset()
    answer("esc-1", CITIZEN)
    free_before = capacity()["human"]["agents_free"]
    resp = requests.post(f"{BASE}/escalate", data={"call_uuid": "esc-1"}, timeout=15)
    n += 1
    if resp.status_code == 200:
        print(f"  {G}ok{OFF}    {'transfer accepted':<28} {DIM}credentials present{OFF}")
    elif resp.status_code == 502 and capacity()["human"]["agents_free"] == free_before:
        print(f"  {G}ok{OFF}    {'agent released on failure':<28} {DIM}502, pool intact{OFF}")
    else:
        failures.append(f"escalation rollback: HTTP {resp.status_code}")
        print(f"  {R}FAIL{OFF}  escalation rollback")

    reset(wipe_history=True)
    print(f"\n{'=' * 60}")
    if failures:
        print(f"{R}{len(failures)} of {n} checks failed{OFF}")
        for f in failures:
            print(f"  · {f}")
        return 1
    print(f"{G}all {n} checks passed{OFF}\n")
    return 0


# ---------------------------------------------------------------------------
# sim — narrated
# ---------------------------------------------------------------------------


def show(d: dict, note: str = ""):
    c = COLOUR.get(d.get("route", ""), "")
    i = d.get("identity", {})
    who = i.get("number") or "unidentified"
    trust = "" if i.get("confident") else f" {DIM}(untrusted){OFF}"
    print(f"  {c}{d.get('route','?'):<7}{OFF} {d.get('pool',''):<10} {who:<12} "
          f"{DIM}via {i.get('source','?')}{OFF}{trust} "
          f"{DIM}{d.get('elapsed_ms','?')}ms{OFF} {note}")
    for step in d.get("considered", []):
        print(f"      {DIM}· {step}{OFF}")


def banner(title: str):
    print(f"\n{'=' * 72}\n  {title}\n{'=' * 72}")


def sim_identity():
    banner("IDENTITY — can the router tell who is calling?")
    reset(wipe_history=True)
    print(f"  inbound_mode = {state()['inbound_mode']}\n")
    print("  Carrier PASSES the Diversion header:")
    d = call(CITIZEN, True); show(d); hangup(d["_uuid"])
    print("\n  Carrier STRIPS the Diversion header:")
    d = call(CITIZEN, False); show(d); hangup(d["_uuid"])
    print(f"\n  {Y}Without ForwardedFrom every caller presents as the same number,{OFF}")
    print("  so returning-caller routing cannot run at all.")


def sim_fill(total: int, diversion: bool):
    banner(f"CAPACITY — {total} concurrent calls against the thresholds")
    reset()
    c = capacity()
    print(f"  policy={state()['policy']}  ai={c['ai']['cap']}  human={c['human']['cap']} "
          f"({c['human']['agents_free']} agents free)  queue={c['queue']['cap']}\n")
    seen: dict[str, int] = {}
    for i in range(total):
        d = call(f"9177{i:08d}", diversion)
        if d.get("route") not in seen:
            seen[d["route"]] = i + 1
            show(d, f"{DIM}<- first call here (#{i + 1}){OFF}")
    print()
    c = capacity()
    for pool in ("ai", "human", "queue"):
        print(f"  {pool:<6} {c[pool]['in_use']:>3}/{c[pool]['cap']:<3} in use")
    print("\n  first call to each branch:  "
          + "  ".join(f"{COLOUR.get(k,'')}{k}{OFF}=#{v}" for k, v in seen.items()))


def sim_repeat(diversion: bool):
    banner("RETURNING CALLER — does the second call route differently?")
    reset(wipe_history=True)
    print("  First call:")
    d1 = call(CITIZEN, diversion); show(d1)
    requests.post(f"{BASE}/reference",
                  data={"number": CITIZEN, "reference": "REF-2291"}, timeout=5)
    hangup(d1["_uuid"])
    print(f"\n  {DIM}backend posted reference REF-2291 back to /reference{OFF}")
    print("\n  Same caller again:")
    d2 = call(CITIZEN, diversion); show(d2); hangup(d2["_uuid"])
    if d2.get("pool") == "ai_repeat":
        print(f"\n  {G}Routed to the returning-caller agent, reference attached.{OFF}")
    else:
        print(f"\n  {Y}Not recognised — nothing to key history on.{OFF}")


def sim_queue(diversion: bool):
    banner("QUEUE — a caller held, then promoted when a channel frees")
    reset()
    c = capacity()
    total = c["ai"]["cap"] + min(c["human"]["cap"], c["human"]["agents_free"])
    print(f"  Filling every channel ({total} calls)...")
    live = [call(f"9166{i:08d}", diversion)["_uuid"] for i in range(total)]
    print("  One more caller arrives:")
    d = call(CITIZEN, diversion); show(d)
    if d.get("route") != "queue":
        print(f"  {Y}Expected a queue here.{OFF}")
        return
    print(f"\n  {DIM}One call hangs up, freeing a channel...{OFF}")
    hangup(live[0])
    print("  The hold audio finishes and the caller returns to /queue:")
    requests.post(f"{BASE}/queue/{d['_uuid']}", data={"CallUUID": d["_uuid"]}, timeout=10)
    show(last_decision())


# ---------------------------------------------------------------------------
# mock — stand in for a backend
# ---------------------------------------------------------------------------


def cmd_mock(port: int):
    """Print every field the router forwards, and reply with valid XML."""
    from urllib.parse import parse_qsl

    from fastapi import FastAPI, Request
    from fastapi.responses import Response
    import uvicorn

    mock = FastAPI()

    @mock.post("/answer")
    async def backend_answer(request: Request):
        body = dict(parse_qsl((await request.body()).decode()))
        print("\n=== what the backend receives ===")
        for k in sorted(body):
            print(f"  {k:<24} {body[k]}")
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
            '    <Speak>Mock backend speaking.</Speak>\n</Response>',
            media_type="application/xml")

    print(f"  mock backend on http://127.0.0.1:{port}/answer")
    print(f"  point AI_ANSWER_URL at it, set AI_MODE=proxy, and place a call")
    uvicorn.run(mock, host="127.0.0.1", port=port, log_level="warning")


# ---------------------------------------------------------------------------
# call — a real call through the router
# ---------------------------------------------------------------------------


def cmd_call(to: str, forwarded_from: str):
    """Place one real outbound call whose answer URL is the router."""
    import app as router_app

    health = requests.get(f"{BASE}/health", timeout=5).json()
    public = health.get("public_url")
    if not public:
        sys.exit("The router has no PUBLIC_URL — start a tunnel and set it in .env.")

    answer_url = f"{public}/answer"
    if forwarded_from:
        answer_url += f"?ForwardedFrom={forwarded_from}"
    print(f"  answer_url  {answer_url}\n  hangup_url  {public}/hangup")
    try:
        print(f"  response    {router_app.place_call(to, answer_url, f'{public}/hangup')}")
    except router_app.VobizError as exc:
        sys.exit(str(exc))
    print(f"\n  Watch it land: {BASE}/")


# ---------------------------------------------------------------------------


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["verify", "sim", "mock", "call"])
    ap.add_argument("scenario", nargs="?", default="all",
                    choices=["all", "identity", "fill", "repeat", "queue"])
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--calls", type=int, default=95)
    ap.add_argument("--no-diversion", action="store_true")
    ap.add_argument("--to", default=os.getenv("TO_NUMBER", ""))
    ap.add_argument("--forwarded-from", default="")
    ap.add_argument("--port", type=int, default=8091)
    args = ap.parse_args()

    BASE = args.base.rstrip("/")
    div = not args.no_diversion

    if args.command == "mock":
        return cmd_mock(args.port) or 0
    if args.command == "call":
        if not args.to:
            sys.exit("Pass --to, or set TO_NUMBER in .env")
        return cmd_call(args.to, args.forwarded_from) or 0

    state()  # fail fast with a clear message if the router is not running

    if args.command == "verify":
        return cmd_verify()

    if args.scenario in ("identity", "all"):
        sim_identity()
    if args.scenario in ("fill", "all"):
        sim_fill(args.calls, div)
    if args.scenario in ("repeat", "all"):
        sim_repeat(div)
    if args.scenario in ("queue", "all"):
        sim_queue(div)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

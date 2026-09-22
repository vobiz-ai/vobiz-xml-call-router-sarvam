"""
sim.py — drive the router without placing real calls
=====================================================
Every branch of the routing layer is reachable over HTTP, so the thresholds,
the queue and the repeat-caller logic can be proven deterministically in a few
seconds instead of by dialling a hundred times.

    python sim.py identity          how caller identity resolves, with and
                                    without a carrier Diversion header
    python sim.py fill              ramp calls until AI fills, overflows to
                                    human, queues, then rejects
    python sim.py repeat            first-time vs repeat caller routing
    python sim.py queue             a held caller promoted when a slot frees
    python sim.py escalate-dry      what an AI -> human escalation would do
    python sim.py all

    --base http://127.0.0.1:8090    the running router
    --no-diversion                  simulate a carrier that strips Diversion
                                    (i.e. Jio/Vi rather than Airtel)
"""

from __future__ import annotations

import argparse
import itertools
import sys
import uuid as uuidlib

import requests

BASE = "http://127.0.0.1:8090"
COUNTER = itertools.count(1)

G, Y, R, B, DIM, OFF = "\033[32m", "\033[33m", "\033[31m", "\033[34m", "\033[2m", "\033[0m"
COLOUR = {"ai": G, "human": B, "queue": Y, "reject": R}


def _die(msg: str):
    sys.exit(f"{R}{msg}{OFF}")


def state() -> dict:
    try:
        return requests.get(f"{BASE}/state", timeout=5).json()
    except requests.RequestException as exc:
        _die(f"Cannot reach the router at {BASE} — is `python app.py` running? ({exc})")


def reset(wipe_history: bool = False):
    requests.post(f"{BASE}/reset", params={"wipe_history": str(wipe_history).lower()},
                  timeout=5)


def call(citizen: str, sim_number: str = "919999900001", diversion: bool = True,
         call_uuid: str = "") -> dict:
    """One synthetic inbound call, shaped exactly like a Vobiz answer_url POST."""
    cuid = call_uuid or f"sim-{next(COUNTER):04d}-{uuidlib.uuid4().hex[:8]}"
    payload = {
        "CallUUID": cuid,
        "From": sim_number,          # the SIM, because the call was forwarded
        "To": "918888800001",
        "Direction": "inbound",
        "CallStatus": "ringing",
        "Event": "StartApp",
    }
    # ForwardedFrom is present only when the carrier emitted a SIP Diversion
    # header. It is omitted entirely when absent — never sent empty.
    if diversion:
        payload["ForwardedFrom"] = citizen

    r = requests.post(f"{BASE}/answer", data=payload, timeout=10)
    latest = requests.get(f"{BASE}/decisions", params={"n": 1}, timeout=5).json()
    decision = (latest.get("decisions") or [{}])[0]
    decision["_xml"] = r.text
    decision["_call_uuid"] = cuid
    return decision


def hangup(call_uuid: str):
    requests.post(f"{BASE}/hangup", data={"CallUUID": call_uuid}, timeout=5)


def line(d: dict, note: str = ""):
    c = COLOUR.get(d.get("route", ""), "")
    ident = d.get("identity", {})
    who = ident.get("number") or "unidentified"
    src = ident.get("source", "?")
    mark = "" if ident.get("confident") else f" {DIM}(untrusted){OFF}"
    print(f"  {c}{d.get('route','?'):<7}{OFF} {d.get('pool',''):<10} "
          f"{who:<12} {DIM}via {src}{OFF}{mark} "
          f"{DIM}{d.get('elapsed_ms','?')}ms{OFF} {note}")


def show_trail(d: dict):
    for step in d.get("considered", []):
        print(f"      {DIM}· {step}{OFF}")


def banner(title: str):
    print(f"\n{'=' * 74}\n  {title}\n{'=' * 74}")


# ---------------------------------------------------------------------------


def scenario_identity(diversion: bool):
    banner("IDENTITY — can the router tell who is calling?")
    reset(wipe_history=True)
    s = state()
    print(f"  inbound_mode = {s['inbound_mode']}\n")

    print("  Carrier PASSES the Diversion header (Airtel behaviour):")
    d = call("919812345678", diversion=True)
    line(d)
    show_trail(d)
    hangup(d["_call_uuid"])

    print("\n  Carrier STRIPS the Diversion header (Jio/Vi behaviour):")
    d = call("919812345678", diversion=False)
    line(d)
    show_trail(d)
    hangup(d["_call_uuid"])

    print(f"\n  {Y}This is the SIM bottleneck, measured rather than argued:{OFF}")
    print("  without ForwardedFrom every citizen presents as the same SIM number,")
    print("  so repeat-caller routing cannot run at all. A direct DID removes the")
    print("  dependency entirely (set INBOUND_MODE=direct_did).")


def scenario_fill(total: int, diversion: bool):
    banner(f"CAPACITY — {total} concurrent calls against the thresholds")
    reset()
    s = state()
    cap = s["capacity"]
    print(f"  policy={s['policy']}  ai={cap['ai']['cap']}  human={cap['human']['cap']}"
          f"  (agents free {cap['human']['agents_free']})  queue={cap['queue']['cap']}\n")

    if cap["human"]["agents_free"] == 0:
        print(f"  {Y}No agents online — the human branch cannot be reached.{OFF}")
        print(f"  {DIM}Register some: python sim.py agents --add 919812345678,919812345679{OFF}\n")

    seen: dict[str, int] = {}
    for i in range(total):
        d = call(f"9198{i:08d}", diversion=diversion)
        route = d.get("route", "?")
        if route not in seen:
            # Print the call where each branch first trips, and its reasoning.
            seen[route] = i + 1
            line(d, note=f"{DIM}<- first call routed here (#{i + 1}){OFF}")
            show_trail(d)

    print()
    cap = state()["capacity"]
    for pool in ("ai", "human", "queue"):
        p = cap[pool]
        print(f"  {pool:<6} {p['in_use']:>3}/{p['cap']:<3} in use")
    print(f"\n  first call to each branch: "
          + "  ".join(f"{COLOUR.get(k,'')}{k}{OFF}=#{v}" for k, v in seen.items()))


def scenario_repeat(diversion: bool):
    banner("REPEAT CALLER — does the second call route differently?")
    reset(wipe_history=True)
    citizen = "919812345678"

    print("  First call from this citizen:")
    d1 = call(citizen, diversion=diversion)
    line(d1)
    show_trail(d1)

    # the backend writes a reference back at the end of the conversation.
    requests.post(f"{BASE}/reference",
                  data={"number": citizen, "reference": "REF-2291",
                        "summary": "Water supply disruption, ward 14"},
                  timeout=5)
    hangup(d1["_call_uuid"])
    print(f"\n  {DIM}the backend posted reference REF-2291 back to /reference{OFF}")

    print("\n  Same citizen calls again:")
    d2 = call(citizen, diversion=diversion)
    line(d2)
    show_trail(d2)
    hangup(d2["_call_uuid"])

    if d2.get("pool") == "ai_repeat":
        print(f"\n  {G}Routed to the repeat-caller agent with the reference attached.{OFF}")
    else:
        print(f"\n  {Y}Not recognised as a repeat caller.{OFF}")
        if not diversion:
            print("  Expected: with no Diversion header there is nothing to key history on.")


def scenario_queue(diversion: bool):
    banner("QUEUE — a caller held, then promoted when a channel frees")
    reset()
    s = state()
    ai_cap = s["capacity"]["ai"]["cap"]
    human_cap = s["capacity"]["human"]["cap"]
    agents_free = s["capacity"]["human"]["agents_free"]
    to_fill = ai_cap + min(human_cap, agents_free)

    print(f"  Filling every channel ({to_fill} calls)...")
    live = [call(f"9199{i:08d}", diversion=diversion)["_call_uuid"] for i in range(to_fill)]

    print("  One more caller arrives:")
    d = call("917777700001", diversion=diversion)
    line(d)
    show_trail(d)
    if d.get("route") != "queue":
        print(f"  {Y}Expected a queue here; check agent registration.{OFF}")
        return

    print(f"\n  {DIM}One AI call hangs up, freeing a channel...{OFF}")
    hangup(live[0])

    print("  The held caller's hold audio finishes and it comes back to /queue:")
    # Deliberately sends only CallUUID and the SIM's From — no ForwardedFrom.
    # The caller must still be recognised, from context cached at /answer.
    r = requests.post(f"{BASE}/queue/{d['_call_uuid']}",
                      data={"CallUUID": d["_call_uuid"], "From": "919999900001"},
                      timeout=10)
    latest = requests.get(f"{BASE}/decisions", params={"n": 1}, timeout=5).json()
    d2 = (latest.get("decisions") or [{}])[0]
    line(d2)
    show_trail(d2)
    print(f"\n  {DIM}XML returned:{OFF}")
    for ln in r.text.strip().splitlines():
        print(f"      {DIM}{ln}{OFF}")


def scenario_escalate(diversion: bool):
    banner("ESCALATION — AI to human mid-call (dry run)")
    reset()
    d = call("919812345678", diversion=diversion)
    line(d, note="in progress with the AI")
    print(f"\n  {DIM}POST /escalate {{\"call_uuid\": \"{d['_call_uuid']}\"}}{OFF}")
    r = requests.post(f"{BASE}/escalate", data={"call_uuid": d["_call_uuid"]}, timeout=15)
    try:
        body = r.json()
    except ValueError:
        body = {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    if body.get("ok"):
        print(f"  {G}Transfer issued to agent {body['agent']}{OFF}  "
              f"(Vobiz {body.get('vobiz', {}).get('status')})")
    else:
        print(f"  {Y}{body.get('error')}{OFF}")
        print(f"  {DIM}A real CallUUID and Vobiz credentials are needed for the "
              f"transfer itself; the pool move and XML are exercised regardless.{OFF}")
    xml = requests.get(f"{BASE}/transfer-target", params={"agent": "919812345678"},
                       timeout=5).text
    print(f"\n  {DIM}XML the transferred leg would execute:{OFF}")
    for ln in xml.strip().splitlines():
        print(f"      {DIM}{ln}{OFF}")


def cmd_agents(add: str):
    for number in [n.strip() for n in add.split(",") if n.strip()]:
        requests.post(f"{BASE}/agents",
                      data={"number": number, "register": "true", "online": "true",
                            "busy": "false"},
                      timeout=5)
    print(requests.get(f"{BASE}/agents", timeout=5).json())


def main():
    global BASE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario",
                    choices=["identity", "fill", "repeat", "queue", "escalate-dry",
                             "agents", "all"])
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--calls", type=int, default=95)
    ap.add_argument("--no-diversion", action="store_true",
                    help="simulate a carrier that strips the SIP Diversion header")
    ap.add_argument("--add", default="", help="agents to register, comma separated")
    args = ap.parse_args()

    BASE = args.base.rstrip("/")
    diversion = not args.no_diversion
    state()  # fail fast with a clear message if the router is not running

    if args.scenario == "agents":
        return cmd_agents(args.add)
    if args.scenario in ("identity", "all"):
        scenario_identity(diversion)
    if args.scenario in ("fill", "all"):
        scenario_fill(args.calls, diversion)
    if args.scenario in ("repeat", "all"):
        scenario_repeat(diversion)
    if args.scenario in ("queue", "all"):
        scenario_queue(diversion)
    if args.scenario in ("escalate-dry", "all"):
        scenario_escalate(diversion)
    print()


if __name__ == "__main__":
    main()

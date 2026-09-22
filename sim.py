"""
sim.py — drive the router with fake calls
==========================================
Every branch is reachable over HTTP, so routing can be watched without dialling
anything. This is the narrated view; verify.py asserts the same paths and exits
non-zero on failure — use that in CI, this to see what is happening.

    python sim.py identity          how caller identity resolves, with and
                                    without a carrier Diversion header
    python sim.py fill              where each capacity threshold trips
    python sim.py repeat            first-time vs returning caller
    python sim.py queue             a held caller promoted when a slot frees
    python sim.py all

    --base http://127.0.0.1:8090    the running router
    --no-diversion                  simulate a carrier that strips Diversion
"""

from __future__ import annotations

import argparse
import itertools
import sys

import requests

BASE = "http://127.0.0.1:8090"
COUNTER = itertools.count(1)

# Synthetic numbers. Never put real subscriber numbers in a repository.
FORWARDER = "919999900001"      # the line that forwards, when INBOUND_MODE=sim_forward
DIALLED = "918888800001"        # the number being called
CITIZEN = "917777700001"        # a caller

G, Y, R, B, DIM, OFF = "\033[32m", "\033[33m", "\033[31m", "\033[34m", "\033[2m", "\033[0m"
COLOUR = {"ai": G, "human": B, "queue": Y, "reject": R}


def state() -> dict:
    try:
        return requests.get(f"{BASE}/state", timeout=5).json()
    except requests.RequestException as exc:
        sys.exit(f"{R}Cannot reach the router at {BASE} — is it running? ({exc}){OFF}")


def reset(wipe_history: bool = False):
    requests.post(f"{BASE}/reset", params={"wipe_history": str(wipe_history).lower()},
                  timeout=5)


def call(citizen: str, diversion: bool = True, uuid: str = "") -> dict:
    """One synthetic inbound call, shaped like a platform answer_url POST."""
    cuid = uuid or f"sim-{next(COUNTER):04d}"
    payload = {"CallUUID": cuid, "From": FORWARDER, "To": DIALLED,
               "Direction": "inbound", "CallStatus": "ringing", "Event": "StartApp"}
    # ForwardedFrom is present only when the carrier emitted a SIP Diversion
    # header. It is omitted entirely when absent — never sent empty.
    if diversion:
        payload["ForwardedFrom"] = citizen

    requests.post(f"{BASE}/answer", data=payload, timeout=10)
    d = requests.get(f"{BASE}/decisions", params={"n": 1}, timeout=5).json()["decisions"][0]
    d["_uuid"] = cuid
    return d


def hangup(uuid: str):
    requests.post(f"{BASE}/hangup", data={"CallUUID": uuid}, timeout=5)


def show(d: dict, note: str = ""):
    c = COLOUR.get(d.get("route", ""), "")
    i = d.get("identity", {})
    who = i.get("number") or "unidentified"
    trust = "" if i.get("confident") else f" {DIM}(untrusted){OFF}"
    print(f"  {c}{d.get('route','?'):<7}{OFF} {d.get('pool',''):<10} {who:<12} "
          f"{DIM}via {i.get('source','?')}{OFF}{trust} {DIM}{d.get('elapsed_ms','?')}ms{OFF} {note}")
    for step in d.get("considered", []):
        print(f"      {DIM}· {step}{OFF}")


def banner(title: str):
    print(f"\n{'=' * 72}\n  {title}\n{'=' * 72}")


# ---------------------------------------------------------------------------


def scenario_identity():
    banner("IDENTITY — can the router tell who is calling?")
    reset(wipe_history=True)
    print(f"  inbound_mode = {state()['inbound_mode']}\n")

    print("  Carrier PASSES the Diversion header:")
    d = call(CITIZEN, diversion=True); show(d); hangup(d["_uuid"])

    print("\n  Carrier STRIPS the Diversion header:")
    d = call(CITIZEN, diversion=False); show(d); hangup(d["_uuid"])

    print(f"\n  {Y}Without ForwardedFrom every caller presents as the same number,{OFF}")
    print("  so returning-caller routing cannot run at all.")


def scenario_fill(total: int, diversion: bool):
    banner(f"CAPACITY — {total} concurrent calls against the thresholds")
    reset()
    cap = state()["capacity"]
    print(f"  policy={state()['policy']}  ai={cap['ai']['cap']}  "
          f"human={cap['human']['cap']} ({cap['human']['agents_free']} agents free)  "
          f"queue={cap['queue']['cap']}\n")

    seen: dict[str, int] = {}
    for i in range(total):
        d = call(f"9177777{i:05d}", diversion=diversion)
        if d.get("route") not in seen:
            seen[d["route"]] = i + 1
            show(d, f"{DIM}<- first call here (#{i + 1}){OFF}")

    print()
    cap = state()["capacity"]
    for pool in ("ai", "human", "queue"):
        print(f"  {pool:<6} {cap[pool]['in_use']:>3}/{cap[pool]['cap']:<3} in use")
    print("\n  first call to each branch:  "
          + "  ".join(f"{COLOUR.get(k,'')}{k}{OFF}=#{v}" for k, v in seen.items()))


def scenario_repeat(diversion: bool):
    banner("RETURNING CALLER — does the second call route differently?")
    reset(wipe_history=True)

    print("  First call:")
    d1 = call(CITIZEN, diversion=diversion); show(d1)
    requests.post(f"{BASE}/reference",
                  data={"number": CITIZEN, "reference": "REF-2291",
                        "summary": "Unresolved query"}, timeout=5)
    hangup(d1["_uuid"])
    print(f"\n  {DIM}backend posted reference REF-2291 back to /reference{OFF}")

    print("\n  Same caller again:")
    d2 = call(CITIZEN, diversion=diversion); show(d2); hangup(d2["_uuid"])

    if d2.get("pool") == "ai_repeat":
        print(f"\n  {G}Routed to the returning-caller agent, reference attached.{OFF}")
    else:
        print(f"\n  {Y}Not recognised — nothing to key history on.{OFF}")


def scenario_queue(diversion: bool):
    banner("QUEUE — a caller held, then promoted when a channel frees")
    reset()
    cap = state()["capacity"]
    fill = cap["ai"]["cap"] + min(cap["human"]["cap"], cap["human"]["agents_free"])
    print(f"  Filling every channel ({fill} calls)...")
    live = [call(f"9166666{i:05d}", diversion=diversion)["_uuid"] for i in range(fill)]

    print("  One more caller arrives:")
    d = call(CITIZEN, diversion=diversion); show(d)
    if d.get("route") != "queue":
        print(f"  {Y}Expected a queue here.{OFF}"); return

    print(f"\n  {DIM}One call hangs up, freeing a channel...{OFF}")
    hangup(live[0])
    print("  The hold audio finishes and the caller returns to /queue:")
    requests.post(f"{BASE}/queue/{d['_uuid']}", data={"CallUUID": d["_uuid"]}, timeout=10)
    show(requests.get(f"{BASE}/decisions", params={"n": 1}, timeout=5).json()["decisions"][0])


def main():
    global BASE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", choices=["identity", "fill", "repeat", "queue", "all"])
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--calls", type=int, default=95)
    ap.add_argument("--no-diversion", action="store_true",
                    help="simulate a carrier that strips the SIP Diversion header")
    args = ap.parse_args()

    BASE = args.base.rstrip("/")
    div = not args.no_diversion
    state()  # fail fast with a clear message if the router is not running

    if args.scenario in ("identity", "all"):
        scenario_identity()
    if args.scenario in ("fill", "all"):
        scenario_fill(args.calls, div)
    if args.scenario in ("repeat", "all"):
        scenario_repeat(div)
    if args.scenario in ("queue", "all"):
        scenario_queue(div)
    print()


if __name__ == "__main__":
    main()

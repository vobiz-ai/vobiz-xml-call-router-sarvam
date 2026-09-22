"""
verify.py — assert every routing branch, end to end
====================================================
Drives the live router and checks both the decision AND the XML shape for
each branch. Fills pools by reading /state rather than by assuming capacities,
so it stays correct when the caps in .env change.

    python verify.py
"""

import sys
import xml.etree.ElementTree as ET

import requests

B = "http://127.0.0.1:8090"
G, R, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
failures: list[str] = []
n = 0


def state() -> dict:
    return requests.get(f"{B}/state", timeout=5).json()["capacity"]


def ai_shape() -> list[str]:
    """The AI branch emits different verbs per AI_MODE, so read the mode rather
    than hardcoding one shape and reporting a false failure in the others."""
    mode = requests.get(f"{B}/state", timeout=5).json()["ai_mode"]
    # In proxy mode the XML is Sarvam's, not ours, so its verbs are not ours
    # to assert. None means "must parse and route correctly, shape is theirs".
    return {
        "sarvam_stream": ["Stream", "Speak", "Hangup"],
        "stream": ["Stream", "Hangup"],
        "proxy": None,
    }.get(mode, ["Speak", "Speak", "Wait", "Hangup"])


def answer(uuid: str, citizen: str = "", **extra):
    payload = {"CallUUID": uuid, "From": "919999900001", "To": "918888800001",
               "Direction": "inbound", "CallStatus": "ringing"}
    if citizen:
        payload["ForwardedFrom"] = citizen
    payload.update(extra)
    return requests.post(f"{B}/answer", data=payload, timeout=10)


def last_decision() -> dict:
    return requests.get(f"{B}/decisions", params={"n": 1}, timeout=5).json()["decisions"][0]


def verbs(r) -> list[str]:
    return [c.tag for c in ET.fromstring(r.text)]


def expect(label: str, r, route: str, pool: str, shape: list[str]):
    global n
    n += 1
    d = last_decision()
    try:
        got = verbs(r)
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


def fill(pool: str, prefix: str):
    """Saturate one pool, reading capacity back rather than assuming it."""
    guard = 0
    while state()[pool]["free"] > 0 and guard < 500:
        guard += 1
        answer(f"{prefix}-{guard}", f"9198{guard:08d}")


def reset(wipe: bool = False):
    requests.post(f"{B}/reset", params={"wipe_history": str(wipe).lower()}, timeout=5)


# ---------------------------------------------------------------------------

print("\nIdentity")
reset(wipe=True)
r = answer("id-1", "917777700001")
expect("carrier sends Diversion", r, "ai", "ai_new", ai_shape())
assert last_decision()["identity"]["source"] == "ForwardedFrom", "expected ForwardedFrom"
assert last_decision()["identity"]["confident"] is True

r = answer("id-2")  # no ForwardedFrom
expect("carrier strips Diversion", r, "ai", "ai_new", ai_shape())
d = last_decision()
if d["identity"]["confident"]:
    failures.append("identity should not be trusted without ForwardedFrom")
else:
    print(f"  {G}ok{OFF}    {'identity correctly untrusted':<28} {DIM}{d['identity']['note'][:44]}…{OFF}")

print("\nRepeat caller")
reset(wipe=True)
answer("rc-1", "917777700002")
requests.post(f"{B}/complaint", data={"number": "917777700002",
                                      "complaint_id": "GRV-2291"}, timeout=5)
requests.post(f"{B}/hangup", data={"CallUUID": "rc-1"}, timeout=5)
r = answer("rc-2", "917777700002")
expect("second call -> repeat agent", r, "ai", "ai_repeat",
       ai_shape())

print("\nCapacity cascade")
reset()
fill("ai", "cap-ai")
r = answer("ov-1", "917777700010")
expect("AI full -> human", r, "human", "human", ["Speak", "Dial", "Redirect"])

fill("human", "cap-hu")
r = answer("ov-2", "917777700011")
expect("human full -> queue", r, "queue", "queue", ["Speak", "Wait", "Redirect"])

r = requests.post(f"{B}/queue/ov-2", data={"CallUUID": "ov-2"}, timeout=10)
expect("later hold cycle (no Speak)", r, "queue", "queue", ["Wait", "Redirect"])

fill("queue", "cap-q")
r = answer("ov-3", "917777700012")
expect("queue full -> reject", r, "reject", "none", ["Wait", "Hangup"])

print("\nQueue behaviour")
reset()
fill("ai", "q-ai")
fill("human", "q-hu")
answer("held", "917777700020")
requests.post(f"{B}/hangup", data={"CallUUID": "q-ai-1"}, timeout=5)   # free one AI slot
r = requests.post(f"{B}/queue/held", data={"CallUUID": "held"}, timeout=10)
expect("promoted when slot frees", r, "ai", "ai_new", ai_shape())
if last_decision()["identity"]["source"] != "ForwardedFrom":
    failures.append("identity was lost across the hold")
else:
    print(f"  {G}ok{OFF}    {'identity survives the hold':<28} {DIM}cached at /answer{OFF}")

print("\nQueue timeout")
reset()
fill("ai", "t-ai")
fill("human", "t-hu")
answer("timeout", "917777700030")
for _ in range(3):
    r = requests.post(f"{B}/queue/timeout", data={"CallUUID": "timeout"}, timeout=10)
expect("gives up after max cycles", r, "reject", "none", ["Speak", "Hangup"])

print("\nSlot accounting")
reset()
before = state()["ai"]["in_use"]
answer("acct-1", "917777700040")
mid = state()["ai"]["in_use"]
requests.post(f"{B}/hangup", data={"CallUUID": "acct-1"}, timeout=5)
after = state()["ai"]["in_use"]
n += 1
if (before, mid, after) == (0, 1, 0):
    print(f"  {G}ok{OFF}    {'reserve then release':<28} {DIM}0 -> 1 -> 0{OFF}")
else:
    failures.append(f"slot accounting: {before} -> {mid} -> {after}, expected 0 -> 1 -> 0")
    print(f"  {R}FAIL{OFF}  slot accounting {before} -> {mid} -> {after}")

print("\nEscalation rolls back on failure")
reset()
answer("esc-1", "917777700050")
free_before = state()["human"]["agents_free"]
resp = requests.post(f"{B}/escalate", data={"call_uuid": "esc-1"}, timeout=15)
free_after = state()["human"]["agents_free"]
n += 1
if resp.status_code == 502 and free_before == free_after:
    print(f"  {G}ok{OFF}    {'agent released on failure':<28} "
          f"{DIM}502, {free_after} agents still free{OFF}")
elif resp.status_code == 200:
    print(f"  {G}ok{OFF}    {'transfer accepted':<28} {DIM}credentials present{OFF}")
else:
    failures.append(f"escalation rollback: {resp.status_code}, "
                    f"agents {free_before} -> {free_after}")
    print(f"  {R}FAIL{OFF}  escalation rollback")

reset(wipe=True)
print(f"\n{'=' * 60}")
if failures:
    print(f"{R}{len(failures)} of {n} checks failed{OFF}")
    for f in failures:
        print(f"  · {f}")
    sys.exit(1)
print(f"{G}all {n} checks passed{OFF}\n")

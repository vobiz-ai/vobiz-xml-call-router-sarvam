"""
router.py — the routing decision engine
=======================================
Pure functions. No I/O, no globals, no framework. Everything the engine needs
arrives as arguments, so every branch in here is unit-testable without placing
a call (see sim.py).

The engine answers one question: a call just arrived, where does it go?

    resolve_identity(params, config)  -> Identity   who is calling (and can we trust it)
    decide(identity, history, capacity, config) -> Decision   where the call goes

app.py turns a Decision into Vobiz XML. Keeping that split means the hard part
is testable and the XML part is boring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Literal, Optional

Route = Literal["ai", "human", "queue", "reject"]
Pool = Literal["ai_new", "ai_repeat", "human", "queue", "none"]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Everything that makes the routing policy what it is.

    Defaults match the capacity flowchart (30 human / 50 AI / 5 queue). The
    email thread instead describes "70% of channels reserved for AI, overflow
    to human" — that is a different policy, so it is a switch rather than a
    hardcoded assumption. See README §Policy.
    """

    policy: Literal["ai_first", "human_first"] = "ai_first"

    ai_capacity: int = 50
    human_capacity: int = 30
    queue_capacity: int = 5

    # How many hold cycles before a queued caller is given up on. Each cycle is
    # hold_seconds long, so 3 x 15s = the 45 second hold in the flowchart.
    queue_max_cycles: int = 3
    hold_seconds: int = 15

    # sim_forward: calls reach Vobiz via a SIM that forwards them, so `From` is
    #              the forwarding line, not the caller. Identity is only knowable if the
    #              carrier emitted a SIP Diversion header (-> ForwardedFrom).
    # direct_did:  callers dial the Vobiz DID directly, so `From` IS the caller.
    inbound_mode: Literal["sim_forward", "direct_did"] = "sim_forward"

    # The forwarding SIM's own number, when known. Lets us positively identify
    # "this From is the SIM, not a caller" rather than merely suspecting it.
    sim_number: str = ""

    # Route known callers to a dedicated repeat-caller agent when one exists.
    repeat_caller_routing: bool = True

    # A caller seen within this many days counts as a repeat caller.
    repeat_window_days: int = 30


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def normalise(number: str) -> str:
    """Reduce any number format to its last 10 digits.

    Vobiz does not format numbers uniformly across parameters — a single
    payload can carry a national-format `From`, an E.164 `CallerName` and a
    `To` that still has a trunk prefix. Comparing on the last 10 digits is the
    only thing that matches reliably across them.
    """
    digits = re.sub(r"\D", "", number or "")
    return digits[-10:] if len(digits) >= 10 else digits


@dataclass(frozen=True)
class Identity:
    number: str                 # normalised to 10 digits — for history lookups
    source: str                 # ForwardedFrom | From | unknown
    confident: bool             # can this be used to look up history?
    note: str = ""              # why not, when confident is False
    raw: str = ""               # exactly as Vobiz sent it — for forwarding on

    def best(self) -> str:
        """The form to hand downstream. Normalising is for matching our own
        records; anything we forward should keep the country code, because
        the receiver may well parse it as E.164."""
        return self.raw or self.number


def resolve_identity(params: dict, config: Config) -> Identity:
    """Work out who is calling, and whether we are allowed to believe it.

    This is the function that makes the SIM-forwarding problem visible instead
    of silently routing every caller as if they were the same person.
    """
    forwarded = normalise(params.get("ForwardedFrom", ""))
    caller = normalise(params.get("From", ""))

    # ForwardedFrom is populated from the SIP Diversion header and is omitted
    # entirely when absent — never sent as an empty string. When it IS present
    # it is the original caller, and it beats From in every mode.
    if forwarded:
        return Identity(forwarded, "ForwardedFrom", True,
                        raw=params.get("ForwardedFrom", "").strip())

    if config.inbound_mode == "direct_did":
        if caller:
            return Identity(caller, "From", True,
                            raw=params.get("From", "").strip())
        return Identity("", "unknown", False, "From was empty on a direct DID call")

    # sim_forward and no Diversion header: From is the forwarding SIM. It is
    # the same value for every caller, so it identifies nobody.
    if config.sim_number and caller == normalise(config.sim_number):
        return Identity(
            "", "From", False,
            "From is the forwarding SIM, not the caller; carrier sent no Diversion header",
        )
    return Identity(
        caller, "From", False,
        "SIM forwarding is in use and no ForwardedFrom arrived, so From cannot be "
        "trusted as the caller's number",
        raw=params.get("From", "").strip(),
    )


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capacity:
    """A snapshot of what is currently in use. Supplied by state.py."""

    ai_in_use: int = 0
    human_in_use: int = 0
    queued: int = 0
    human_agents_free: int = 0     # online AND not already on a call

    def ai_free(self, c: Config) -> int:
        return max(0, c.ai_capacity - self.ai_in_use)

    def human_free(self, c: Config) -> int:
        # A human slot needs both a channel and a logged-in agent to answer it.
        # Capacity alone is not availability.
        return max(0, min(c.human_capacity - self.human_in_use, self.human_agents_free))

    def queue_free(self, c: Config) -> int:
        return max(0, c.queue_capacity - self.queued)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallerHistory:
    known: bool = False
    call_count: int = 0
    reference: str = ""        # whatever the backend writes back (ticket, case, order)
    last_seen: str = ""
    summary: str = ""


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    route: Route
    pool: Pool
    reason: str
    identity: Identity
    history: CallerHistory
    capacity: Capacity
    considered: list[str] = field(default_factory=list)  # the path taken, in order
    queue_cycle: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["identity"] = asdict(self.identity)
        d["history"] = asdict(self.history)
        d["capacity"] = asdict(self.capacity)
        return d


def decide(
    identity: Identity,
    history: CallerHistory,
    capacity: Capacity,
    config: Config,
    queue_cycle: int = 0,
) -> Decision:
    """Choose a destination for one call.

    `considered` records every option that was examined and why it was passed
    over, because "why did this call go to a human" is the question that gets
    asked about a routing layer and guessing at it later is miserable.
    """
    trail: list[str] = []

    # Which AI agent this caller belongs to, if AI is where they end up.
    repeat = config.repeat_caller_routing and identity.confident and history.known
    ai_pool: Pool = "ai_repeat" if repeat else "ai_new"

    if config.repeat_caller_routing and not identity.confident:
        trail.append(
            f"repeat-caller lookup skipped: {identity.note or 'caller not identifiable'}"
        )
    elif repeat:
        trail.append(
            f"known caller ({history.call_count} prior calls"
            + (f", ref {history.reference}" if history.reference else "")
            + ") -> repeat-caller agent"
        )
    elif identity.confident:
        trail.append("caller identified and not seen before -> new-caller agent")

    def try_ai() -> Optional[Decision]:
        free = capacity.ai_free(config)
        if free > 0:
            trail.append(f"AI pool has {free}/{config.ai_capacity} free -> routing to AI")
            return Decision("ai", ai_pool, "AI channel available", identity, history,
                            capacity, trail, queue_cycle)
        trail.append(f"AI pool full ({capacity.ai_in_use}/{config.ai_capacity})")
        return None

    def try_human() -> Optional[Decision]:
        free = capacity.human_free(config)
        if free > 0:
            trail.append(
                f"human pool has {free} free "
                f"({capacity.human_in_use}/{config.human_capacity} channels in use, "
                f"{capacity.human_agents_free} agents free) -> routing to human"
            )
            return Decision("human", "human", "human agent available", identity,
                            history, capacity, trail, queue_cycle)
        if capacity.human_agents_free == 0:
            trail.append(
                "no human agent free — every agent is offline or already on a call"
            )
        else:
            trail.append(
                f"human pool full ({capacity.human_in_use}/{config.human_capacity})"
            )
        return None

    order = (try_ai, try_human) if config.policy == "ai_first" else (try_human, try_ai)
    for attempt in order:
        result = attempt()
        if result:
            return result

    # Both pools are full. Hold the caller if there is room to.
    if queue_cycle >= config.queue_max_cycles:
        trail.append(
            f"held for {config.queue_max_cycles * config.hold_seconds}s without a "
            "free channel -> giving up"
        )
        return Decision("reject", "none", "queue timed out", identity, history,
                        capacity, trail, queue_cycle)

    free = capacity.queue_free(config)
    if free > 0 or queue_cycle > 0:
        # A caller already holding keeps their slot; they are counted in
        # `queued` already, so do not make them re-win a slot each cycle.
        trail.append(
            f"queue has {free}/{config.queue_capacity} free -> holding "
            f"(cycle {queue_cycle + 1} of {config.queue_max_cycles})"
        )
        return Decision("queue", "queue", "all channels busy, caller held", identity,
                        history, capacity, trail, queue_cycle)

    trail.append(f"queue full ({capacity.queued}/{config.queue_capacity}) -> rejecting")
    return Decision("reject", "none", "no capacity anywhere", identity, history,
                    capacity, trail, queue_cycle)

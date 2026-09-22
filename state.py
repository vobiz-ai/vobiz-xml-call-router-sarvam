"""
state.py — capacity accounting, caller history, decision log
=============================================================
Vobiz does not know which of your channels are AI and which are human, and it
will never tell you "the AI pool is full". Occupancy is something this service
has to count for itself: reserve a slot when a call is routed, release it when
the hangup webhook arrives.

The failure mode that matters is a *leaked* slot — a hangup webhook that never
arrives leaves a slot reserved forever, and the pool silently shrinks until
every caller is rejected. Every reservation therefore carries a TTL and is
swept, so the worst case is a slot freed late rather than never.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from router import Capacity, CallerHistory, Config

DB_PATH = Path(__file__).parent / "data" / "router.db"


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Live capacity
# ---------------------------------------------------------------------------


@dataclass
class Slot:
    call_uuid: str
    pool: str
    reserved_at: float
    expires_at: float
    caller: str = ""


class Slots:
    """In-memory occupancy for the AI, human and queue pools.

    In-memory is correct here: these counts describe calls that are live on
    *this* process's watch. A restart means every call it was tracking is gone
    too, so persisting them would restore phantom occupancy.

    For more than one router instance this becomes Redis with the same shape —
    the interface is deliberately three methods wide.
    """

    def __init__(self, ttl_seconds: int = 3600):
        self._slots: dict[str, Slot] = {}
        self._lock = threading.Lock()
        self.ttl = ttl_seconds
        self.leaked = 0  # slots reclaimed by the sweeper, not by a hangup

    def reserve(self, call_uuid: str, pool: str, caller: str = "", ttl: int | None = None) -> Slot:
        with self._lock:
            slot = Slot(call_uuid, pool, _now(), _now() + (ttl or self.ttl), caller)
            self._slots[call_uuid] = slot
            return slot

    def release(self, call_uuid: str) -> Slot | None:
        with self._lock:
            return self._slots.pop(call_uuid, None)

    def move(self, call_uuid: str, pool: str) -> Slot | None:
        """Re-pool a live call — a queued caller promoted to AI, or an AI call
        escalated to a human. The call keeps one slot throughout; it just
        changes which pool that slot is counted against."""
        with self._lock:
            slot = self._slots.get(call_uuid)
            if slot:
                slot.pool = pool
            return slot

    def sweep(self) -> int:
        """Reclaim slots whose call should have ended by now."""
        with self._lock:
            dead = [u for u, s in self._slots.items() if s.expires_at < _now()]
            for u in dead:
                del self._slots[u]
            self.leaked += len(dead)
            return len(dead)

    def counts(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for s in self._slots.values():
                out[s.pool] = out.get(s.pool, 0) + 1
            return out

    def snapshot(self, agents_free: int) -> Capacity:
        c = self.counts()
        return Capacity(
            ai_in_use=c.get("ai_new", 0) + c.get("ai_repeat", 0),
            human_in_use=c.get("human", 0),
            queued=c.get("queue", 0),
            human_agents_free=agents_free,
        )

    def live(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "call_uuid": s.call_uuid,
                    "pool": s.pool,
                    "caller": s.caller,
                    "held_for_s": round(_now() - s.reserved_at, 1),
                }
                for s in sorted(self._slots.values(), key=lambda x: x.reserved_at)
            ]


# ---------------------------------------------------------------------------
# Human agents
# ---------------------------------------------------------------------------


class AgentPool:
    """Who is logged in and reachable. Agents register over /agents.

    Kept separate from Slots because "30 channels" and "how many agents are at
    their desk right now" are different numbers, and routing to a channel with
    nobody behind it just rings out.
    """

    def __init__(self, numbers: list[str] | None = None):
        self._agents: dict[str, dict] = {}
        self._lock = threading.Lock()
        for n in numbers or []:
            self.register(n)

    def register(self, number: str, name: str = "") -> dict:
        with self._lock:
            agent = self._agents.setdefault(
                number, {"number": number, "name": name or number, "online": True, "busy": False}
            )
            agent["online"] = True
            return agent

    def set_status(self, number: str, online: bool | None = None, busy: bool | None = None):
        with self._lock:
            agent = self._agents.get(number)
            if not agent:
                return None
            if online is not None:
                agent["online"] = online
            if busy is not None:
                agent["busy"] = busy
            return agent

    def available(self) -> list[dict]:
        with self._lock:
            return [a for a in self._agents.values() if a["online"] and not a["busy"]]

    def next_available(self) -> dict | None:
        """Round-robin over free agents: take the first and send it to the back."""
        with self._lock:
            free = [a for a in self._agents.values() if a["online"] and not a["busy"]]
            if not free:
                return None
            agent = free[0]
            self._agents[agent["number"]] = self._agents.pop(agent["number"])
            return agent

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._agents.values())


# ---------------------------------------------------------------------------
# Caller history
# ---------------------------------------------------------------------------


class History:
    """Persistent caller memory — what makes a repeat caller a repeat caller.

    SQLite because it survives a restart, needs no service, and the lookup is
    a primary-key hit on a table that will hold tens of thousands of rows. If
    this ever outgrows that, the interface is `lookup` and `record`.
    """

    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS callers (
                    number       TEXT PRIMARY KEY,
                    first_seen   TEXT NOT NULL,
                    last_seen    TEXT NOT NULL,
                    call_count   INTEGER NOT NULL DEFAULT 0,
                    reference    TEXT DEFAULT '',
                    summary      TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS calls (
                    call_uuid    TEXT PRIMARY KEY,
                    number       TEXT,
                    identity_src TEXT,
                    route        TEXT,
                    pool         TEXT,
                    started      TEXT,
                    ended        TEXT,
                    duration_s   REAL,
                    reason       TEXT
                );
                -- Callers who were never answered. The flowchart's "number
                -- captured, SMS sent" branch reads from here.
                CREATE TABLE IF NOT EXISTS missed (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    number     TEXT,
                    at         TEXT,
                    reason     TEXT,
                    sms_sent   INTEGER DEFAULT 0
                );
                """
            )

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def lookup(self, number: str, config: Config) -> CallerHistory:
        if not number:
            return CallerHistory()
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM callers WHERE number = ?", (number,)).fetchone()
        if not row:
            return CallerHistory()

        # A caller who last rang a year ago is not "a repeat caller" in any
        # sense the routing cares about.
        try:
            last = datetime.fromisoformat(row["last_seen"])
            if datetime.now(timezone.utc) - last > timedelta(days=config.repeat_window_days):
                return CallerHistory()
        except ValueError:
            pass

        return CallerHistory(
            known=True,
            call_count=row["call_count"],
            reference=row["reference"] or "",
            last_seen=row["last_seen"],
            summary=row["summary"] or "",
        )

    def record_call(self, number: str, call_uuid: str, identity_src: str, route: str,
                    pool: str, reason: str = ""):
        now = _iso(_now())
        with self._lock, self._connect() as db:
            if number:
                db.execute(
                    """
                    INSERT INTO callers (number, first_seen, last_seen, call_count)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(number) DO UPDATE SET
                        last_seen  = excluded.last_seen,
                        call_count = call_count + 1
                    """,
                    (number, now, now),
                )
            db.execute(
                """INSERT OR REPLACE INTO calls
                   (call_uuid, number, identity_src, route, pool, started, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (call_uuid, number, identity_src, route, pool, now, reason),
            )

    def close_call(self, call_uuid: str, duration_s: float | None = None):
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT started FROM calls WHERE call_uuid = ?", (call_uuid,)
            ).fetchone()
            computed = duration_s
            if computed is None and row and row["started"]:
                try:
                    computed = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(row["started"])
                    ).total_seconds()
                except ValueError:
                    computed = None
            db.execute(
                "UPDATE calls SET ended = ?, duration_s = ? WHERE call_uuid = ?",
                (_iso(_now()), computed, call_uuid),
            )

    def set_reference(self, number: str, reference: str, summary: str = ""):
        """What the backend writes back at the end of a conversation — a ticket
        or case number — so the next call from this caller can be routed
        with it."""
        now = _iso(_now())
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO callers (number, first_seen, last_seen, call_count,
                                     reference, summary)
                VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT(number) DO UPDATE SET
                    reference = excluded.reference,
                    summary   = excluded.summary,
                    last_seen = excluded.last_seen
                """,
                (number, now, now, reference, summary),
            )

    def record_missed(self, number: str, reason: str):
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO missed (number, at, reason) VALUES (?, ?, ?)",
                (number, _iso(_now()), reason),
            )

    def stats(self) -> dict:
        with self._lock, self._connect() as db:
            return {
                "known_callers": db.execute("SELECT COUNT(*) c FROM callers").fetchone()["c"],
                "calls_recorded": db.execute("SELECT COUNT(*) c FROM calls").fetchone()["c"],
                "missed": db.execute("SELECT COUNT(*) c FROM missed").fetchone()["c"],
                "by_pool": {
                    r["pool"]: r["n"]
                    for r in db.execute(
                        "SELECT pool, COUNT(*) n FROM calls GROUP BY pool"
                    ).fetchall()
                },
            }

    def reset(self):
        with self._lock, self._connect() as db:
            db.executescript("DELETE FROM callers; DELETE FROM calls; DELETE FROM missed;")


# ---------------------------------------------------------------------------
# Decision log
# ---------------------------------------------------------------------------


class DecisionLog:
    """The last N routing decisions, with the full reasoning trail.

    "Why did this call go to a human?" is the question a routing layer gets
    asked, and reconstructing it afterwards from platform logs is miserable.
    """

    def __init__(self, size: int = 200):
        self._log: deque = deque(maxlen=size)
        self._lock = threading.Lock()

    def add(self, call_uuid: str, decision, elapsed_ms: float, params: dict):
        with self._lock:
            self._log.appendleft(
                {
                    "at": _iso(_now()),
                    "call_uuid": call_uuid,
                    "route": decision.route,
                    "pool": decision.pool,
                    "reason": decision.reason,
                    "elapsed_ms": round(elapsed_ms, 1),
                    "identity": {
                        "number": decision.identity.number,
                        "source": decision.identity.source,
                        "confident": decision.identity.confident,
                        "note": decision.identity.note,
                    },
                    "repeat_caller": decision.history.known,
                    "considered": decision.considered,
                    "raw": {
                        k: params.get(k)
                        for k in ("From", "To", "ForwardedFrom", "Direction", "CallStatus")
                        if params.get(k) is not None
                    },
                }
            )

    def recent(self, n: int = 50) -> list[dict]:
        with self._lock:
            return list(self._log)[:n]

    def clear(self):
        with self._lock:
            self._log.clear()

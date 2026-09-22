"""
vobiz.py — the Vobiz REST calls this router needs
==================================================
Trimmed from 1-testing/vobiz.py. Only two endpoints matter here: placing a
test call, and transferring a live one (which is how AI -> human escalation
works).
"""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

API_BASE = "https://api.vobiz.ai/api/v1"
AUTH_ID = os.getenv("VOBIZ_AUTH_ID", "")
AUTH_TOKEN = os.getenv("VOBIZ_AUTH_TOKEN", "")
FROM_NUMBER = os.getenv("FROM_NUMBER", "")

HEADERS = {
    "Content-Type": "application/json",
    "X-Auth-ID": AUTH_ID,
    "X-Auth-Token": AUTH_TOKEN,
}


class VobizError(RuntimeError):
    """Raised instead of exiting, because this module is imported by a server.

    sys.exit inside a request handler raises SystemExit — a BaseException that
    most `except Exception` guards do not catch and that can take the worker
    down. CLI entry points catch this and exit; the server turns it into a
    response.
    """


def require_credentials():
    if not AUTH_ID or not AUTH_TOKEN:
        raise VobizError("VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN must be set in .env")


def place_call(to: str, answer_url: str, hangup_url: str, from_number: str = "",
               **advanced) -> dict:
    """POST /Account/{auth_id}/Call/ — queues an outbound call.

    Used to drive the router from a real call without waiting for an inbound
    one. A 401 means credentials, a 402 means balance, and a `to` error means
    both are fine — check in that order before debugging code.
    """
    require_credentials()
    payload = {
        "from": from_number or FROM_NUMBER,
        "to": to,
        "answer_url": answer_url,
        "answer_method": "POST",
        "hangup_url": hangup_url,
        "hangup_method": "POST",
    }
    payload.update({k: v for k, v in advanced.items() if v is not None})
    r = requests.post(f"{API_BASE}/Account/{AUTH_ID}/Call/", json=payload,
                      headers=HEADERS, timeout=30)
    if r.status_code >= 400:
        raise VobizError(f"Vobiz returned {r.status_code}: {r.text}")
    return r.json()


def transfer_call(call_uuid: str, aleg_url: str) -> dict:
    """POST /Account/{auth_id}/Call/{call_uuid}/ — redirects a live leg.

    Returns 202. The leg abandons its current XML document immediately, so
    whatever the backend was doing on that leg stops the moment this is accepted.
    """
    require_credentials()
    r = requests.post(
        f"{API_BASE}/Account/{AUTH_ID}/Call/{call_uuid}/",
        json={"legs": "aleg", "aleg_url": aleg_url, "aleg_method": "POST"},
        headers=HEADERS,
        timeout=30,
    )
    try:
        return {"status": r.status_code, "body": r.json()}
    except ValueError:
        return {"status": r.status_code, "body": r.text[:500]}

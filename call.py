"""
call.py — place one real call through the router
=================================================
Proves the XML on a live call, once the simulator has proven the logic.

    python call.py --to +9198XXXXXXXX

The call is placed outbound to your handset with the router as its answer URL,
so you hear whichever branch the router chose. Inbound testing is the same
thing without this script: point the Answer URL of the Vobiz application at
{PUBLIC_URL}/answer and dial the DID.
"""

import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

import vobiz

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


def server_base() -> str:
    """The router's public URL, from its own /health — authoritative over .env
    because a restarted tunnel changes the hostname."""
    port = os.getenv("PORT", "8090")
    try:
        health = requests.get(f"http://127.0.0.1:{port}/health", timeout=3).json()
        if health.get("public_url"):
            return health["public_url"].rstrip("/")
    except requests.RequestException:
        pass
    public = (os.getenv("PUBLIC_URL") or "").rstrip("/")
    if public:
        return public
    sys.exit("No PUBLIC_URL. Start the tunnel and set it in .env.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default=os.getenv("TO_NUMBER", ""), help="number to ring")
    ap.add_argument("--forwarded-from", default="",
                    help="simulate a forwarded call by setting ForwardedFrom")
    args = ap.parse_args()
    if not args.to:
        sys.exit("Pass --to, or set TO_NUMBER in .env")

    base = server_base()
    answer_url = f"{base}/answer"
    if args.forwarded_from:
        answer_url += f"?ForwardedFrom={args.forwarded_from}"

    print(f"  answer_url  {answer_url}")
    print(f"  hangup_url  {base}/hangup")
    try:
        result = vobiz.place_call(args.to, answer_url, f"{base}/hangup")
    except vobiz.VobizError as exc:
        sys.exit(str(exc))
    print(f"  response    {json.dumps(result, indent=2)}")
    print(f"\n  Watch it land:  {base}/  (or python -m json.tool < curl {base}/decisions)")


if __name__ == "__main__":
    main()

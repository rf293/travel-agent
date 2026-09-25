#!/usr/bin/env python3
"""Two-step `letsfg auth` for machines whose loopback the approver's browser cannot reach.

`letsfg auth` waits on a 127.0.0.1 listener, so it only works when the person
approving uses a browser on the same machine. On a remote VM:

  1. python scripts/letsfg_remote_auth.py start
     → prints the letsfg.co/connect approval link; a PERSON opens and approves it.
  2. Their browser then fails to load http://127.0.0.1:<port>/callback?code=…
     That address-bar URL is the hand-off. Paste it back promptly (codes expire):
     python scripts/letsfg_remote_auth.py finish '<redirect URL>'
     → tokens land in ~/.letsfg/config.json and auto-refresh like `letsfg auth`.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from letsfg.connectors.auth import (
    _CLIENT_NAME,
    _FALLBACK_TTL,
    _SCOPE,
    _discover,
    _pkce,
    _post_form,
    _post_json_abs,
    save_token,
)

DEFAULT_STATE = Path.home() / ".letsfg" / "pending_auth.json"


def start(state_path: Path) -> str:
    meta = _discover()
    verifier, challenge = _pkce()
    state = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()
    redirect_uri = f"http://127.0.0.1:{secrets.randbelow(16000) + 49152}/callback"

    status, reg = _post_json_abs(
        meta["registration_endpoint"],
        {
            "client_name": _CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    client_id = reg.get("client_id") if isinstance(reg, dict) else None
    if status not in (200, 201) or not client_id:
        raise SystemExit(f"LetsFG client registration failed (HTTP {status}): {reg}")

    auth_url = meta["authorization_endpoint"] + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": _SCOPE,
        }
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "state": state,
                "token_endpoint": meta["token_endpoint"],
                "created_at": time.time(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    state_path.chmod(0o600)
    return auth_url


def finish(redirect_url: str, state_path: Path) -> None:
    pending = json.loads(state_path.read_text(encoding="utf-8"))
    query = parse_qs(urlparse(redirect_url.strip()).query)
    if query.get("error"):
        raise SystemExit(f"Approval was not granted: {query['error'][0]}")
    code = (query.get("code") or [""])[0]
    if not code:
        raise SystemExit("No ?code= in that URL — paste the full 127.0.0.1/callback address.")
    if (query.get("state") or [""])[0] != pending["state"]:
        raise SystemExit("State mismatch — that redirect belongs to a different `start`.")

    status, data = _post_form(
        pending["token_endpoint"],
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": pending["client_id"],
            "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["code_verifier"],
        },
    )
    if status != 200 or not data.get("access_token"):
        detail = data.get("error_description") or data.get("error") or data
        raise SystemExit(f"Token exchange failed (HTTP {status}): {detail}")

    expires_in = data.get("expires_in")
    save_token(
        str(data["access_token"]),
        time.time() + (float(expires_in) if isinstance(expires_in, (int, float)) else _FALLBACK_TTL),
        refresh_token=data.get("refresh_token"),
        client_id=str(pending["client_id"]),
    )
    state_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="Pending-auth file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="Register a client and print the approval link")
    finish_cmd = sub.add_parser("finish", help="Exchange the pasted redirect URL for tokens")
    finish_cmd.add_argument("redirect_url")
    args = parser.parse_args()

    if args.command == "start":
        print(start(args.state))
        print(f"\nPending state saved to {args.state}", file=sys.stderr)
    else:
        finish(args.redirect_url, args.state)
        print("LetsFG connected — token saved to ~/.letsfg/config.json (auto-refreshes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

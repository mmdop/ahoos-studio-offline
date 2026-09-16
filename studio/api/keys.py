"""Per-caller API keys, and the token limit behind them.

The database decides both. check_key() says who is calling and whether they are
already over; consume_tokens() records what a request cost. Neither is callable
by `authenticated` -- both are revoked down to the service role -- so this
module is the only thing that reaches them, and it runs on the API server.

WHY A SERVICE KEY IS RIGHT HERE AND WRONG IN deploy/php

config.example.php says the service_role key has no place on that box, and that
is correct: the PHP host serves pages to browsers, and a key that bypasses
row-level security on a machine whose whole job is rendering untrusted input is
a key one template bug away from being everyone's. It uses the publishable key
and lets RLS do the work.

This is the other kind of server. It holds the provider credentials already, it
serves no HTML, and the two functions it needs are deliberately reachable by
nothing else. Verifying a key means reading api_keys by hash across every user,
which no RLS policy permits and none should.

WITHOUT IT

If SUPABASE_URL and SUPABASE_SERVICE_KEY are unset, this returns None from
check() and every caller falls back to the shared NIMBUS_API_KEY -- which is
exactly how the API behaved before per-caller keys existed. A half-configured
deployment keeps working rather than refusing everyone.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request

TIMEOUT = 15


def _config() -> tuple[str, str] | None:
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or ""
    return (url, key) if url and key else None


def enabled() -> bool:
    return _config() is not None


def hash_key(presented: str) -> str:
    """The stored form. The key itself is never written down anywhere."""
    return hashlib.sha256(presented.encode("utf-8")).hexdigest()


def _rpc(name: str, payload: dict) -> dict | None:
    config = _config()
    if config is None:
        return None
    url, service = config
    request = urllib.request.Request(
        f"{url}/rest/v1/rpc/{name}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "apikey": service,
            "Authorization": f"Bearer {service}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return {"_error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:                      # network, DNS, timeout
        return {"_error": f"{type(exc).__name__}: {exc}"}
    try:
        return json.loads(body)
    except ValueError:
        return {"_error": "unreadable answer"}


def check(presented: str) -> dict | None:
    """Who is this, and are they already over?

    Returns None when per-caller keys are not configured, which means "not my
    decision" rather than "no" -- the caller falls back to the shared key.

    A database that is configured but unreachable returns an error dict rather
    than None, because there the answer is genuinely unknown and letting the
    request through would be a limit that stops applying whenever the database
    is having a bad minute.
    """
    if not enabled():
        return None
    return _rpc("check_key", {"p_key_hash": hash_key(presented)})


def consume(presented: str, tokens_in: int, tokens_out: int) -> dict | None:
    """Record what a request cost. Returns the decision for the next one."""
    if not enabled():
        return None
    return _rpc("consume_tokens", {
        "p_key_hash": hash_key(presented),
        "p_tokens_in": max(0, int(tokens_in or 0)),
        "p_tokens_out": max(0, int(tokens_out or 0)),
    })


def usage_tokens(usage: dict) -> tuple[int, int]:
    """Pull the two numbers out of whatever an engine reported.

    Engines name them input_tokens/output_tokens; a provider that answers with
    OpenAI's spelling reaches here unchanged, so both are read. A missing count
    is zero rather than an error: a request that happened and reported nothing
    should not be free, but it should also not fail after the work is done.
    """
    inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    return int(inp), int(out)

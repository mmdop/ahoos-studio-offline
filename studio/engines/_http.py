"""Shared plumbing for OpenAI-shaped providers.

DeepSeek, Ollama, Groq, Gemini's compatibility endpoint, OpenRouter, and most
others all speak `POST /chat/completions` with the same JSON. This module holds the
parts they share so each engine only has to describe what makes it different.

Standard library only — these are plain HTTP JSON APIs and pulling in an SDK to
call one would add a dependency for nothing.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable

RETRY_STATUSES = {429, 500, 502, 503, 504}


def _positive_int(name: str, default: int) -> int:
    """Read a positive integer from the environment, ignoring junk."""
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


# Defaults suit the evaluation harness, which runs from a terminal where waiting
# out a slow provider beats failing a task: six attempts at fifteen minutes each
# is up to ninety minutes before the caller sees an error.
#
# Behind an HTTP request that is the wrong trade entirely. A serving deployment
# sits behind a proxy with its own much shorter patience -- Render answers 502
# long before the first attempt has even timed out -- so the caller gets a
# useless error instead of the real one, and the worker stays blocked meanwhile.
# Deployments set NIMBUS_HTTP_TIMEOUT and NIMBUS_HTTP_ATTEMPTS to fail fast and
# report what actually went wrong; the eval harness keeps the patient defaults.
MAX_ATTEMPTS = _positive_int("NIMBUS_HTTP_ATTEMPTS", 6)
# Generous: a local model on a small GPU can take minutes for a long answer.
TIMEOUT_SECONDS = _positive_int("NIMBUS_HTTP_TIMEOUT", 900)
# Free tiers rate-limit per minute, so a 429 needs real patience rather than the
# few seconds that suffice for a transient 5xx.
RATE_LIMIT_BACKOFF = (5, 15, 30, 60, 90)
MAX_RETRY_AFTER = 120
# A 429 whose reset is further out than this is not a burst limit — it is a quota.
# Waiting cannot clear it, so retrying only wastes the caller's time.
QUOTA_THRESHOLD_SECONDS = 300


def _is_daily_quota(detail: str) -> bool:
    """Spot a per-day quota in a 429 body when no headers say so.

    Google sends no rate-limit headers and reports `retryDelay: 8s` even when the
    exhausted quota is daily — following that advice retries forever against a
    limit that resets tomorrow. The quota id is the reliable signal.
    """
    lowered = detail.lower()
    return any(
        marker in lowered
        for marker in ("perday", "per_day", "per-day", "requests_per_day", "free_tier_requests")
    )


def _reset_seconds_away(headers: Any) -> float | None:
    """How long until the limit resets, from X-RateLimit-Reset, or None.

    Providers send this as epoch seconds or epoch milliseconds depending on who
    wrote the API, so both are handled.
    """
    if not headers:
        return None
    raw = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 1e11:  # milliseconds
        value /= 1000.0
    return value - time.time()


def extract_json(text: str) -> str:
    """Pull the first complete JSON object out of a model response.

    Providers without schema-constrained decoding get the schema in the prompt
    instead, so the reply may arrive wrapped in a fence or trailed by commentary.
    Brace counting is string-aware: a `}` inside a quoted value must not close the
    object, and an escaped quote must not end the string.
    """
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    start = text.find("{")
    if start == -1:
        return text

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    return text[start:]


def parse_body(raw: bytes) -> dict[str, Any]:
    """Parse a response body, tolerating SSE-style keep-alive noise.

    OpenRouter emits comment lines (`: OPENROUTER PROCESSING`) ahead of the body
    while a request sits in a queue, so a long wait can prepend hundreds of lines
    that are not JSON. Any line starting with `:` is a comment by the SSE spec and
    is never part of a JSON object, so dropping them is safe for every provider.
    """
    text = raw.decode("utf-8", "replace")
    cleaned = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(":")
    ).strip()

    # Some providers also prefix `data: ` on a single-shot response.
    if cleaned.startswith("data:"):
        cleaned = cleaned[len("data:") :].strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        snippet = cleaned[:200] if cleaned else text[:200]
        raise ValueError(
            f"provider returned a body that is not JSON ({exc}). First 200 chars: {snippet!r}"
        ) from exc


def post_json(
    url: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str],
    on_error: Callable[[int, str], str | None] | None = None,
    on_rate_limit: Callable[[int, int, int], None] | None = None,
    error_cls: type[Exception] = RuntimeError,
    timeout: int = TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """POST JSON, retrying transient failures.

    `on_error(status, body)` lets an engine turn a provider-specific status into a
    message a person can act on. Returning a string raises it immediately;
    returning None falls through to the generic handling.

    `on_rate_limit(seconds, attempt, total)` is called before waiting out a 429, so
    a long pause on a free tier looks like progress rather than a hang.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )

    last_error = ""
    for attempt in range(MAX_ATTEMPTS):
        wait = 2**attempt
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return parse_body(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            if on_error is not None:
                message = on_error(exc.code, detail)
                if message is not None:
                    raise error_cls(message) from exc
            if exc.code not in RETRY_STATUSES:
                raise error_cls(f"provider returned {exc.code}: {detail}") from exc

            if exc.code == 429:
                # Distinguish a burst limit from a spent quota. A daily cap resets
                # hours away, so backing off against it is pure waste — fail now
                # and say when it clears.
                away = _reset_seconds_away(exc.headers)
                if away is not None and away > QUOTA_THRESHOLD_SECONDS:
                    hours, remainder = divmod(int(away), 3600)
                    raise error_cls(
                        f"quota exhausted, not a burst limit — retrying will not help. "
                        f"Resets in {hours}h {remainder // 60}m. Provider said: {detail[:200]}"
                    ) from exc

                if _is_daily_quota(detail):
                    raise error_cls(
                        "daily quota exhausted — retrying will not help, whatever "
                        f"retryDelay the provider suggests. Provider said: {detail[:300]}"
                    ) from exc

                # Prefer what the provider actually asked for; fall back to a
                # schedule long enough to clear a per-minute window.
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    wait = min(int(float(retry_after)), MAX_RETRY_AFTER)
                except (TypeError, ValueError):
                    wait = RATE_LIMIT_BACKOFF[min(attempt, len(RATE_LIMIT_BACKOFF) - 1)]
                if on_rate_limit is not None:
                    on_rate_limit(wait, attempt + 1, MAX_ATTEMPTS)

            last_error = f"{exc.code}: {detail}"
        except urllib.error.URLError as exc:
            last_error = str(exc.reason)
        except TimeoutError:
            last_error = f"no response within {timeout}s"

        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(wait)

    raise error_cls(f"provider unreachable after {MAX_ATTEMPTS} attempts: {last_error}")


def usage_from(usage: dict[str, Any]) -> dict[str, int]:
    """Map OpenAI-shaped usage fields onto the names this project uses."""
    mapped = {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "cache_read_input_tokens": usage.get("prompt_cache_hit_tokens"),
    }
    return {key: int(value) for key, value in mapped.items() if value is not None}

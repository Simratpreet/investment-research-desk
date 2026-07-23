"""Lightweight, dependency-free security helpers.

In-process only (per-worker rate-limit state, per-worker semaphores) — correct
for the single-instance container the app deploys to. If this ever runs multiple
workers/instances, move the rate-limit + concurrency state to Redis.

Provides:
  - client_ip(req)          : best-effort caller IP (honours one proxy hop)
  - rate_limit_ok(...)      : sliding-window per-key limiter
  - valid_segment(s)        : strict path-segment validator (^[A-Za-z0-9_-]{1,64}$)
  - resolve_within(base, *) : realpath containment guard (blocks .. traversal)
"""

import os
import re
import threading
import time
from collections import deque

# --- Sliding-window rate limiter -------------------------------------------

_rl_lock = threading.Lock()
_rl_hits: dict[str, deque] = {}


def client_ip(req) -> str:
    """Best-effort client IP. Trusts a single X-Forwarded-For hop (the reverse
    proxy in front on Render/Fly), falling back to the socket peer."""
    xff = req.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return req.remote_addr or "unknown"


def rate_limit_ok(key: str, max_calls: int, window_s: float) -> bool:
    """Return True if `key` is under `max_calls` within the trailing
    `window_s` seconds, recording this call. False means over the limit."""
    now = time.monotonic()
    cutoff = now - window_s
    with _rl_lock:
        dq = _rl_hits.get(key)
        if dq is None:
            dq = deque()
            _rl_hits[key] = dq
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= max_calls:
            return False
        dq.append(now)
        # Opportunistic cleanup so the dict doesn't grow unbounded across many
        # distinct keys (e.g. spoofed X-Forwarded-For values).
        if len(_rl_hits) > 4096:
            for k in [k for k, v in _rl_hits.items() if not v]:
                _rl_hits.pop(k, None)
        return True


# --- Path containment ------------------------------------------------------

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def valid_segment(s) -> bool:
    """A single path component that is safe to place in a filesystem path:
    letters, digits, underscore, hyphen only. No dots (so '..' is impossible),
    no separators, 1-64 chars."""
    return isinstance(s, str) and bool(_SEGMENT_RE.match(s))


def resolve_within(base: str, *segments: str):
    """Join `segments` under `base` and return the realpath ONLY if it stays
    inside `base`; otherwise None. Defence-in-depth behind valid_segment():
    resolves symlinks and '..' before checking containment."""
    base_real = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base_real, *segments))
    if target == base_real or target.startswith(base_real + os.sep):
        return target
    return None

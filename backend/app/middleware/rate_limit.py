"""App-level rate limiting - previously absent entirely (confirmed by grep:
every existing 429/Retry-After handler in this codebase is about
*downstream* provider limits - Gemini/Veo/Postiz - not this app limiting
its own users). In-memory, per-client-IP, sliding window - same
single-process-scale trade-off as auth/login_throttle.py (this app's own
job-queue/background.py already carries the same caveat): doesn't survive
a restart or get shared across replicas, and will need moving to a shared
store (Redis) if this ever runs as more than one instance. Closes the
actual gap that exists today in the meantime.

Deliberately coarse and global (one limit for the whole app) rather than
per-route tuning - this is meant to catch runaway/abusive request volume
(a script hammering the app), not to enforce fine-grained business quotas
(those are separate concerns - e.g. the reel cost cap, or the per-account
login_throttle, which already covers the specific "brute-force one
account's password" threat this general limiter isn't tuned for).
"""

import time
from collections import defaultdict
from threading import Lock

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

_WINDOW_SECONDS = 60
_MAX_REQUESTS_PER_WINDOW = 240  # generous - catches abuse, not normal use, even with the board's own polling

_lock = Lock()
_hits: dict[str, list[float]] = defaultdict(list)


def _client_ip(request: Request) -> str:
    # Trusts X-Forwarded-For's first hop when present (this app expects to
    # sit behind a reverse proxy in any real deployment) - falls back to
    # the direct connecting IP for local/no-proxy setups. Not hardened
    # against a spoofed header from an untrusted direct client; fine for
    # this coarse, abuse-catching limiter, not something to rely on for
    # per-IP security decisions elsewhere.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        ip = _client_ip(request)
        now = time.monotonic()
        with _lock:
            recent = [t for t in _hits[ip] if now - t < _WINDOW_SECONDS]
            recent.append(now)
            _hits[ip] = recent
            count = len(recent)
        if count > _MAX_REQUESTS_PER_WINDOW:
            return JSONResponse(
                {"detail": "Too many requests - please slow down and try again shortly."},
                status_code=429,
            )
        return await call_next(request)

"""In-memory login-attempt throttling, keyed by the submitted email - not
Redis/DB-backed, consistent with this app's current single-process
architecture (worker/background.py's own "personal-tool, single-process
scale" framing applies here too). A real multi-replica deployment (the
infra-scaling phase of the SaaS-readiness plan) will need this moved to a
shared store (Redis) since this in-memory state doesn't survive a restart
or get shared across replicas - acceptable for now since it closes the
actual gap that exists today: /login had no throttling at all, making any
single account trivially brute-forceable regardless of password strength.

Deliberately keyed by email, not IP - the threat this addresses is
brute-forcing one specific account's password; IP-based limiting (against
credential-stuffing/enumeration across many accounts from one source) is a
separate, broader concern already tracked as its own item in the
SaaS-readiness plan, not folded in here to keep this fix small and its
threat model clear.
"""

import time
from collections import defaultdict
from threading import Lock

_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 15 * 60
_LOCKOUT_SECONDS = 15 * 60

_lock = Lock()
_attempts: dict[str, list[float]] = defaultdict(list)
_locked_until: dict[str, float] = {}


def _normalize(email: str) -> str:
    return email.strip().lower()


def is_locked_out(email: str) -> bool:
    key = _normalize(email)
    with _lock:
        until = _locked_until.get(key)
        if until and until > time.monotonic():
            return True
        if until:
            del _locked_until[key]
        return False


def record_failure(email: str) -> None:
    """Call after a failed authentication attempt. Locks the account out for
    _LOCKOUT_SECONDS once _MAX_ATTEMPTS failures land within _WINDOW_SECONDS
    of each other - resets the attempt count on locking (not sliding
    indefinitely) so an account that just got locked out doesn't need its
    old attempts to also expire before it can be tried again after the
    lockout itself ends."""
    key = _normalize(email)
    now = time.monotonic()
    with _lock:
        recent = [t for t in _attempts[key] if now - t < _WINDOW_SECONDS]
        recent.append(now)
        if len(recent) >= _MAX_ATTEMPTS:
            _locked_until[key] = now + _LOCKOUT_SECONDS
            _attempts[key] = []
        else:
            _attempts[key] = recent


def record_success(email: str) -> None:
    key = _normalize(email)
    with _lock:
        _attempts.pop(key, None)
        _locked_until.pop(key, None)

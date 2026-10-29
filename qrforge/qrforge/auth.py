"""Password-gate helpers for QRForge. No Flask import, so they are easy to test.

The gate is optional: it is active only when the QRFORGE_PASSWORD environment
variable is set. The password is never stored in the code base.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import threading
import time
from collections import deque


def password_matches(candidate: str, expected: str) -> bool:
    """Constant-time password comparison."""
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def session_fingerprint(secret_key: str, password: str) -> str:
    """Value stored in the signed session cookie after a successful login.

    It is derived from the password, so changing QRFORGE_PASSWORD
    automatically signs everyone out.
    """
    message = b"qrforge-auth-v1:" + password.encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()[:32]


class LoginThrottle:
    """Blocks an address after too many wrong passwords (in memory, per process).

    After ``max_failures`` wrong attempts within ``window_seconds`` the address
    is refused until the oldest failure ages out of the window. A successful
    login clears the address's record.
    """

    def __init__(self, max_failures: int = 5, window_seconds: int = 900, clock=time.monotonic):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        attempts = self._failures.setdefault(key, deque())
        while attempts and now - attempts[0] >= self.window_seconds:
            attempts.popleft()
        return attempts

    def retry_after(self, key: str) -> int:
        """Seconds until ``key`` may try again, or 0 if it may try now."""
        with self._lock:
            now = self._clock()
            attempts = self._prune(key, now)
            if len(attempts) < self.max_failures:
                return 0
            return max(1, math.ceil(self.window_seconds - (now - attempts[0])))

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            if len(self._failures) > 10_000:  # bound memory under a flood of distinct addresses
                self._failures = {k: v for k, v in self._failures.items() if v and now - v[-1] < self.window_seconds}
            self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

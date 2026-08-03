"""A small fixed-window rate limiter for the PUBLIC customer-gallery routes.

Everything else on this API sits behind the service token
(:func:`api.auth.service_token_allows`), so ``/j/{code}`` is the one surface an
unauthenticated stranger can reach. Two things make it worth metering:

* it resolves a short code, and a *miss* used to scan every job on disk (now indexed
  — see :meth:`api.jobs.JobStore.find_by_gallery_token` — but still work);
* the locked page polls ``/j/{code}/state`` every few seconds per open tab, so
  legitimate traffic is chatty and abnormal traffic is easy to hide inside.

Not a security control — the 65-bit code is what makes the page unguessable. This
caps the *cost* of someone trying anyway, and of a page left open for a week.

Deliberately dependency-free and in-process: one uvicorn worker's counters are its
own. That is fine for the job it does (blunting a flood from one source); a
distributed limit would need Redis and isn't worth it at dropzone volume. Fixed
window rather than a token bucket for the same reason — one integer per caller.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

#: Fixed window length. Short enough that a blocked caller recovers quickly, long
#: enough to smooth the `/state` poll (10 requests/minute per open page).
WINDOW_S = 60.0

#: Stop the counter dict growing without bound when a caller rotates addresses.
#: Well above the number of distinct customers a dropzone sees in a minute.
_MAX_TRACKED = 4096


class FixedWindowLimiter:
    """Counts requests per caller per :data:`WINDOW_S`. Thread-safe, bounded."""

    def __init__(
        self,
        limit_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        #: ``<= 0`` disables the limiter entirely (:meth:`allow` always True).
        self.limit = int(limit_per_minute)
        self._clock = clock
        self._lock = threading.Lock()
        #: caller key → (window start, count in this window)
        self._hits: dict[str, tuple[float, int]] = {}

    def allow(self, key: str) -> tuple[bool, int]:
        """``(allowed, retry_after_seconds)`` for one request from ``key``.

        ``retry_after`` is 0 when allowed, else the seconds left in the window — so the
        caller gets a truthful ``Retry-After`` instead of a guess.
        """
        if self.limit <= 0:
            return (True, 0)
        now = self._clock()
        with self._lock:
            start, count = self._hits.get(key, (now, 0))
            if now - start >= WINDOW_S:  # window rolled over
                start, count = now, 0
            count += 1
            self._hits[key] = (start, count)
            if len(self._hits) > _MAX_TRACKED:
                self._prune(now)
            if count > self.limit:
                return (False, max(1, int(WINDOW_S - (now - start)) + 1))
        return (True, 0)

    def _prune(self, now: float) -> None:
        """Drop callers whose window has expired. Called under the lock."""
        stale = [k for k, (start, _) in self._hits.items() if now - start >= WINDOW_S]
        for k in stale:
            del self._hits[k]
        if len(self._hits) > _MAX_TRACKED:
            # Everything is live and we're still over: forget the lot rather than grow.
            # A limiter that leaks memory is a worse outage than one that briefly
            # forgives.
            self._hits.clear()

    def reset(self) -> None:
        """Forget every counter (tests, and a config reload)."""
        with self._lock:
            self._hits.clear()


def caller_key(client_host: str | None, forwarded_for: str | None) -> str:
    """Identify the caller for metering: the real client IP where we can know it.

    Behind the lockdown proxy every request arrives from the proxy's own address, so
    metering on that would put the whole internet in one bucket and throttle real
    customers the moment anyone scanned. ``X-Forwarded-For``'s first hop is the client
    the proxy saw, so it wins when present — the proxy configs in
    ``deploy/PROXY_LOCKDOWN.md`` set it.

    Spoofable by a direct caller, which is *why* this is a cost cap and not a security
    control: the worst a spoofer achieves is metering themselves separately.
    """
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return client_host or "unknown"

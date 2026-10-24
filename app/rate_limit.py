"""Rate limits on /v1.

Four routes are limited (the numbers live in app/config.py):

- GET /v1/shapid/{shap_id}  per client IP, strict. Checked BEFORE
  authentication, so probing without a token is limited too.
- POST /v1/users            per client IP, strict. Registration is rare.
- POST /v1/vouchers/lookup  per access token. Abuse control: the route is
  already authenticated.
- GET /v1/deposits/{id}     per access token, set well above the app's
  polling (once a second for the first 30 s, then every 3 s, then every 5 s:
  src/polling.ts in the app).

The per-token limits are checked AFTER authentication, so a made-up token is
a 401 and never becomes a key: nobody can fill the store with junk.

Keys
----
A key is the bucket name plus either the client IP or a SHA-256 of the access
token (domain-separated, so it is not the `user_sessions.token_hash` value
either). Never a PIN, voucher token, ID number, account number or ShapID: a
value in the limiter's store is a value in one more place it can leak from.

The client IP
-------------
Render's proxy APPENDS the connecting address to X-Forwarded-For and keeps
whatever the client sent before it. Uvicorn's --forwarded-allow-ips "*" makes
`request.client` the LEFTMOST entry, which the client wrote itself, so it
cannot be used here: a new fake address per request would get a fresh limit
every time. We take the entry `trusted_proxy_hops` from the RIGHT instead,
the one our own proxy added. A header that is absent falls back to the socket
peer; one that is malformed or too short shares a single "unknown" bucket,
so a broken header is never a way around the limit. IPv6 addresses are keyed
by their /64, which one subscriber typically gets whole.

State lives in process memory
-----------------------------
Counters are kept in this process only. That is correct for exactly one
uvicorn worker on one instance, which is what render.yaml runs today. With
N workers or instances every client gets N times the limit, and a restart
forgets everything. More than one process needs a shared store (Redis
INCR/EXPIRE, or a Postgres table) behind the same `hit` interface, keyed the
same way. create_app logs a warning when WEB_CONCURRENCY asks for more than
one worker.
"""

import hashlib
import ipaddress
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request

from app.config import Settings
from app.deposits import Refused

UNKNOWN_CLIENT = "unknown"
# Sweep keys that have gone quiet every this many hits, so memory stays bounded.
SWEEP_EVERY = 1000


class RateLimited(Refused):
    """429 {"reason": "rate_limited"} with Retry-After (see app/routes/v1.py)."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("rate_limited")
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class Limit:
    requests: int
    window_seconds: int


@dataclass(frozen=True)
class Limits:
    shapid: Limit
    register: Limit
    lookup: Limit
    deposit_status: Limit

    @classmethod
    def from_settings(cls, s: Settings) -> "Limits":
        return cls(
            shapid=Limit(s.rate_limit_shapid_requests, s.rate_limit_shapid_window_seconds),
            register=Limit(s.rate_limit_register_requests, s.rate_limit_register_window_seconds),
            lookup=Limit(s.rate_limit_lookup_requests, s.rate_limit_lookup_window_seconds),
            deposit_status=Limit(
                s.rate_limit_deposit_status_requests, s.rate_limit_deposit_status_window_seconds
            ),
        )


class RateLimiter:
    """A sliding-window log per key: the times of the last `requests` hits.
    In process memory only; see the module docstring."""

    def __init__(
        self,
        limits: Limits,
        trusted_proxy_hops: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self.trusted_proxy_hops = trusted_proxy_hops
        self.clock = clock
        self._hits: dict[str, tuple[int, deque[float]]] = {}
        self._lock = threading.Lock()  # sync routes run on a thread pool
        self._since_sweep = 0

    def hit(self, key: str, limit: Limit) -> None:
        """Count one request against `key`, or raise RateLimited."""
        now = self.clock()
        with self._lock:
            self._maybe_sweep(now)
            _, times = self._hits.setdefault(key, (limit.window_seconds, deque()))
            while times and times[0] <= now - limit.window_seconds:
                times.popleft()
            if len(times) >= limit.requests:
                retry_after = times[0] + limit.window_seconds - now
                raise RateLimited(max(1, math.ceil(retry_after)))
            times.append(now)

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._hits)

    def _maybe_sweep(self, now: float) -> None:
        self._since_sweep += 1
        if self._since_sweep < SWEEP_EVERY:
            return
        self._since_sweep = 0
        stale = [k for k, (window, times) in self._hits.items() if not times or times[-1] <= now - window]
        for key in stale:
            del self._hits[key]


def _normalise_ip(value: str) -> str | None:
    """An address from a header or socket, as a key: IPv4 whole, IPv6 by /64.
    None for anything that is not an IP address (ports are allowed)."""
    value = value.strip()
    if len(value) > 64:
        return None
    if value.startswith("["):  # [v6]:port
        value = value[1 : value.find("]")] if "]" in value else ""
    elif value.count(":") == 1:  # v4:port
        value = value.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def client_ip(request: Request, trusted_proxy_hops: int) -> str:
    """The client's address for rate limiting, never the client's say-so.

    `trusted_proxy_hops` is how many proxies in front of us append to
    X-Forwarded-For (1 on Render). 0 ignores the header entirely."""
    forwarded = request.headers.getlist("x-forwarded-for")
    if trusted_proxy_hops > 0 and forwarded:
        hops = [h.strip() for h in ",".join(forwarded).split(",")]
        if len(hops) < trusted_proxy_hops:
            return UNKNOWN_CLIENT
        return _normalise_ip(hops[-trusted_proxy_hops]) or UNKNOWN_CLIENT
    # No header: uvicorn left the socket peer alone, and the peer is real.
    peer = request.client.host if request.client else ""
    return _normalise_ip(peer) or (peer[:64] if peer else UNKNOWN_CLIENT)


def ip_key(bucket: str, ip: str) -> str:
    return f"{bucket}:ip:{ip}"


def token_key(bucket: str, authorization: str | None) -> str:
    """A key from the bearer token's hash. Call only after authentication."""
    _, _, token = (authorization or "").partition(" ")
    digest = hashlib.sha256(b"kasideposit-rate-limit\x00" + token.strip().encode()).hexdigest()
    return f"{bucket}:token:{digest}"

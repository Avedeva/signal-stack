from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from signal_stack.config import settings


class FetchError(RuntimeError):
    pass


class BoundedTTLCache:
    """Small thread-safe LRU cache with expiration based on a monotonic clock."""

    def __init__(self, *, maxsize: int, ttl_seconds: float, clock=time.monotonic):
        if maxsize < 1 or ttl_seconds <= 0:
            raise ValueError("Cache capacity and TTL must be positive")
        self.maxsize = maxsize
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._items: OrderedDict[object, tuple[float, object]] = OrderedDict()

    def _expire(self, now: float) -> None:
        for key, (expires_at, _value) in list(self._items.items()):
            if expires_at <= now:
                self._items.pop(key, None)

    def get(self, key):
        now = self._clock()
        with self._lock:
            self._expire(now)
            item = self._items.get(key)
            if item is None:
                return None
            expires_at, value = item
            self._items.move_to_end(key)
            return value

    def set(self, key, value) -> None:
        now = self._clock()
        with self._lock:
            self._expire(now)
            self._items[key] = (now + self.ttl_seconds, value)
            self._items.move_to_end(key)
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            self._expire(self._clock())
            return len(self._items)


@dataclass
class _HostState:
    lock: threading.Lock
    last_request: float = 0.0
    active: int = 0


class HostPacer:
    """Bounded per-host pacing state; lock objects are retained while referenced."""

    def __init__(self, *, capacity: int, ttl_seconds: float, clock=time.monotonic, sleeper=time.sleep):
        if capacity < 1 or ttl_seconds <= 0:
            raise ValueError("Pacing capacity and TTL must be positive")
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._sleep = sleeper
        self._guard = threading.Lock()
        self._states: OrderedDict[str, _HostState] = OrderedDict()
        self._overflow = _HostState(threading.Lock())

    def _state(self, host: str) -> tuple[_HostState, str | None]:
        now = self._clock()
        with self._guard:
            expired = [key for key, state in self._states.items() if state.active == 0 and now - state.last_request >= self.ttl_seconds]
            for key in expired:
                self._states.pop(key, None)
            state = self._states.get(host)
            key = host
            if state is None:
                if len(self._states) >= self.capacity:
                    evictable = next((name for name, value in self._states.items() if value.active == 0 and now - value.last_request >= self.ttl_seconds), None)
                    if evictable is not None:
                        self._states.pop(evictable, None)
                if len(self._states) < self.capacity:
                    state = _HostState(threading.Lock())
                    self._states[host] = state
                else:
                    state = self._overflow
                    key = None
            if key is not None:
                self._states.move_to_end(key)
            state.active += 1
            return state, key

    def pace(self, host: str, delay_seconds: float, *, deadline: float | None = None) -> None:
        state, key = self._state(host)
        try:
            with state.lock:
                now = self._clock()
                pause = max(0.0, delay_seconds - (now - state.last_request)) if state.last_request else 0.0
                if pause:
                    if deadline is not None and now + pause >= deadline:
                        self._sleep(max(0.0, deadline - now))
                        raise FetchError("Total request deadline exceeded while respecting host pacing")
                    self._sleep(pause)
                now = self._clock()
                if deadline is not None and now >= deadline:
                    raise FetchError("Total request deadline exceeded while respecting host pacing")
                state.last_request = now
        finally:
            with self._guard:
                state.active -= 1
                if key is not None and key in self._states:
                    self._states.move_to_end(key)

    def __len__(self) -> int:
        with self._guard:
            return len(self._states)


def normalize_url(url: str) -> str:
    """Normalize an HTTP URL and reject credential-bearing or unusual-port URLs."""
    candidate = (url or "").strip()
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError as exc:
        raise ValueError("Malformed URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are supported")
    if not parts.hostname or parts.username or parts.password:
        raise ValueError("URL must contain a host and must not contain credentials")
    if port not in {None, 80, 443}:
        raise ValueError("Non-standard URL ports are not allowed")
    raw_host = parts.hostname.rstrip(".")
    try:
        ip = ipaddress.ip_address(raw_host)
        host = ip.compressed.lower()
        netloc_host = f"[{host}]" if ip.version == 6 else host
    except ValueError:
        host = raw_host.encode("idna").decode("ascii").lower()
        netloc_host = host
    netloc = netloc_host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc_host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def _resolve_host(host: str, port: int, *, deadline: float | None = None) -> list[str]:
    """Resolve through the OS resolver in a killable child when called inside a timed request."""
    if deadline is None:
        try:
            return sorted({item[4][0].split("%", 1)[0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        except OSError as exc:
            raise FetchError(f"Host could not be resolved: {host}") from exc
    resolver_code = (
        "import json,socket,sys; "
        "print(json.dumps(sorted({item[4][0].split('%',1)[0] for item in "
        "socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),type=socket.SOCK_STREAM)})))"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", resolver_code, host, str(port)],
            check=False,
            capture_output=True,
            text=True,
            timeout=_remaining(deadline),
        )
    except subprocess.TimeoutExpired as exc:
        raise FetchError("Total request deadline exceeded during DNS resolution") from exc
    if completed.returncode != 0:
        raise FetchError(f"Host could not be resolved: {host}")
    try:
        addresses = json.loads(completed.stdout)
    except (ValueError, TypeError) as exc:
        raise FetchError(f"Host returned invalid DNS resolution data: {host}") from exc
    _remaining(deadline)
    return [str(address) for address in addresses if isinstance(address, str)]


def assert_public_host(url: str, *, deadline: float | None = None) -> list[str]:
    """Resolve and return only globally routable IPs; reject mixed/private answers."""
    normalized = normalize_url(url)
    parsed = urlsplit(normalized)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        try:
            direct_ip = ipaddress.ip_address(host or "")
            addresses = {direct_ip.compressed}
        except ValueError:
            addresses = set(_resolve_host(host, port, deadline=deadline))
    except OSError as exc:
        raise FetchError(f"Host could not be resolved: {host}") from exc
    if not addresses:
        raise FetchError(f"Host has no addresses: {host}")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise FetchError(f"Private or non-public network addresses are not allowed: {host}")
    return sorted(addresses)


@dataclass
class FetchResult:
    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes

    @property
    def text(self) -> str:
        content_type = next((v for k, v in self.headers.items() if k.lower() == "content-type"), "")
        encoding = "utf-8"
        for part in content_type.split(";")[1:]:
            if "charset=" in part.lower():
                encoding = part.split("=", 1)[1].strip(" \"'")
        try:
            return self.content.decode(encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


_host_pacer = HostPacer(
    capacity=settings.host_pacing_capacity,
    ttl_seconds=max(86_400.0, settings.domain_delay_seconds * 2),
)
_robots_cache = BoundedTTLCache(maxsize=settings.robots_cache_capacity, ttl_seconds=86_400.0)
_robots_locks = tuple(threading.Lock() for _ in range(64))


def _pace(host: str, *, deadline: float | None = None) -> None:
    _host_pacer.pace(host, settings.domain_delay_seconds, deadline=deadline)


def _remaining(deadline: float | None) -> float:
    if deadline is None:
        return settings.request_timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchError("Total request deadline exceeded")
    return remaining


class _DeadlineSocketReader:
    """File-like socket reader that resets the socket timeout against one total deadline."""

    def __init__(self, sock, deadline: float):
        self.sock = sock
        self.deadline = deadline
        self._keepalive = sock.makefile("rb")
        self.buffer = bytearray()
        self.closed = False

    def _recv(self, size: int = 65536) -> bytes:
        self.sock.settimeout(_remaining(self.deadline))
        return self.sock.recv(max(1, size))

    def _fill(self, required: int) -> None:
        while len(self.buffer) < required and not self.closed:
            chunk = self._recv(max(1, required - len(self.buffer)))
            if not chunk:
                self.closed = True
                break
            self.buffer.extend(chunk)

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            while not self.closed:
                chunk = self._recv()
                if not chunk:
                    self.closed = True
                    break
                self.buffer.extend(chunk)
            size = len(self.buffer)
        else:
            self._fill(size)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def read1(self, size: int = -1) -> bytes:
        if not self.buffer and not self.closed:
            chunk = self._recv(65536 if size is None or size < 0 else size)
            if chunk:
                self.buffer.extend(chunk)
            else:
                self.closed = True
        if size is None or size < 0:
            size = len(self.buffer)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def readline(self, size: int = -1) -> bytes:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                stop = newline + 1
                break
            if size >= 0 and len(self.buffer) >= size:
                stop = size
                break
            if self.closed:
                stop = len(self.buffer)
                break
            chunk = self._recv(1 if size == 1 else 4096)
            if not chunk:
                self.closed = True
                stop = len(self.buffer)
                break
            self.buffer.extend(chunk)
        if size >= 0:
            stop = min(stop, size)
        result = bytes(self.buffer[:stop])
        del self.buffer[:stop]
        return result

    def readinto(self, target) -> int:
        data = self.read1(len(target))
        target[:len(data)] = data
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True
        if self._keepalive is not None:
            self._keepalive.close()
            self._keepalive = None


class _DeadlineHTTPResponse(http.client.HTTPResponse):
    def __init__(self, sock, *, deadline: float, method: str | None = None, url: str | None = None):
        self._total_deadline = deadline
        super().__init__(sock, method=method, url=url)
        original_fp = self.fp
        self.fp = _DeadlineSocketReader(sock, deadline)
        original_fp.close()


def _http_get(url: str, *, max_bytes: int, resolved_ips: list[str] | None = None, deadline: float | None = None) -> FetchResult:
    """Fetch with the socket pinned to the IP that passed SSRF checks (blocks DNS rebinding)."""
    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    addresses = resolved_ips or assert_public_host(normalized, deadline=deadline)
    target = parts.path or "/"
    if parts.query:
        target += f"?{parts.query}"
    errors = []
    for address in addresses:
        connection = None
        try:
            timeout = min(settings.request_timeout, _remaining(deadline))
            if parts.scheme == "https":
                connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
            else:
                connection = http.client.HTTPConnection(host, port, timeout=timeout)
            connection._create_connection = lambda _address, timeout, source_address=None, ip=address, p=port: socket.create_connection((ip, p), timeout, source_address)
            if deadline is not None:
                connection.response_class = lambda sock, method=None, url=None: _DeadlineHTTPResponse(sock, deadline=deadline, method=method, url=url)
            connection.request("GET", target, headers={
                "Host": parts.netloc,
                "User-Agent": settings.user_agent,
                "Accept": "text/html, application/rss+xml, application/atom+xml, application/xml, text/xml, text/plain",
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            _remaining(deadline)
            if connection.sock is not None:
                connection.sock.settimeout(min(settings.request_timeout, _remaining(deadline)))
            response = connection.getresponse()
            _remaining(deadline)
            headers = {key: value for key, value in response.getheaders()}
            length = next((value for key, value in headers.items() if key.lower() == "content-length"), None)
            if length:
                try:
                    if int(length) > max_bytes:
                        raise FetchError("Response exceeded the configured size limit")
                except ValueError:
                    pass
            if response.status in {301, 302, 303, 307, 308}:
                body = b""
            else:
                chunks = []
                total = 0
                while True:
                    _remaining(deadline)
                    chunk = response.read1(min(65_536, max_bytes + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError("Response exceeded the configured size limit")
                    chunks.append(chunk)
                body = b"".join(chunks)
            return FetchResult(normalized, response.status, headers, body)
        except FetchError:
            raise
        except socket.timeout as exc:
            message = "Total request deadline exceeded" if deadline is not None and time.monotonic() >= deadline - .01 else "HTTP request timed out"
            raise FetchError(message) from exc
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            if deadline is not None and time.monotonic() >= deadline:
                raise FetchError("Total request deadline exceeded") from exc
            errors.append(str(exc))
        finally:
            if connection is not None:
                connection.close()
    raise FetchError(f"Request failed for {host}: {'; '.join(errors)[:600]}")


def _robots_parser(url: str, *, deadline: float, resolved_ips: list[str]) -> RobotFileParser:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    cached = _robots_cache.get(origin)
    if cached is not None:
        return cached
    lock = _robots_locks[hash(origin) % len(_robots_locks)]
    with lock:
        cached = _robots_cache.get(origin)
        if cached is not None:
            return cached
        robots_url = f"{origin}/robots.txt"
        _pace(parts.hostname or "", deadline=deadline)
        response = _http_get(robots_url, max_bytes=min(settings.max_response_bytes, 512_000), resolved_ips=resolved_ips, deadline=deadline)
        parser = RobotFileParser(robots_url)
        if response.status_code == 404:
            parser.parse([])
        elif response.status_code == 200:
            parser.parse(response.text.splitlines())
        else:
            # Be conservative when access rules cannot be read.
            parser.parse(["User-agent: *", "Disallow: /"])
        _robots_cache.set(origin, parser)
        return parser


def _fetch(url: str, *, max_bytes: int, redirects_left: int, deadline: float) -> FetchResult:
    _remaining(deadline)
    normalized = normalize_url(url)
    resolved_ips = assert_public_host(normalized, deadline=deadline)
    _remaining(deadline)
    host = urlsplit(normalized).hostname or ""
    parser = _robots_parser(normalized, deadline=deadline, resolved_ips=resolved_ips)
    if not parser.can_fetch(settings.user_agent, normalized):
        raise FetchError("robots.txt disallows this URL")
    _pace(host, deadline=deadline)
    response = _http_get(normalized, max_bytes=max_bytes, resolved_ips=resolved_ips, deadline=deadline)
    if response.status_code in {301, 302, 303, 307, 308}:
        location = next((v for k, v in response.headers.items() if k.lower() == "location"), None)
        if not location or redirects_left <= 0:
            raise FetchError("Redirect limit reached or redirect target missing")
        return _fetch(urljoin(normalized, location), max_bytes=max_bytes, redirects_left=redirects_left - 1, deadline=deadline)
    if response.status_code < 200 or response.status_code >= 300:
        raise FetchError(f"Source returned HTTP {response.status_code}")
    content_type = next((v for k, v in response.headers.items() if k.lower() == "content-type"), "").split(";", 1)[0].strip().lower()
    allowed = {"text/html", "application/xhtml+xml", "application/rss+xml", "application/atom+xml", "application/xml", "text/xml", "text/plain"}
    if content_type and content_type not in allowed:
        raise FetchError(f"Unsupported content type: {content_type}")
    return response


def fetch_public_url(url: str, *, max_bytes: int | None = None) -> FetchResult:
    deadline = time.monotonic() + settings.total_request_deadline_seconds
    return _fetch(url, max_bytes=max_bytes or settings.max_response_bytes, redirects_left=4, deadline=deadline)


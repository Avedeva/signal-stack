import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import urlsplit, urlunsplit

import pytest

from signal_stack.services.extraction import extract_documents, parse_feed
from signal_stack.services.security import FetchError, FetchResult, assert_public_host, fetch_public_url, normalize_url
import signal_stack.services.security as security


def test_url_normalization_and_rejections():
    assert normalize_url(" HTTPS://Example.COM/path?q=1#fragment ") == "https://example.com/path?q=1"
    with pytest.raises(ValueError):
        normalize_url("file:///C:/secret")
    with pytest.raises(ValueError):
        normalize_url("https://user:password@example.com/")
    with pytest.raises(ValueError):
        normalize_url("https://example.com:8443/")


def test_ssrf_blocks_private_resolved_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))])
    with pytest.raises(FetchError, match="non-public"):
        assert_public_host("https://example.com")


def test_fetch_obeys_robots_and_enforces_response_limit(monkeypatch):
    security._robots_cache.clear()
    monkeypatch.setattr(security, "_pace", lambda host, **kwargs: None)
    monkeypatch.setattr(security, "_resolve_host", lambda *a, **k: ["93.184.216.34"])
    calls = []

    def fake_get(url, *, max_bytes, resolved_ips=None, deadline=None):
        calls.append(url)
        if url.endswith("robots.txt"):
            return FetchResult(url, 200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /private\n")
        content = b"<html>ok</html>"
        if len(content) > max_bytes:
            raise FetchError("Response exceeded the configured size limit")
        return FetchResult(url, 200, {"Content-Type": "text/html", "Content-Length": str(len(content))}, content)

    monkeypatch.setattr(security, "_http_get", fake_get)
    fetched = fetch_public_url("https://example.com/public")
    assert fetched.text == "<html>ok</html>"
    with pytest.raises(FetchError, match="robots.txt disallows"):
        fetch_public_url("https://example.com/private/page")
    with pytest.raises(FetchError, match="size limit"):
        fetch_public_url("https://example.com/large", max_bytes=4)
    assert calls.count("https://example.com/robots.txt") == 1


def test_rss_and_html_extraction_preserve_source_urls():
    rss = b'''<?xml version="1.0"?><rss version="2.0"><channel><item><title>New program launch</title><link>https://publisher.example/story</link><description>Article body and details.</description><pubDate>Mon, 10 Nov 2025 10:00:00 GMT</pubDate></item></channel></rss>'''
    docs = parse_feed(FetchResult("https://feed.example/rss", 200, {"Content-Type": "application/rss+xml"}, rss))
    assert len(docs) == 1
    assert docs[0].url == "https://publisher.example/story"
    assert docs[0].published_at.year == 2025
    html = b'<html><head><title>Site</title><link rel="canonical" href="/article"/></head><body><nav>skip</nav><h1>Headline</h1><p>Real article content.</p></body></html>'
    page = extract_documents(FetchResult("https://source.example/old", 200, {"Content-Type": "text/html"}, html), "website")[0]
    assert page.url == "https://source.example/article"
    assert "Headline" in page.content and "skip" not in page.content


def test_html_article_metadata_extracts_publication_and_modification_dates():
    html = b'''<html><head><title>Launch</title><meta property="og:type" content="article">
    <meta property="article:published_time" content="2026-11-10T09:30:00+05:30">
    <meta property="article:modified_time" content="2026-11-11T10:00:00Z"></head>
    <body><article><h1>Launch</h1><p>A substantial article describing the launch and its customer impact.</p></article></body></html>'''
    document = extract_documents(FetchResult("https://publisher.example/news/launch", 200, {"Content-Type": "text/html"}, html), "website")[0]
    assert document.published_at.isoformat() == "2026-11-10T04:00:00"
    assert document.modified_at.isoformat() == "2026-11-11T10:00:00"
    assert document.is_article is True


def test_jsonld_article_dates_are_extracted_and_invalid_dates_remain_unknown():
    html = b'''<html><head><title>Report</title><script type="application/ld+json">
    {"@type":"NewsArticle","datePublished":"2026-11-12T00:00:00Z","dateModified":"2026-11-13"}
    </script></head><body><h1>Report</h1><p>Long-form public report with evidence and information.</p></body></html>'''
    article = extract_documents(FetchResult("https://publisher.example/report", 200, {"Content-Type": "text/html"}, html), "website")[0]
    assert article.published_at.isoformat() == "2026-11-12T00:00:00"
    assert article.modified_at.isoformat() == "2026-11-13T00:00:00"
    assert article.is_article is True
    no_date = extract_documents(FetchResult("https://publisher.example/undated", 200, {"Content-Type": "text/html"}, b"<html><title>Page</title><p>Undated details.</p></html>"), "website")[0]
    assert no_date.published_at is None
    assert no_date.modified_at is None


def test_jsonld_event_date_is_kept_separate_from_publication_and_modification():
    html = b'''<html><head><title>Annual summit</title><script type="application/ld+json">
    {"@type":"Event","startDate":"2026-12-02T09:00:00-05:00","datePublished":"2026-10-01","dateModified":"2026-10-03"}
    </script></head><body><h1>Annual summit</h1><p>Event details and registration information.</p></body></html>'''
    document = extract_documents(FetchResult("https://publisher.example/summit", 200, {"Content-Type": "text/html"}, html), "website")[0]
    assert document.event_date.isoformat() == "2026-12-02T14:00:00"
    assert document.published_at.isoformat() == "2026-10-01T00:00:00"
    assert document.modified_at.isoformat() == "2026-10-03T00:00:00"


def test_bounded_ttl_cache_expires_and_evicts_lru_entries():
    now = [0.0]
    cache = security.BoundedTTLCache(maxsize=2, ttl_seconds=10, clock=lambda: now[0])
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1
    cache.set("c", 3)
    assert len(cache) == 2
    assert cache.get("b") is None
    now[0] = 11
    assert cache.get("a") is None
    assert cache.get("c") is None


def test_host_pacing_is_per_host_and_same_host_respects_delay():
    pacer = security.HostPacer(capacity=4, ttl_seconds=5)
    pacer.pace("example-a", 0.08)
    started = time.monotonic()
    same_host = threading.Thread(target=lambda: pacer.pace("example-a", 0.18))
    same_host.start()
    time.sleep(0.02)
    different_started = time.monotonic()
    pacer.pace("example-b", 0.18)
    different_elapsed = time.monotonic() - different_started
    same_host.join(timeout=1)
    assert not same_host.is_alive()
    assert different_elapsed < 0.1
    assert time.monotonic() - started >= 0.15


def test_bounded_caches_support_concurrent_read_and_write():
    cache = security.BoundedTTLCache(maxsize=64, ttl_seconds=10)
    def update(index):
        cache.set(str(index), index)
        return cache.get(str(index))
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(update, range(100))) == list(range(100))
    assert len(cache) <= 64


def _fetch_from_local_http_server(monkeypatch, path, *, deadline=1.0, max_bytes=1024):
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        probe.listen()
        try:
            client = socket.create_connection(probe.getsockname(), timeout=.1)
        except OSError as exc:
            pytest.skip(f"Sandbox blocks loopback HTTP integration tests: {exc}")
        else:
            client.close()
    except OSError as exc:
        pytest.skip(f"Sandbox blocks loopback HTTP integration tests: {exc}")
    finally:
        probe.close()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/robots.txt":
                body = b"User-agent: *\nAllow: /\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/never" or self.path == "/slow-headers":
                time.sleep(.5)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"late")
                except OSError:
                    pass
            elif self.path == "/trickle":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for _ in range(30):
                    try:
                        self.wfile.write(b"1\r\nx\r\n")
                        self.wfile.flush()
                    except OSError:
                        break
                    time.sleep(.03)
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass
            elif self.path == "/large":
                body = b"0123456789"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/target")
                self.end_headers()
            elif self.path == "/forbidden-redirect":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/target")
                self.end_headers()
            else:
                body = b"local response"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_create_connection = socket.create_connection
    original_normalize = security.normalize_url

    def local_normalize(url):
        parts = urlsplit(url)
        if parts.port:
            return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
        return original_normalize(url)

    def local_dns(host, port, *, deadline=None):
        if host == "public.example":
            return ["93.184.216.34"]
        return [item[4][0].split("%", 1)[0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]

    def local_connect(address, timeout=None, source_address=None):
        if address[0] == "93.184.216.34":
            address = ("127.0.0.1", server.server_port)
        return real_create_connection(address, timeout, source_address)

    monkeypatch.setattr(security, "settings", replace(security.settings, total_request_deadline_seconds=deadline, request_timeout=3, domain_delay_seconds=0))
    monkeypatch.setattr(security, "normalize_url", local_normalize)
    monkeypatch.setattr(security, "_resolve_host", local_dns)
    monkeypatch.setattr(socket, "create_connection", local_connect)
    security._robots_cache.clear()
    url = f"http://public.example:{server.server_port}{path}"
    try:
        return security.fetch_public_url(url, max_bytes=max_bytes)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_local_http_fetch_success_redirect_and_size_limit(monkeypatch):
    assert _fetch_from_local_http_server(monkeypatch, "/normal").content == b"local response"
    assert _fetch_from_local_http_server(monkeypatch, "/redirect").content == b"local response"
    with pytest.raises(FetchError, match="size limit"):
        _fetch_from_local_http_server(monkeypatch, "/large", max_bytes=4)


@pytest.mark.parametrize("path", ["/never", "/slow-headers", "/trickle"])
def test_local_http_fetch_enforces_one_wall_clock_deadline(monkeypatch, path):
    started = time.monotonic()
    with pytest.raises(FetchError, match="deadline|timed out|timeout"):
        _fetch_from_local_http_server(monkeypatch, path, deadline=.18)
    assert time.monotonic() - started < .8


def test_local_http_redirect_to_forbidden_address_is_revalidated(monkeypatch):
    with pytest.raises(FetchError, match="non-public"):
        _fetch_from_local_http_server(monkeypatch, "/forbidden-redirect")


def test_atom_published_and_updated_dates_are_not_conflated():
    atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Update</title><link href="https://publisher.example/update"/><published>2026-10-01T00:00:00Z</published><updated>2026-10-03T00:00:00Z</updated><summary>Article content and information.</summary></entry></feed>'''
    document = parse_feed(FetchResult("https://publisher.example/feed", 200, {"Content-Type": "application/atom+xml"}, atom))[0]
    assert document.published_at.isoformat() == "2026-10-01T00:00:00"
    assert document.modified_at.isoformat() == "2026-10-03T00:00:00"


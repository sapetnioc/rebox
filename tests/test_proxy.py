"""Tests of the allowlist proxy against a local HTTP server (no network needed)."""

from __future__ import annotations

import http.server
import socket
import threading
from pathlib import Path

import pytest

from rebox.config import AllowRule, domain_matches
from rebox.proxy import AllowlistProxy, Upstream, split_host_port


class Hello(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = f"hello {self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def web_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hello)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture()
def make_proxy(tmp_path: Path):
    proxies = []

    def factory(rules):
        proxy = AllowlistProxy(rules, tmp_path / "p.sock", log_path=tmp_path / "net.log")
        proxy.start()
        proxies.append(proxy)
        return proxy

    yield factory
    for proxy in proxies:
        proxy.stop()


def exchange(sock_path: Path, request: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(10)
        sock.connect(str(sock_path))
        sock.sendall(request)
        chunks = []
        while data := sock.recv(65536):
            chunks.append(data)
    return b"".join(chunks)


def tunnel_get(sock_path: Path, host: str, port: int) -> tuple[bytes, bytes]:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(10)
        sock.connect(str(sock_path))
        sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        status = b""
        while b"\r\n\r\n" not in status:
            data = sock.recv(1)
            if not data:
                return status, b""
            status += data
        if b" 200 " not in status.split(b"\r\n")[0]:
            return status, b""
        sock.sendall(f"GET /tunnel HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        body = b""
        while data := sock.recv(65536):
            body += data
    return status, body


@pytest.mark.parametrize("pattern,host,expected", [
    ("example.com", "example.com", True),
    ("example.com", "www.example.com", False),
    ("*.example.com", "www.example.com", True),
    ("*.example.com", "a.b.example.com", True),
    ("*.example.com", "example.com", False),
    ("*.example.com", "badexample.com", False),
    ("Example.COM.", "example.com", True),
])
def test_domain_matches(pattern, host, expected):
    assert domain_matches(pattern, host) is expected


def test_split_host_port():
    assert split_host_port("example.com:8443", 443) == ("example.com", 8443)
    assert split_host_port("example.com", 443) == ("example.com", 443)
    assert split_host_port("[::1]:80", 443) == ("::1", 80)


def test_http_and_connect_allowed(make_proxy, web_server):
    proxy = make_proxy([AllowRule("local", ["localhost"], [web_server], allow_private=True)])
    response = exchange(proxy.socket_path,
                        f"GET http://localhost:{web_server}/x?y=1 HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    assert response.startswith(b"HTTP/1.0 200") and response.endswith(b"hello /x?y=1")

    status, body = tunnel_get(proxy.socket_path, "localhost", web_server)
    assert b"200 Connection Established" in status
    assert body.endswith(b"hello /tunnel")
    assert sum(proxy.allowed.values()) == 2 and not proxy.denied


def test_unlisted_domain_and_port_denied(make_proxy, web_server):
    proxy = make_proxy([AllowRule("local", ["localhost"], [web_server], allow_private=True)])
    status, _ = tunnel_get(proxy.socket_path, "example.org", 443)
    assert status.startswith(b"HTTP/1.1 403")
    status, _ = tunnel_get(proxy.socket_path, "localhost", web_server + 1)  # port not allowed
    assert status.startswith(b"HTTP/1.1 403")
    response = exchange(proxy.socket_path, b"GET http://example.org/ HTTP/1.1\r\nHost: example.org\r\n\r\n")
    assert response.startswith(b"HTTP/1.1 403")
    assert set(proxy.denied) == {"example.org:443", f"localhost:{web_server + 1}", "example.org:80"}
    log = proxy.log_path.read_text()
    assert "DENY example.org:443" in log and "DENY example.org:80" in log


def test_private_addresses_refused_by_default(make_proxy, web_server):
    proxy = make_proxy([AllowRule("local", ["localhost"], [web_server])])
    status, _ = tunnel_get(proxy.socket_path, "localhost", web_server)
    assert status.startswith(b"HTTP/1.1 403")
    assert b"private" in status or "private" in proxy.log_path.read_text()


def test_bad_requests(make_proxy):
    proxy = make_proxy([])
    assert exchange(proxy.socket_path, b"nonsense\r\n\r\n").startswith(b"HTTP/1.1 400")
    assert exchange(proxy.socket_path, b"GET /relative HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 400")


def test_upstream_from_env():
    up = Upstream.from_env({"HTTPS_PROXY": "http://user:p%40ss@proxy.local:3128"})
    assert (up.host, up.port) == ("proxy.local", 3128)
    assert up.auth == "Basic dXNlcjpwQHNz"
    assert Upstream.from_env({}) is None

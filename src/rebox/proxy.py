"""Domain allowlist proxy for ``network = "allowlist"``.

The sandbox runs without any network interface except loopback. This proxy
runs on the host, listens on a Unix socket that is bind-mounted into the
sandbox, and a relay inside the sandbox exposes it as 127.0.0.1:3128
(HTTP_PROXY / HTTPS_PROXY). It supports:

- ``CONNECT host:port`` (HTTPS and any TLS protocol): checked on the host name,
  the encrypted stream is relayed untouched (no TLS interception);
- plain HTTP requests in absolute form (``GET http://host/path``).

Names are resolved on the host. Connections to loopback, private or link-local
addresses are refused unless the matching group sets ``allow_private = true``.
If the host itself needs an HTTP proxy (``https_proxy`` / ``http_proxy`` in the
environment of the tool), allowed connections are chained through it.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import datetime as dt
import ipaddress
import os
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from urllib.parse import unquote, urlsplit

from .config import AllowRule

READ_TIMEOUT = 30
CONNECT_TIMEOUT = 20
MAX_HEADER = 64 * 1024
HOP_BY_HOP = {"proxy-connection", "proxy-authorization", "connection", "keep-alive"}


@dataclass
class Upstream:
    host: str
    port: int
    auth: str | None = None

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Upstream | None":
        env = os.environ if environ is None else environ
        for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
            value = env.get(key)
            if not value:
                continue
            url = urlsplit(value if "://" in value else f"http://{value}")
            if url.scheme not in ("http", "") or not url.hostname:
                continue
            auth = None
            if url.username:
                creds = f"{unquote(url.username)}:{unquote(url.password or '')}"
                auth = "Basic " + base64.b64encode(creds.encode()).decode()
            return cls(url.hostname, url.port or 8080, auth)
        return None


class Denied(Exception):
    pass


def split_host_port(target: str, default_port: int) -> tuple[str, int]:
    if target.startswith("["):  # [IPv6]:port
        host, _, rest = target[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, _, port = target.rpartition(":") if target.count(":") == 1 else (target, "", "")
    return host, int(port) if port else default_port


def is_private(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified)


class AllowlistProxy:
    """Filtering proxy served on a Unix socket from a background thread."""

    def __init__(
        self,
        rules: list[AllowRule],
        socket_path: Path,
        log_path: Path | None = None,
        upstream: Upstream | None = None,
    ) -> None:
        self.rules = rules
        self.socket_path = socket_path
        self.log_path = log_path
        self.upstream = upstream
        self.allowed: collections.Counter[str] = collections.Counter()
        self.denied: collections.Counter[str] = collections.Counter()
        self._log: TextIO | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    # Lifecycle ------------------------------------------------------------- #
    def start(self) -> None:
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = self.log_path.open("a", buffering=1)
        self._thread = threading.Thread(target=self._run, name="allowlist-proxy", daemon=True)
        self._thread.start()
        self._ready.wait(10)
        if self._error:
            raise RuntimeError(f"Could not start the allowlist proxy: {self._error}") from self._error

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(5)
        if self._log:
            self._log.close()
        self.socket_path.unlink(missing_ok=True)

    def __enter__(self) -> "AllowlistProxy":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            server = loop.run_until_complete(
                asyncio.start_unix_server(self._handle, path=str(self.socket_path), limit=MAX_HEADER)
            )
            os.chmod(self.socket_path, 0o600)
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            server.close()
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.run_until_complete(asyncio.sleep(0))
            loop.close()

    # Policy ---------------------------------------------------------------- #
    def match(self, host: str, port: int) -> AllowRule | None:
        for rule in self.rules:
            if rule.matches(host, port):
                return rule
        return None

    def _record(self, verdict: str, host: str, port: int, detail: str = "") -> None:
        key = f"{host}:{port}"
        (self.allowed if verdict == "ALLOW" else self.denied)[key] += 1
        if self._log:
            stamp = dt.datetime.now().isoformat(timespec="seconds")
            self._log.write(f"{stamp} {verdict} {key} {detail}".rstrip() + "\n")

    def summary(self) -> str:
        allowed, denied = sum(self.allowed.values()), sum(self.denied.values())
        text = f"network allowlist: {allowed} connection(s) allowed, {denied} refused"
        if denied:
            hosts = ", ".join(h for h, _ in self.denied.most_common(10))
            text += f" ({hosts})"
        if self.log_path:
            text += f". Log: {self.log_path}"
        return text

    # Connection handling --------------------------------------------------- #
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError,
                    ConnectionError):
                return
            lines = head.decode("latin-1").split("\r\n")
            try:
                method, target, version = lines[0].split(" ", 2)
            except ValueError:
                await self._reply(writer, 400, "Bad Request")
                return
            headers = [line for line in lines[1:] if line]

            if method.upper() == "CONNECT":
                host, port = split_host_port(target, 443)
                await self._connect(reader, writer, host, port)
            else:
                url = urlsplit(target)
                if url.scheme != "http" or not url.hostname:
                    await self._reply(writer, 400, "Bad Request",
                                      "Only absolute http:// URLs and CONNECT are supported.")
                    return
                await self._http(reader, writer, method, url, version, headers)
        except Exception as exc:  # noqa: BLE001 - never kill the server loop
            if self._log:
                self._log.write(f"# error: {exc!r}\n")
        finally:
            writer.close()

    async def _open(self, host: str, port: int, rule: AllowRule) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a TCP connection to an allowed destination (directly)."""
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ConnectionError(f"cannot resolve {host}: {exc}") from exc
        addresses = list(dict.fromkeys(info[4][0] for info in infos))
        if not rule.allow_private:
            addresses = [a for a in addresses if not is_private(a)]
            if not addresses:
                raise Denied(f"{host} resolves to a private/loopback address")
        last: Exception | None = None
        for address in addresses:
            try:
                return await asyncio.wait_for(asyncio.open_connection(address, port), CONNECT_TIMEOUT)
            except (OSError, asyncio.TimeoutError) as exc:
                last = exc
        raise ConnectionError(f"cannot connect to {host}:{port}: {last}")

    def _upstream_headers(self) -> str:
        return f"Proxy-Authorization: {self.upstream.auth}\r\n" if self.upstream and self.upstream.auth else ""

    async def _connect(self, reader, writer, host: str, port: int) -> None:
        rule = self.match(host, port)
        if not rule:
            self._record("DENY", host, port, "CONNECT")
            await self._reply(writer, 403, "Forbidden", f"{host}:{port} is not in the sandbox allowlist.")
            return
        try:
            if self.upstream:
                up_reader, up_writer = await asyncio.wait_for(
                    asyncio.open_connection(self.upstream.host, self.upstream.port), CONNECT_TIMEOUT)
                up_writer.write((f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                                 f"{self._upstream_headers()}\r\n").encode("latin-1"))
                await up_writer.drain()
                response = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT)
                status = response.split(b" ", 2)[1:2]
                if status != [b"200"]:
                    up_writer.close()
                    raise ConnectionError(f"upstream proxy refused: {response.splitlines()[0]!r}")
            else:
                up_reader, up_writer = await self._open(host, port, rule)
        except Denied as exc:
            self._record("DENY", host, port, str(exc))
            await self._reply(writer, 403, "Forbidden", str(exc))
            return
        except (ConnectionError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            self._record("ERROR", host, port, str(exc))
            await self._reply(writer, 502, "Bad Gateway", str(exc))
            return
        self._record("ALLOW", host, port, f"CONNECT [{rule.name}]")
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await self._pipe(reader, writer, up_reader, up_writer)

    async def _http(self, reader, writer, method: str, url, version: str, headers: list[str]) -> None:
        host, port = url.hostname, url.port or 80
        rule = self.match(host, port)
        if not rule:
            self._record("DENY", host, port, method)
            await self._reply(writer, 403, "Forbidden", f"{host}:{port} is not in the sandbox allowlist.")
            return
        kept = [h for h in headers if h.split(":", 1)[0].strip().lower() not in HOP_BY_HOP]
        try:
            if self.upstream:
                up_reader, up_writer = await asyncio.wait_for(
                    asyncio.open_connection(self.upstream.host, self.upstream.port), CONNECT_TIMEOUT)
                request_target = url.geturl()
                extra = self._upstream_headers()
            else:
                up_reader, up_writer = await self._open(host, port, rule)
                request_target = (url.path or "/") + (f"?{url.query}" if url.query else "")
                extra = ""
        except Denied as exc:
            self._record("DENY", host, port, str(exc))
            await self._reply(writer, 403, "Forbidden", str(exc))
            return
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            self._record("ERROR", host, port, str(exc))
            await self._reply(writer, 502, "Bad Gateway", str(exc))
            return
        self._record("ALLOW", host, port, f"{method} [{rule.name}]")
        head = f"{method} {request_target} {version}\r\n" + "".join(f"{h}\r\n" for h in kept)
        head += extra + "Connection: close\r\n\r\n"
        up_writer.write(head.encode("latin-1"))
        await up_writer.drain()
        await self._pipe(reader, writer, up_reader, up_writer)

    @staticmethod
    async def _pipe(c_reader, c_writer, u_reader, u_writer) -> None:
        async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                try:
                    if dst.can_write_eof():
                        dst.write_eof()
                except (OSError, RuntimeError):
                    pass

        try:
            await asyncio.gather(copy(c_reader, u_writer), copy(u_reader, c_writer))
        finally:
            u_writer.close()

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, code: int, reason: str, body: str = "") -> None:
        payload = (body or reason).encode() + b"\n"
        writer.write(
            f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload
        )
        try:
            await writer.drain()
        except ConnectionError:
            pass

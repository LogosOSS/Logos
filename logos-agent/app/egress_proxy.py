"""The only way out of the session network: an allowlisting HTTPS proxy.

Agent sessions run on an internal network whose only other peer is the model
gateway. Some repositories cannot be worked on without downloading things —
a language toolchain, a prebuilt dependency cache. An administrator can name
the HTTPS hosts sessions of a linked repository may reach; this proxy is
where that list is enforced.

* Only ``CONNECT`` to port 443. There is no plain HTTP and no other port.
* The caller authenticates as its session with a random token the runner
  created at launch; only the token's hash is stored. A session that is not
  running gets nothing.
* The host must match the repository's list, read live from the database,
  so withdrawing a host takes effect within seconds.
* Every address the host resolves to must be public. A name that resolves
  into a private, loopback, link-local or otherwise non-global range is
  refused, and the connection goes to the address that was checked, so a
  second lookup cannot steer it elsewhere.

The proxy sees only the encrypted stream; it carries no credentials of its
own into that stream.

Run as ``python -m app.egress_proxy`` from the runner image.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import socket
import time
from collections import defaultdict
from typing import Any

from . import db

logger = logging.getLogger("logos-agent.egress")

LISTEN_HOST = os.getenv("LOGOS_AGENT_EGRESS_LISTEN_HOST", "0.0.0.0")  # NOSONAR - a container-internal listener
LISTEN_PORT = int(os.getenv("LOGOS_AGENT_EGRESS_LISTEN_PORT", "3128"))
HEAD_LIMIT = 8192
HEAD_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 15.0
IDLE_TIMEOUT_S = 300.0
MAX_CONNECTIONS_PER_SESSION = 64
GRANT_TTL_S = 5.0
_USER = re.compile(r"^session-(\d{1,12})$")
_TARGET = re.compile(r"^([A-Za-z0-9.-]{1,253}):(\d{1,5})$")


def host_allowed(host: str, patterns: list[str]) -> bool:
    """Whether ``host`` is on the list. ``*.example.org`` matches subdomains only."""
    host = host.lower().rstrip(".")
    for pattern in patterns:
        pattern = str(pattern).lower().strip()
        if not pattern:
            continue
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]) and host != pattern[2:]:
                return True
        elif host == pattern:
            return True
    return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _credentials(header: str) -> tuple[int, str] | None:
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    user, _, password = decoded.partition(":")
    match = _USER.match(user)
    if not match or not password:
        return None
    return int(match.group(1)), password


async def public_addresses(host: str, port: int) -> list[str]:
    """The addresses to connect to, or nothing when any of them is not public."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses:
        return []
    for address in addresses:
        if not ipaddress.ip_address(address.split("%", 1)[0]).is_global:
            return []
    return addresses


class EgressProxy:
    def __init__(self) -> None:
        self._grants: dict[int, tuple[float, dict[str, Any] | None]] = {}
        self._open: dict[int, int] = defaultdict(int)

    async def _grant(self, session_id: int) -> dict[str, Any] | None:
        cached = self._grants.get(session_id)
        now = time.monotonic()
        if cached and now - cached[0] < GRANT_TTL_S:
            return cached[1]
        grant = await db.egress_grant(session_id)
        self._grants[session_id] = (now, grant)
        return grant

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session_id: int | None = None
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=HEAD_TIMEOUT_S)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
                return
            lines = head.decode("latin-1").split("\r\n")
            method, _, rest = lines[0].partition(" ")
            target = rest.partition(" ")[0]
            headers = {}
            for line in lines[1:]:
                name, sep, value = line.partition(":")
                if sep:
                    headers[name.strip().lower()] = value.strip()
            if method.upper() != "CONNECT":
                await self._refuse(writer, 405, "only HTTPS through CONNECT is allowed")
                return
            match = _TARGET.match(target)
            if not match or int(match.group(2)) != 443:
                await self._refuse(writer, 403, "only port 443 is allowed")
                return
            host = match.group(1).lower()
            credentials = _credentials(headers.get("proxy-authorization", ""))
            if credentials is None:
                await self._refuse(writer, 407, "session credentials required")
                return
            session_id, token = credentials
            grant = await self._grant(session_id)
            if (
                grant is None
                or grant.get("status") != "running"
                or not grant.get("token_hash")
                or not hmac.compare_digest(str(grant["token_hash"]), token_hash(token))
            ):
                await self._refuse(writer, 407, "unknown or inactive session")
                return
            if not host_allowed(host, list(grant.get("hosts") or [])):
                logger.info("session %s refused %s: not on the repository's list", session_id, host)
                await self._refuse(writer, 403, f"{host} is not an allowed host for this repository")
                return
            if self._open[session_id] >= MAX_CONNECTIONS_PER_SESSION:
                await self._refuse(writer, 429, "too many open connections")
                return
            addresses = await public_addresses(host, 443)
            if not addresses:
                logger.info("session %s refused %s: no public address", session_id, host)
                await self._refuse(writer, 403, f"{host} does not resolve to a public address")
                return
            upstream = await self._connect(addresses)
            if upstream is None:
                await self._refuse(writer, 502, f"could not connect to {host}")
                return
            up_reader, up_writer = upstream
            logger.info("session %s connected to %s", session_id, host)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            self._open[session_id] += 1
            try:
                await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer))
            finally:
                self._open[session_id] -= 1
                up_writer.close()
        except Exception:
            logger.exception("egress connection of session %s failed", session_id)
        finally:
            writer.close()

    @staticmethod
    async def _connect(addresses: list[str]) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        for address in addresses:
            try:
                return await asyncio.wait_for(asyncio.open_connection(address, 443), timeout=CONNECT_TIMEOUT_S)
            except (OSError, asyncio.TimeoutError):
                continue
        return None

    @staticmethod
    async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await asyncio.wait_for(source.read(65536), timeout=IDLE_TIMEOUT_S)
                if not chunk:
                    break
                sink.write(chunk)
                await sink.drain()
        except (OSError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            try:
                if sink.can_write_eof():
                    sink.write_eof()
            except OSError:
                pass

    @staticmethod
    async def _refuse(writer: asyncio.StreamWriter, code: int, reason: str) -> None:
        phrases = {403: "Forbidden", 405: "Method Not Allowed", 407: "Proxy Authentication Required"}
        phrases.update({429: "Too Many Requests", 502: "Bad Gateway"})
        body = (reason + "\n").encode("utf-8")
        head = f"HTTP/1.1 {code} {phrases.get(code, 'Error')}\r\nContent-Type: text/plain\r\n"
        if code == 407:
            head += 'Proxy-Authenticate: Basic realm="logos-agent-session"\r\n'
        head += f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        try:
            writer.write(head.encode("latin-1") + body)
            await writer.drain()
        except OSError:
            pass


async def serve() -> None:
    proxy = EgressProxy()
    server = await asyncio.start_server(proxy.handle, LISTEN_HOST, LISTEN_PORT, limit=HEAD_LIMIT)
    logger.info("egress proxy listening on %s:%s", LISTEN_HOST, LISTEN_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    asyncio.run(serve())

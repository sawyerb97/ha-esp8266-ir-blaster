"""HTTP client for the mdhiggins ESP8266-HTTP-IR-Blaster firmware.

Endpoints used (all GET unless noted, all accept ?pass=):
  /              status page (HTML): codes sent/received, GPIO map
  /msg           send one code: code=DATA:TYPE:BITS, out, repeat, rdelay,
                 pulse, pdelay, address, simple=1
  /json  (POST)  send a JSON array of commands (raw, pronto, delay, named)
  /received?id=N details page for received code N (1 = newest), incl. raw
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
import html
import ipaddress
import json
import logging
import re
import time
from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT

_LOGGER = logging.getLogger(__name__)
IP_CACHE_SECONDS = 300


class IRBlasterError(Exception):
    """Base error."""


class IRBlasterConnectionError(IRBlasterError):
    """Device unreachable or returned an unexpected response."""


class IRBlasterAuthError(IRBlasterError):
    """Passcode rejected (HTTP 401)."""


@dataclass(frozen=True)
class IRCode:
    """One row from the status page."""

    time: str
    data: str
    type: str
    bits: int
    address: str

    @property
    def code(self) -> str:
        """Firmware short form DATA:TYPE:BITS."""
        return f"{self.data}:{self.type}:{self.bits}"


@dataclass
class IRStatus:
    """Parsed status page."""

    title: str = ""
    received: list[IRCode] = field(default_factory=list)
    sent: list[IRCode] = field(default_factory=list)
    gpio: dict[str, int] = field(default_factory=dict)


_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_ROW_RE = re.compile(
    r"<tr class='text-uppercase'><td>(?:<a [^>]*>)?(.*?)(?:</a>)?</td>"
    r"<td><code>(.*?)</code></td><td><code>(.*?)</code></td>"
    r"<td><code>(.*?)</code></td><td><code>(.*?)</code></td></tr>"
)
_GPIO_RE = re.compile(r"<span class='badge'>GPIO (\d+)</span>\s*([^<]+?)\s*</li>")
_RAW_RE = re.compile(r"<dt>Raw</dt>\s*<dd><code>(.*?)</code></dd>", re.S)


def _rows(fragment: str) -> list[IRCode]:
    out: list[IRCode] = []
    for t, d, ty, b, a in _ROW_RE.findall(fragment):
        try:
            bits = int(b)
        except ValueError:
            bits = 0
        out.append(IRCode(html.unescape(t), html.unescape(d), html.unescape(ty), bits, html.unescape(a)))
    return out


def parse_status(page: str) -> IRStatus:
    """Parse the firmware's home page."""
    status = IRStatus()
    if m := _TITLE_RE.search(page):
        status.title = html.unescape(m.group(1)).strip()
    sent_i = page.find("Codes Transmitted")
    recv_i = page.find("Codes Received")
    if sent_i != -1:
        status.sent = _rows(page[sent_i : recv_i if recv_i > sent_i else None])
    if recv_i != -1:
        status.received = _rows(page[recv_i:])
    for pin, label in _GPIO_RE.findall(page):
        status.gpio[label.strip()] = int(pin)
    return status


class IRBlasterClient:
    """Async client. All requests are serialized: the ESP8266 server is single-threaded."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        passcode: str | None,
        resolver: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self._session = session
        self._resolver = resolver
        self._ip: str | None = None
        self._ip_ts = 0.0
        self.host = host
        self.port = port
        self._passcode = passcode or ""
        self._lock = asyncio.Lock()
        self.last_send = 0.0  # monotonic time of last IR send

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, str]:
        params: dict[str, str] = {}
        if self._passcode:
            params["pass"] = self._passcode
        for k, v in (extra or {}).items():
            if v is not None:
                params[k] = str(v)
        return params

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None, data: str | None = None) -> tuple[int, str]:
        async with self._lock:
            try:
                return await self._do_request(method, path, params, data)
            except IRBlasterConnectionError:
                if self._ip is None:
                    raise
                self._ip = None  # address may have changed (DHCP); resolve again once
                return await self._do_request(method, path, params, data)

    async def _target(self) -> str:
        """Host to connect to: a cached IP for hostnames, so requests skip the mDNS lookup."""
        if self._resolver is None:
            return self.host
        try:
            ipaddress.ip_address(self.host)
            return self.host
        except ValueError:
            pass
        if self._ip and time.monotonic() - self._ip_ts < IP_CACHE_SECONDS:
            return self._ip
        try:
            ip = await self._resolver(self.host)
        except Exception as err:  # noqa: BLE001 - fall back to the hostname
            _LOGGER.debug("Resolving %s failed: %s", self.host, err)
            ip = None
        self._ip, self._ip_ts = ip, time.monotonic()
        return ip or self.host

    async def _do_request(self, method: str, path: str, params: dict[str, Any] | None, data: str | None) -> tuple[int, str]:
        target = await self._target()
        start = time.monotonic()
        try:
            async with self._session.request(
                method,
                f"http://{target}:{self.port}{path}",
                params=self._params(params),
                data=data,
                headers={"Content-Type": "application/json"} if data is not None else None,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                text = await resp.text(errors="replace")
                status = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise IRBlasterConnectionError(f"{self.host}:{self.port} unreachable: {err}") from err
        _LOGGER.debug("%s %s via %s: HTTP %s in %.0f ms", method, path, target, status, (time.monotonic() - start) * 1000)
        if status == 401:
            raise IRBlasterAuthError(text.strip() or "Unauthorized")
        return status, text

    async def get_status(self) -> IRStatus:
        status, text = await self._request("GET", "/")
        if status != 200 or "ESP8266 IR Controller" not in text:
            raise IRBlasterConnectionError(f"Unexpected response from {self.host} (HTTP {status}); not an ESP8266 IR Controller?")
        return parse_status(text)

    async def get_received_raw(self, index: int = 1) -> list[int] | None:
        """Raw timings (µs) for received code `index` (1 = newest)."""
        status, text = await self._request("GET", "/received", {"id": index})
        if status != 200 or not (m := _RAW_RE.search(text)):
            return None
        raw = html.unescape(m.group(1)).strip()
        try:
            values = [int(x) for x in raw.split(",") if x.strip()]
        except ValueError:
            return None
        return values or None

    async def send_code(self, code: str, out: int = 1, repeat: int = 1, rdelay: int | None = None,
                        pulse: int | None = None, pdelay: int | None = None, address: str | None = None) -> None:
        """Send DATA:TYPE:BITS via /msg."""
        self.last_send = time.monotonic()
        status, text = await self._request(
            "GET", "/msg",
            {"code": code, "out": out, "repeat": repeat, "rdelay": rdelay,
             "pulse": pulse, "pdelay": pdelay, "address": address, "simple": 1},
        )
        if status != 200:
            raise IRBlasterError(f"Send failed (HTTP {status}): {text.strip()[:200]}")

    async def send_json(self, commands: list[dict[str, Any]], out: int = 1) -> None:
        """POST a command array to /json (raw, pronto, delay or named-protocol entries)."""
        self.last_send = time.monotonic()
        status, text = await self._request("POST", "/json", {"out": out, "simple": 1}, data=json.dumps(commands))
        if status != 200:
            raise IRBlasterError(f"JSON send failed (HTTP {status}): {text.strip()[:200]}")

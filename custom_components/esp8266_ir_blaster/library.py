"""Flipper-IRDB browser (https://github.com/Lucaslhm/Flipper-IRDB)."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import DOMAIN

REPO = "Lucaslhm/Flipper-IRDB"
BRANCH = "main"
TREE_URL = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/"
CONVERTED = "_Converted_"
OTHER_CATEGORY = "Other (converted from IRDB / Pronto)"
INDEX_MAX_AGE = 7 * 24 * 3600
_KEY = f"{DOMAIN}_library"


class LibraryError(Exception):
    """Library unavailable."""


def _pretty(s: str) -> str:
    return s.replace("_", " ")


class IRLibrary:
    """Index: category -> brand -> {label: repo_path}."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store = Store(hass, 1, f"{DOMAIN}_library_index")
        self._index: dict[str, dict[str, dict[str, str]]] | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def build_index(paths: list[str]) -> dict[str, dict[str, dict[str, str]]]:
        index: dict[str, dict[str, dict[str, str]]] = {}
        for p in paths:
            if not p.endswith(".ir"):
                continue
            parts = p.split("/")
            if parts[0] == CONVERTED:
                # _Converted_/CSV/<L>/<Brand>/<Type>/<file>  or  _Converted_/Pronto/<L>/<Brand>/<file>
                if len(parts) < 5:
                    continue
                cat, brand, rest = OTHER_CATEGORY, parts[3], parts[4:]
                label = f"{' / '.join(_pretty(x) for x in rest)[:-3]} ({parts[1]})"
            else:
                if len(parts) < 3 or parts[0].startswith((".", "_")):
                    continue
                cat, brand, rest = _pretty(parts[0]), parts[1], parts[2:]
                label = " / ".join(_pretty(x) for x in rest)[:-3]
            index.setdefault(cat, {}).setdefault(_pretty(brand), {})[label] = p
        return index

    async def async_index(self, force: bool = False) -> dict[str, dict[str, dict[str, str]]]:
        async with self._lock:
            if self._index is not None and not force:
                return self._index
            cached = await self._store.async_load()
            if cached and not force and time.time() - cached.get("ts", 0) < INDEX_MAX_AGE:
                self._index = cached["index"]
                return self._index
            session = async_get_clientsession(self.hass)
            try:
                async with session.get(
                    TREE_URL,
                    headers={"Accept": "application/vnd.github+json", "User-Agent": "home-assistant-esp8266-ir-blaster"},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        raise LibraryError(f"GitHub returned HTTP {resp.status}")
                    tree = await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                if cached:
                    self._index = cached["index"]
                    return self._index
                raise LibraryError(str(err)) from err
            index = self.build_index([e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"])
            await self._store.async_save({"ts": time.time(), "index": index})
            self._index = index
            return index

    async def async_fetch(self, path: str) -> str:
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(RAW_URL + quote(path), timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    raise LibraryError(f"HTTP {resp.status} fetching {path}")
                return await resp.text(errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise LibraryError(str(err)) from err


def get_library(hass: HomeAssistant) -> IRLibrary:
    if _KEY not in hass.data:
        hass.data[_KEY] = IRLibrary(hass)
    return hass.data[_KEY]

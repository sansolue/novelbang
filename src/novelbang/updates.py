"""Tells the desktop app when a newer release is out.

At most once a day it asks GitHub for the latest release (one request, nothing about
you or your writing sent with it) and remembers the answer in update.json. It never
downloads or installs anything: the app shows a link to the release page.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import httpx

from . import __version__
from .llm import config_dir

LATEST_URL = "https://api.github.com/repos/sansolue/novelbang/releases/latest"
RELEASES_PAGE = "https://github.com/sansolue/novelbang/releases"
CHECK_EVERY = 24 * 3600
CACHE_FILE = "update.json"
log = logging.getLogger("novelbang")


def parse_version(text: str) -> tuple[int, ...] | None:
    """(0, 2, 0) from "v0.2.0" or "0.2.0"; None for anything else, such as "0.3.0rc1"."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(x) for x in m[1].split(".")) if m else None


def is_newer(latest: str, current: str = __version__) -> bool:
    new, cur = parse_version(latest), parse_version(current)
    if not new or not cur:
        return False
    width = max(len(new), len(cur))
    return new + (0,) * (width - len(new)) > cur + (0,) * (width - len(cur))


async def fetch_latest() -> dict:
    """The latest published release (GitHub leaves out drafts and prereleases)."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"NovelBang/{__version__}"}
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        r = await client.get(LATEST_URL, headers=headers)
        r.raise_for_status()
        data = r.json()
    url = data.get("html_url") or RELEASES_PAGE
    if not url.startswith("https://github.com/"):
        url = RELEASES_PAGE
    return {"version": str(data.get("tag_name", "")).removeprefix("v"), "url": url}


class UpdateChecker:
    def __init__(self, cache_path: Path | None = None):
        self.cache_path = cache_path or config_dir() / CACHE_FILE

    def _cached(self) -> dict:
        try:
            data = json.loads(self.cache_path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    async def status(self) -> dict:
        cached = self._cached()
        if time.time() - float(cached.get("checked", 0)) >= CHECK_EVERY:
            try:
                cached = {**await fetch_latest(), "checked": time.time()}
                log.info("Update check: latest release %s (this is %s)", cached["version"] or "?", __version__)
            except (httpx.HTTPError, ValueError) as e:
                # Offline or rate-limited: try again tomorrow, keeping what we knew.
                cached = {**cached, "checked": time.time()}
                log.info("Update check failed: %s", type(e).__name__)
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(cached), "utf-8")
            except OSError:
                log.warning("Couldn't save %s", self.cache_path)
        latest = cached.get("version") or ""
        return {
            "current": __version__,
            "latest": latest,
            "newer": bool(latest) and is_newer(latest),
            "url": cached.get("url") or RELEASES_PAGE,
        }

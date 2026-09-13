"""Resolve the latest release tag of a GitHub repository.

Used for SDKs that are cloned at a tag rather than installed from a package
manager. The tag is looked up when the Containerfile is generated and then
written into it literally, so the definition tracks upstream when you ask it to
and stays reproducible once written.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"
USER_AGENT = "devenv-forge/0.1 (release tag lookup)"
TIMEOUT = 30.0
#: Releases are not frequent, and an unauthenticated caller gets 60 requests an
#: hour, so a long cache keeps well clear of the limit.
CACHE_TTL_HOURS = 24.0


@dataclass(slots=True)
class Release:
    tag: str
    published: str = ""
    #: True when the tag came from a cache or a fallback rather than the API.
    stale: bool = False
    error: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.tag)


def _cache_path(cache_dir: Path, repo: str) -> Path:
    safe = repo.replace("/", "__")
    return cache_dir / "github" / f"{safe}.json"


def _read_cache(cache_dir: Path, repo: str, *, allow_stale: bool) -> Release | None:
    path = _cache_path(cache_dir, repo)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(payload["fetched_at"])
    except (OSError, ValueError, KeyError):
        return None
    age = (datetime.now(timezone.utc) - fetched).total_seconds()
    if age > CACHE_TTL_HOURS * 3600 and not allow_stale:
        return None
    return Release(
        tag=str(payload.get("tag", "")),
        published=str(payload.get("published", "")),
        stale=age > CACHE_TTL_HOURS * 3600,
    )


def _write_cache(cache_dir: Path, repo: str, release: Release) -> None:
    path = _cache_path(cache_dir, repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "tag": release.tag,
                    "published": release.published,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def latest_release(
    repo: str,
    cache_dir: Path,
    *,
    fallback: str = "",
    refresh: bool = False,
) -> Release:
    """Latest release tag for ``owner/name``.

    Falls back to a cached answer, then to *fallback*, so a rate limit or an
    offline machine degrades to a pinned tag rather than a broken file.
    """
    if not refresh:
        cached = _read_cache(cache_dir, repo, allow_stale=False)
        if cached is not None:
            return cached

    request = urllib.request.Request(
        f"{API}/repos/{urllib.parse.quote(repo)}/releases/latest",
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        stale = _read_cache(cache_dir, repo, allow_stale=True)
        if stale is not None and stale.tag:
            stale.stale = True
            stale.error = str(exc)
            return stale
        return Release(tag=fallback, stale=bool(fallback), error=str(exc))

    tag = str(payload.get("tag_name") or "")
    if not tag:
        return Release(tag=fallback, stale=bool(fallback), error="no tag in response")

    release = Release(tag=tag, published=str(payload.get("published_at") or "")[:10])
    _write_cache(cache_dir, repo, release)
    return release

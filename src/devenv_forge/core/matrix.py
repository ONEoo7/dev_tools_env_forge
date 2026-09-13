"""Builds the distro comparison matrix from the per-distro sources."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import CATALOG, DISTROS, Distro, PackageSpec, names_for_distro
from .sources import Found, LookupError_, build_sources

_SEGMENT_RE = re.compile(r"(\d+|[a-zA-Z]+)")

#: How long a resolved distro answer stays usable. Package indexes move in days,
#: not minutes, so a re-run within this window should cost nothing.
RESOLVED_TTL_HOURS = 12.0


def _resolved_cache_path(cache_dir: Path, distro_key: str) -> Path:
    return cache_dir / "resolved" / f"{distro_key}.json"


def read_resolved_cache(
    cache_dir: Path, distro_key: str, names: Sequence[str]
) -> dict[str, Found] | None:
    """Return cached versions when they are fresh and still match the catalogue."""
    import json
    from datetime import datetime, timezone

    path = _resolved_cache_path(cache_dir, distro_key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(payload["fetched_at"])
    except (OSError, ValueError, KeyError):
        return None
    if (datetime.now(timezone.utc) - fetched).total_seconds() > RESOLVED_TTL_HOURS * 3600:
        return None
    # A catalogue edit changes the questions, so the old answers no longer apply.
    if payload.get("names") != list(names):
        return None
    try:
        return {
            name: Found(
                version=entry["version"],
                package=entry["package"],
                component=entry.get("component", ""),
            )
            for name, entry in payload["found"].items()
        }
    except (KeyError, TypeError):
        return None


def write_resolved_cache(
    cache_dir: Path, distro_key: str, names: Sequence[str], found: dict[str, Found]
) -> None:
    import json
    from datetime import datetime, timezone

    path = _resolved_cache_path(cache_dir, distro_key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "names": list(names),
                    "found": {
                        name: {
                            "version": hit.version,
                            "package": hit.package,
                            "component": hit.component,
                        }
                        for name, hit in found.items()
                    },
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def version_key(version: str) -> tuple:
    """Sort key that compares versions segment by segment."""
    parts: list[tuple[int, object]] = []
    for token in _SEGMENT_RE.findall(version.strip()):
        parts.append((1, int(token)) if token.isdigit() else (0, token.lower()))
    return tuple(parts)


@dataclass(slots=True)
class Cell:
    distro: str
    version: str = ""
    package: str = ""
    component: str = ""
    available: bool = False
    #: True when the distro has no open index and the figure is inferred.
    approximate: bool = False
    meta: bool = False
    error: str = ""

    @property
    def display(self) -> str:
        if self.error:
            return "error"
        if self.meta:
            return "meta-package"
        return self.version if self.available else "not packaged"


@dataclass(slots=True)
class Row:
    spec: PackageSpec
    cells: dict[str, Cell] = field(default_factory=dict)

    def newest_keys(self) -> set[str]:
        """Distro keys holding the highest version in this row."""
        usable = [c for c in self.cells.values() if c.available and c.version and not c.meta]
        if not usable:
            return set()
        best = max(version_key(c.version) for c in usable)
        return {c.distro for c in usable if version_key(c.version) == best}

    @property
    def missing_count(self) -> int:
        return sum(1 for c in self.cells.values() if not c.available and not c.meta)


@dataclass(slots=True)
class MatrixResult:
    rows: list[Row] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Seconds spent per distro. Zero means the answer came from cache.
    timings: dict[str, float] = field(default_factory=dict)
    elapsed: float = 0.0

    def slowest(self) -> tuple[str, float] | None:
        live = {k: v for k, v in self.timings.items() if v > 0}
        if not live:
            return None
        key = max(live, key=lambda k: live[k])
        return key, live[key]

    def coverage(self, distro_key: str) -> tuple[int, int]:
        """How many packages the distro has, out of the comparable total."""
        have = total = 0
        for row in self.rows:
            cell = row.cells.get(distro_key)
            if cell is None or cell.meta:
                continue
            total += 1
            if cell.available:
                have += 1
        return have, total


def build_matrix(
    cache_dir: Path,
    *,
    specs: Sequence[PackageSpec] = CATALOG,
    distros: Sequence[Distro] = DISTROS,
    refresh: bool = False,
    on_progress: Callable[[int, int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> MatrixResult:
    """Query every distro and assemble the comparison rows.

    The distros are queried concurrently. They are independent hosts, so the
    cost is the slowest single index rather than the sum of all six.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    started = time.monotonic()
    result = MatrixResult()
    sources = build_sources()
    resolved: dict[str, dict[str, Found]] = {}
    failures: dict[str, set[str]] = {}

    def query(distro: Distro):
        names = names_for_distro(distro.key)
        if not refresh:
            cached = read_resolved_cache(cache_dir, distro.key, names)
            if cached is not None:
                return distro, cached, set(), 0.0, ""

        source = sources.get(distro.key)
        if source is None:
            return distro, {}, set(), 0.0, "no package index configured"

        begin = time.monotonic()
        try:
            source.prepare(names)
            found = source.lookup(names)
        except LookupError_ as exc:
            return distro, {}, set(), time.monotonic() - begin, str(exc)

        failed = set(getattr(source, "failures", ()) or ())
        # Resolving nothing at all points at a broken endpoint rather than a
        # distro that carries none of these tools.
        if names and not found:
            return distro, {}, failed, time.monotonic() - begin, (
                "index returned nothing; the endpoint may be wrong or unreachable"
            )
        # Only a complete answer is worth caching. Storing a partial one would
        # freeze a transient network failure in place for hours.
        if found and not failed:
            write_resolved_cache(cache_dir, distro.key, names, found)
        return distro, found, failed, time.monotonic() - begin, ""

    completed = 0
    total = len(distros)
    with ThreadPoolExecutor(max_workers=max(1, total)) as pool:
        futures = [pool.submit(query, d) for d in distros]
        for future in as_completed(futures):
            if should_cancel is not None and should_cancel():
                break
            distro, found, failed, seconds, error = future.result()
            resolved[distro.key] = found
            result.timings[distro.key] = seconds
            if error:
                result.errors.append(f"{distro.label}: {error}")
            if failed:
                failures[distro.key] = failed
                result.errors.append(
                    f"{distro.label}: {len(failed)} lookup(s) failed; "
                    "those cells are shown as unknown"
                )
            completed += 1
            if on_progress is not None:
                on_progress(completed, total, distro.label)

    for spec in specs:
        row = Row(spec=spec)
        for distro in distros:
            row.cells[distro.key] = _cell_for(
                spec, distro, resolved.get(distro.key, {}), failures.get(distro.key, set())
            )
        result.rows.append(row)

    result.elapsed = time.monotonic() - started
    return result


def _cell_for(
    spec: PackageSpec, distro: Distro, found: dict, failed: set[str] | None = None
) -> Cell:
    if spec.meta:
        names = spec.names_for(distro.key)
        return Cell(
            distro=distro.key,
            package=names[0] if names else "",
            available=True,
            meta=True,
        )

    approximate = bool(distro.proxy_note)
    # Candidates are in preference order, so the first that resolves wins.
    for name in spec.names_for(distro.key):
        hit = found.get(name)
        if hit is not None:
            return Cell(
                distro=distro.key,
                version=hit.version,
                package=hit.package,
                component=hit.component,
                available=True,
                approximate=approximate,
            )
    # Only call it absent when every candidate was actually checked. If a
    # lookup failed, say so rather than implying the package does not exist.
    if failed and any(name in failed for name in spec.names_for(distro.key)):
        return Cell(
            distro=distro.key,
            available=False,
            approximate=approximate,
            error="lookup failed",
        )
    return Cell(distro=distro.key, available=False, approximate=approximate)

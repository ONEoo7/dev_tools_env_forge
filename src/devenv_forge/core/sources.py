"""Per-distro package version lookups.

Each distro is queried through its own index, because binary package names are
what a user actually installs and only the native indexes know them. An
aggregator that tracks source packages cannot tell ``binutils`` apart from
``binutils-arm-none-eabi``, and on the apt distros those are the same source.

RHEL is the exception. Red Hat publishes no open package index, so it reads the
public CentOS Stream repodata, which RHEL is built from, plus EPEL.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass

USER_AGENT = "devenv-forge/0.1 (distro package comparison)"
TIMEOUT = 60.0

#: Concurrency for indexes that answer one package per request. Kept modest:
#: these are free community services, not something to hammer.
MAX_WORKERS = 6

#: Every remote endpoint, in one place. Only Alpine is a package mirror; the
#: rest are query services. Each can be pointed elsewhere with an environment
#: variable, which is how you would use a mirror closer to you.
DEFAULT_ENDPOINTS = {
    "debian": "https://qa.debian.org/madison.php",
    "ubuntu": "https://people.canonical.com/~ubuntu-archive/madison.cgi",
    "fedora": "https://mdapi.fedoraproject.org",
    "arch": "https://geo.mirror.pkgbuild.com",
    "alpine": "https://dl-cdn.alpinelinux.org/alpine",
    "rhel": "https://mirror.stream.centos.org",
    "epel": "https://download.fedoraproject.org/pub/epel",
}


def endpoint(key: str) -> str:
    """Resolve an endpoint, allowing DEVENV_FORGE_<KEY>_URL to override it."""
    import os

    return os.environ.get(
        f"DEVENV_FORGE_{key.upper()}_URL", DEFAULT_ENDPOINTS[key]
    ).rstrip("/")


def parallel_lookup(
    names: Sequence[str],
    fetch: Callable[[str], "Found | None"],
    failures: set[str] | None = None,
) -> dict[str, "Found"]:
    """Run a per-package fetch across a small thread pool.

    One request per package is unavoidable for indexes with no bulk endpoint,
    but doing them one after another is not: Fedora's 22 packages take about
    eleven seconds serially and under two in parallel.
    """
    from concurrent.futures import ThreadPoolExecutor

    if not names:
        return {}
    found: dict[str, Found] = {}
    workers = min(MAX_WORKERS, len(names))

    def guarded(name: str):
        try:
            return fetch(name)
        except LookupError_:
            # A transport failure is not evidence the package is absent.
            if failures is not None:
                failures.add(name)
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, hit in zip(names, pool.map(guarded, names)):
            if hit is not None:
                found[name] = hit
    return found


@dataclass(slots=True)
class Found:
    version: str
    package: str
    #: e.g. Ubuntu's "universe" or Fedora's "updates". Shown as a caveat.
    component: str = ""


class LookupError_(RuntimeError):
    """Raised when an index cannot be reached at all."""


def http_get(
    url: str, *, binary: bool = False, missing_codes: tuple[int, ...] = (404,)
):
    """Fetch a URL. Codes in *missing_codes* mean "absent" and return empty.

    Anything else raises, so a transport failure is never mistaken for a
    package that does not exist.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in missing_codes:
            return b"" if binary else ""
        raise LookupError_(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise LookupError_(str(exc)) from exc
    return payload if binary else payload.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# version normalisation
# ---------------------------------------------------------------------------

_EPOCH_RE = re.compile(r"^\d+:")
#: Vendor suffix with no dash in front, e.g. Ubuntu's "1.93.1ubuntu1" or an rpm
#: "3.1.el9". The lookbehind on digits matters: without it "el" would match
#: inside Arm's "14.2.rel1" and truncate a real version.
_VENDOR_SUFFIX_RE = re.compile(
    r"(?<=\d)(?:ubuntu|build)\d+.*$|\.(?:fc|el)\d+.*$", re.IGNORECASE
)


def upstream_version(raw: str) -> str:
    """Reduce a distro version string to the comparable upstream version.

    Packaging conventions differ wildly and the noise is not comparable across
    distros, so it is stripped:

    ``1:2.47.3-0+deb13u1`` and ``2.54.0-r0`` and ``10.2.1+ds-0ubuntu1`` and
    ``16.2.1+r23+gd564253eb6c8`` all reduce to their upstream version.
    """
    text = _EPOCH_RE.sub("", raw.strip())
    # The packaging revision follows the first dash on apt and apk alike.
    text = text.split("-", 1)[0]
    # Repacking and VCS markers follow a plus.
    text = text.split("+", 1)[0]
    text = _VENDOR_SUFFIX_RE.sub("", text)
    return text.strip().rstrip(".") or raw.strip()


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


class VersionSource(ABC):
    """Resolves binary package names to versions for one distro."""

    #: True when one call can answer every package at once.
    bulk = False

    #: Names whose lookup failed for transport reasons. Reported as unknown
    #: rather than as "not packaged", which would be a false negative. Arch's
    #: search API answering HTTP 429 under concurrency is exactly how a silent
    #: version of this turned five real packages into apparent absences.
    failures: set[str] = frozenset()  # type: ignore[assignment]

    @abstractmethod
    def lookup(self, names: Sequence[str]) -> dict[str, Found]:
        """Return a mapping of the names that exist to their versions."""

    def prepare(self, all_names: Sequence[str]) -> None:
        """Optional hook to load a whole index before individual lookups."""


class MadisonSource(VersionSource):
    """Debian and Ubuntu, via their madison services.

    Both return the same pipe-delimited table and both are binary aware, which
    is what makes the Arm cross packages resolvable.
    """

    bulk = True

    def __init__(self, url: str, params: dict[str, str], package_param: str = "package") -> None:
        self.url = url
        self.params = params
        self.package_param = package_param
        self._cache: dict[str, Found] = {}

    def prepare(self, all_names: Sequence[str]) -> None:
        if self._cache or not all_names:
            return
        query = dict(self.params)
        query[self.package_param] = " ".join(sorted(set(all_names)))
        text = http_get(f"{self.url}?{urllib.parse.urlencode(query)}")
        self._cache = self._parse(text)

    @staticmethod
    def _parse(text: str) -> dict[str, Found]:
        best: dict[str, Found] = {}
        for line in text.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3 or not parts[0]:
                continue
            name, raw_version, suite = parts[0], parts[1], parts[2]
            # The source row repeats the package with a packaging-only version;
            # binary rows are the ones that matter.
            if len(parts) >= 4 and parts[3].strip() == "source":
                continue
            version = upstream_version(raw_version)
            if not version or not version[0].isdigit():
                continue
            component = suite.split("/")[1] if "/" in suite else ""
            current = best.get(name)
            if current is None or _version_key(version) > _version_key(current.version):
                best[name] = Found(version=version, package=name, component=component)
        return best

    def lookup(self, names: Sequence[str]) -> dict[str, Found]:
        return {n: self._cache[n] for n in names if n in self._cache}


class AlpineSource(VersionSource):
    """Alpine, by downloading and parsing APKINDEX.

    One small download covers every package in the release, which is cheaper
    than any per-package query.
    """

    bulk = True

    def __init__(self, branch: str, repos: Sequence[str] = ("main", "community"),
                 arch: str = "x86_64") -> None:
        self.branch = branch
        self.repos = repos
        self.arch = arch
        self.mirror = endpoint("alpine")
        self._index: dict[str, Found] = {}

    def prepare(self, all_names: Sequence[str]) -> None:
        if self._index:
            return
        from concurrent.futures import ThreadPoolExecutor

        def fetch(repo: str) -> tuple[str, bytes]:
            url = f"{self.mirror}/{self.branch}/{repo}/{self.arch}/APKINDEX.tar.gz"
            return repo, http_get(url, binary=True)

        with ThreadPoolExecutor(max_workers=len(self.repos)) as pool:
            downloads = list(pool.map(fetch, self.repos))

        # Ingest in declared order so main still wins over community.
        for repo, raw in sorted(downloads, key=lambda d: self.repos.index(d[0])):
            if not raw:
                continue
            try:
                with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                    member = archive.extractfile("APKINDEX")
                    if member is None:
                        continue
                    text = member.read().decode("utf-8", "replace")
            except (tarfile.TarError, KeyError, OSError) as exc:
                raise LookupError_(f"could not read APKINDEX: {exc}") from exc
            self._ingest(text, repo)

    def _ingest(self, text: str, component: str) -> None:
        for block in text.split("\n\n"):
            name = version = None
            for line in block.splitlines():
                if line.startswith("P:"):
                    name = line[2:]
                elif line.startswith("V:"):
                    version = line[2:]
            # main wins over community when a name appears in both.
            if name and version and name not in self._index:
                self._index[name] = Found(
                    version=upstream_version(version), package=name, component=component
                )

    def lookup(self, names: Sequence[str]) -> dict[str, Found]:
        return {n: self._index[n] for n in names if n in self._index}


class ArchSource(VersionSource):
    """Arch Linux, by downloading the repository databases.

    The website's search API rate-limits concurrent callers and answers HTTP 429,
    which silently looked like "package absent". The ``.db`` files the package
    manager itself uses have no such limit, cover every package in one pass and
    are quicker than twenty individual queries.
    """

    bulk = True

    def __init__(self, repos: Sequence[str] = ("core", "extra"), arch: str = "x86_64") -> None:
        self.repos = repos
        self.arch = arch
        self.mirror = endpoint("arch")
        self._index: dict[str, Found] = {}

    def prepare(self, all_names: Sequence[str]) -> None:
        if self._index:
            return
        from concurrent.futures import ThreadPoolExecutor

        def fetch(repo: str) -> tuple[str, bytes]:
            url = f"{self.mirror}/{repo}/os/{self.arch}/{repo}.db.tar.gz"
            return repo, http_get(url, binary=True)

        with ThreadPoolExecutor(max_workers=len(self.repos)) as pool:
            downloads = list(pool.map(fetch, self.repos))

        for repo, raw in sorted(downloads, key=lambda d: self.repos.index(d[0])):
            if not raw:
                continue
            try:
                with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                    self._ingest(archive, repo)
            except (tarfile.TarError, OSError) as exc:
                raise LookupError_(f"could not read {repo}.db: {exc}") from exc

    def _ingest(self, archive: tarfile.TarFile, component: str) -> None:
        for member in archive.getmembers():
            if not member.name.endswith("/desc"):
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            lines = handle.read().decode("utf-8", "replace").splitlines()
            name = version = None
            for index, line in enumerate(lines):
                # Each field is a %KEY% marker with its value on the next line.
                if line == "%NAME%" and index + 1 < len(lines):
                    name = lines[index + 1]
                elif line == "%VERSION%" and index + 1 < len(lines):
                    version = lines[index + 1]
            if name and version and name not in self._index:
                self._index[name] = Found(
                    version=upstream_version(version), package=name, component=component
                )

    def lookup(self, names: Sequence[str]) -> dict[str, Found]:
        return {n: self._index[n] for n in names if n in self._index}


class FedoraSource(VersionSource):
    """Fedora, via mdapi, which indexes binary package names.

    mdapi has no bulk endpoint, so this is one request per package. They run
    concurrently, which is the difference between roughly eleven seconds and
    under two.
    """

    def __init__(self, branch: str) -> None:
        self.branch = branch
        self.api = endpoint("fedora")
        self.failures: set[str] = set()

    def _one(self, name: str) -> Found | None:
        # mdapi answers 400, not 404, for a package it does not carry, so both
        # mean absent here. Anything else is a transport failure and is raised
        # so the caller records it rather than showing a false absence.
        body = http_get(
            f"{self.api}/{self.branch}/pkg/{urllib.parse.quote(name)}",
            missing_codes=(400, 404),
        )
        if not body:
            return None
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        version = str(payload.get("version") or "")
        if not version:
            return None
        return Found(
            version=version, package=name, component=str(payload.get("repo") or "")
        )

    def lookup(self, names: Sequence[str]) -> dict[str, Found]:
        self.failures = set()
        return parallel_lookup(names, self._one, self.failures)


class RepodataSource(VersionSource):
    """RHEL-family distros, by reading yum/dnf repository metadata.

    Red Hat publishes no query API, but CentOS Stream is what RHEL is built
    from and its repodata is public. Reading it directly is both faster than an
    aggregator and materially more accurate: the aggregate matched by source
    package and reported Python 3.14 where the default python3 is really 3.12.
    """

    bulk = True
    NS = "{http://linux.duke.edu/metadata/common}"

    def __init__(self, repos: dict[str, str]) -> None:
        self.repos = repos
        self._index: dict[str, Found] = {}

    @staticmethod
    def _decompress(raw: bytes) -> bytes:
        if raw[:2] == b"\x1f\x8b":
            import gzip

            return gzip.decompress(raw)
        if raw[:4] == b"\x28\xb5\x2f\xfd":
            # EPEL ships zstd, which Python 3.14 has in the standard library.
            from compression import zstd

            return zstd.decompress(raw)
        return raw

    def prepare(self, all_names: Sequence[str]) -> None:
        if self._index:
            return
        from concurrent.futures import ThreadPoolExecutor

        def fetch(item: tuple[str, str]) -> tuple[str, bytes]:
            component, root = item
            try:
                meta = http_get(f"{root}/repodata/repomd.xml")
                match = re.search(
                    r'<data type="primary">.*?<location href="([^"]+)"', meta, re.S
                )
                if not match:
                    return component, b""
                return component, http_get(f"{root}/{match.group(1)}", binary=True)
            except LookupError_:
                # One unreachable repository should not lose the others.
                return component, b""

        with ThreadPoolExecutor(max_workers=len(self.repos)) as pool:
            downloads = list(pool.map(fetch, self.repos.items()))

        order = list(self.repos)
        for component, raw in sorted(downloads, key=lambda d: order.index(d[0])):
            if raw:
                self._ingest(self._decompress(raw), component)

    def _ingest(self, xml: bytes, component: str) -> None:
        import io
        from xml.etree import ElementTree

        package_tag = f"{self.NS}package"
        # iterparse with clear() keeps a 25 MB document off the heap.
        for _event, element in ElementTree.iterparse(io.BytesIO(xml), events=("end",)):
            if element.tag != package_tag:
                continue
            name = element.findtext(f"{self.NS}name")
            version = element.find(f"{self.NS}version")
            if name and version is not None and name not in self._index:
                self._index[name] = Found(
                    version=upstream_version(str(version.get("ver") or "")),
                    package=name,
                    component=component,
                )
            element.clear()

    def lookup(self, names: Sequence[str]) -> dict[str, Found]:
        return {n: self._index[n] for n in names if n in self._index}


_SEGMENT_RE = re.compile(r"(\d+|[a-zA-Z]+)")


def _version_key(version: str) -> tuple:
    parts: list[tuple[int, object]] = []
    for token in _SEGMENT_RE.findall(version.strip()):
        parts.append((1, int(token)) if token.isdigit() else (0, token.lower()))
    return tuple(parts)


def build_sources() -> dict[str, VersionSource]:
    """Construct one source per distro key in the catalogue."""
    centos = endpoint("rhel")
    return {
        "debian": MadisonSource(
            endpoint("debian"),
            {"table": "debian", "s": "trixie", "text": "on"},
        ),
        "ubuntu": MadisonSource(
            endpoint("ubuntu"),
            {"s": "resolute", "a": "amd64", "text": "on"},
        ),
        "alpine": AlpineSource("v3.24"),
        "arch": ArchSource(),
        "fedora": FedoraSource("f44"),
        "rhel": RepodataSource(
            {
                "BaseOS": f"{centos}/10-stream/BaseOS/x86_64/os",
                "AppStream": f"{centos}/10-stream/AppStream/x86_64/os",
                "CRB": f"{centos}/10-stream/CRB/x86_64/os",
                # EPEL through the geo redirector; the master server is slow.
                "EPEL": f"{endpoint('epel')}/10/Everything/x86_64",
            }
        ),
    }

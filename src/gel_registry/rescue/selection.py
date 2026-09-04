"""Read-only, deterministic release-plan selection from a rescue capture."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final, Literal, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from ..constants import CAPTURE_ID
from ..contracts import PackageEntry, PackageIndex, parse_semver, semver_key
from ..digest import hash_bytes
from ..github.models import validate_repository
from ..github.transport import API_HEADERS, GITHUB_API, REQUEST_TIMEOUT
from .indexes import RescueCaptureManifest
from .models import RescueAsset, RescuePlan, RescueRelease

type Product = Literal["cli", "server", "ls", "postgis"]

_SHA1 = re.compile(r"[0-9a-f]{40}")


def artifact_name(product: str, platform: str) -> str:
    """Return the canonical release asset filename for one artifact."""

    suffix = ".exe" if platform.endswith("-windows-msvc") else ""
    return f"{product}-{platform}{suffix}"


class UpstreamTag:
    """One upstream tag, whose commit SHA is resolved only when it is read.

    Peeling an annotated tag costs one extra GitHub request, and a
    repository like ``geldata/gel`` carries thousands of tags while a plan
    selects a handful. Selection filters on ``name`` alone, so resolving
    every SHA up front spent the whole hourly API budget before the first
    release was chosen. The SHA is therefore fetched on first access and
    cached; it is never mutated by planning.
    """

    __slots__ = ("_commit", "_resolve", "name")

    def __init__(
        self,
        name: str,
        commit: str | None = None,
        *,
        resolve: Callable[[], str] | None = None,
    ) -> None:
        if (commit is None) == (resolve is None):
            raise ValueError("upstream tag needs exactly one of commit or resolve")
        self.name = name
        self._commit = commit
        self._resolve = resolve

    @property
    def commit(self) -> str:
        """Return the peeled commit SHA, fetching it once if still unresolved."""

        if self._commit is None:
            assert self._resolve is not None
            self._commit = self._resolve()
        return self._commit

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, UpstreamTag):
            return NotImplemented
        return self.name == other.name and self.commit == other.commit

    def __hash__(self) -> int:
        return hash((self.name, self.commit))

    def __repr__(self) -> str:
        state = "unresolved" if self._commit is None else repr(self._commit)
        return f"UpstreamTag({self.name!r}, {state})"


def _target_object(target: object) -> tuple[str, str]:
    """Return the ``(type, sha)`` of a tag target, rejecting anything else."""

    if not isinstance(target, dict):
        raise ValueError("GitHub tag response contains an invalid tag")
    kind = target.get("type")
    sha = target.get("sha")
    if not isinstance(kind, str) or not isinstance(sha, str):
        raise ValueError("GitHub tag response contains an invalid tag")
    if kind not in {"commit", "tag"}:
        raise ValueError("GitHub tag response contains an unsupported object")
    return kind, sha


class TagSource(Protocol):
    """The narrow, injectable read-only boundary used for GitHub tag discovery."""

    def tags(self, repository: str) -> tuple[UpstreamTag, ...]: ...


class GitTagSource:
    """Read tag discovery from ``git ls-remote``, not GitHub's REST API.

    One request returns every tag with its peeled commit, over the git
    protocol, so planning needs no token and is not charged against the
    REST hourly budget. The REST path costs one paginated request per 100
    refs plus one peel per annotated tag, and exhausted that budget in a
    single bulk run.
    """

    #: ``ls-remote`` is a network call to a fixed, argument-free remote.
    _TIMEOUT = 60.0

    def __init__(self, git: str = "git") -> None:
        self._git = git

    def tags(self, repository: str) -> tuple[UpstreamTag, ...]:
        url = f"https://github.com/{validate_repository(repository)}"
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, never a shell
                [self._git, "ls-remote", "--tags", "--", url],
                capture_output=True,
                text=True,
                timeout=self._TIMEOUT,
                check=False,
                # Never block on a credential prompt for a missing remote.
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"git ls-remote failed for {repository}: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()
            raise ValueError(
                f"git ls-remote failed for {repository}: "
                f"{detail[-1] if detail else completed.returncode}"
            )
        return _parse_ls_remote(completed.stdout)


def _parse_ls_remote(output: str) -> tuple[UpstreamTag, ...]:
    """Turn ``ls-remote --tags`` output into tags with peeled commits.

    Annotated tags appear twice: ``refs/tags/x`` carries the tag object
    and ``refs/tags/x^{}`` the commit it points at. The peeled line wins,
    because matching a package build against a tag object's SHA never
    succeeds.
    """

    commits: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        sha, _, reference = line.partition("\t")
        if not _SHA1.fullmatch(sha) or not reference.startswith("refs/tags/"):
            raise ValueError(f"unreadable git ls-remote line: {line!r}")
        name = reference.removeprefix("refs/tags/")
        if name.endswith("^{}"):
            peeled[name.removesuffix("^{}")] = sha
        else:
            commits[name] = sha
    unknown = sorted(set(peeled) - set(commits))
    if unknown:
        raise ValueError(f"git ls-remote peeled an unlisted tag: {unknown[0]}")
    return tuple(
        sorted(
            (UpstreamTag(name, peeled.get(name, sha)) for name, sha in commits.items()),
            key=lambda tag: tag.name,
        )
    )


class GitHubTagSource:
    """Read only GitHub tag discovery, kept outside the selector's core logic."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def tags(self, repository: str) -> tuple[UpstreamTag, ...]:
        tags: list[UpstreamTag] = []
        for page in range(1, 10_001):
            response = self._client.get(
                f"{GITHUB_API}/repos/{repository}/git/refs/tags",
                params={"per_page": "100", "page": str(page)},
                headers=API_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            values = response.json()
            if not isinstance(values, list):
                raise ValueError("GitHub tag response must be a list")
            for value in values:
                if not isinstance(value, dict):
                    raise ValueError("GitHub tag response contains an invalid tag")
                reference = value.get("ref")
                target = value.get("object")
                if (
                    not isinstance(reference, str)
                    or not reference.startswith("refs/tags/")
                    or not isinstance(target, dict)
                ):
                    raise ValueError("GitHub tag response contains an invalid tag")
                tags.append(
                    self._tag(repository, reference.removeprefix("refs/tags/"), target)
                )
            if len(values) < 100:
                break
        else:  # pragma: no cover - protective limit for a malformed API response
            raise ValueError("GitHub tag pagination exceeded the safe limit")
        return tuple(sorted(tags, key=lambda tag: tag.name))

    def _tag(self, repository: str, name: str, target: object) -> UpstreamTag:
        """Validate a tag's target now; defer only the network peel.

        A malformed response still fails immediately -- deferring the
        request must not defer detecting bad data.
        """

        kind, sha = _target_object(target)
        if kind == "commit":
            return UpstreamTag(name, sha)
        return UpstreamTag(name, resolve=partial(self._peel_ref, repository, sha))

    def _peel_ref(self, repository: str, sha: str) -> str:
        return self._peel_commit(repository, {"type": "tag", "sha": sha})

    def _peel_commit(self, repository: str, target: object) -> str:
        kind, sha = _target_object(target)
        if kind == "commit":
            return sha
        response = self._client.get(
            f"{GITHUB_API}/repos/{repository}/git/tags/{sha}",
            headers=API_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("GitHub tag object response must be an object")
        return self._peel_commit(repository, value.get("object"))


@dataclass(frozen=True, slots=True)
class _IndexedPackage:
    entry: PackageEntry
    channel: str
    platform: str
    source_url: str


def _capture_packages(repo: Path, capture: str) -> tuple[_IndexedPackage, ...]:
    if capture != CAPTURE_ID:
        raise ValueError(f"rescue capture must be {CAPTURE_ID!r}")
    root = repo / "upstream" / "packages.edgedb.com" / capture
    manifest = RescueCaptureManifest.model_validate_json(
        (root / "capture.json").read_bytes()
    )
    packages: list[_IndexedPackage] = []
    for evidence in manifest.indexes:
        path = root / "indexes" / f"{evidence.channel}-{evidence.platform}.json"
        body = path.read_bytes()
        digest = hash_bytes(body)
        if digest.size != evidence.byte_size or digest.sha256 != evidence.sha256:
            raise ValueError(f"captured index does not match evidence: {path.name}")
        index = PackageIndex.model_validate_json(body)
        packages.extend(
            _IndexedPackage(
                item, evidence.channel, evidence.platform, evidence.source_url
            )
            for item in index.packages
        )
    return tuple(packages)


def _source_url(source_index: str, reference: str) -> str:
    resolved = urljoin(source_index, reference)
    parsed = urlsplit(resolved)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "packages.edgedb.com"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "legacy install reference must resolve under packages.edgedb.com"
        )
    return resolved


def _asset(indexed: _IndexedPackage, *, name: str) -> RescueAsset:
    refs = [ref for ref in indexed.entry.installrefs if ref.encoding == "identity"]
    if len(refs) != 1:
        raise ValueError(
            f"{indexed.entry.version}: expected exactly one identity install reference"
        )
    reference = refs[0]
    if reference.verification.sha256 is None:
        raise ValueError(f"{name}: missing required sha256 digest")
    return RescueAsset(
        source_url=_source_url(indexed.source_url, reference.ref),
        destination_name=name,
        content_type=reference.type,
        expected_size=reference.verification.size,
        expected_sha256=reference.verification.sha256,
    )


def _reference_asset(
    indexed: _IndexedPackage, reference: object, *, name: str
) -> RescueAsset:
    """Make one direct-copy asset from a validated legacy install reference."""

    from ..contracts import InstallRef

    if not isinstance(
        reference, InstallRef
    ):  # pragma: no cover - PackageEntry binds this
        raise TypeError("expected an install reference")
    if reference.verification.sha256 is None:
        raise ValueError(f"{name}: missing required sha256 digest")
    return RescueAsset(
        source_url=_source_url(indexed.source_url, reference.ref),
        destination_name=name,
        content_type=reference.type,
        expected_size=reference.verification.size,
        expected_sha256=reference.verification.sha256,
    )


#: The legacy origin carries each product under both its EdgeDB-era and its
#: Gel-era basename, and matching only the latter hid most of the archive.
CLI_BASENAMES: Final = frozenset({"edgedb-cli", "gel-cli"})
SERVER_BASENAMES: Final = frozenset({"edgedb-server", "gel-server"})
LS_BASENAMES: Final = frozenset({"edgedb-ls", "gel-ls"})


def _metadata(record: _IndexedPackage, key: str) -> str | None:
    metadata = record.entry.version_details.get("metadata")
    value = metadata.get(key) if isinstance(metadata, dict) else None
    return value if isinstance(value, str) else None


def _matches_tag_commit(record: _IndexedPackage, tag: UpstreamTag) -> bool:
    """Tie a package to a tag through ``scm_revision``, not ``build_hash``.

    ``build_hash`` identifies the build, and differs per platform for the
    same release, so it never matched a tag commit. ``scm_revision`` is
    the abbreviated source commit the package was built from, and it is
    identical across a release's platforms.
    """

    revision = _metadata(record, "scm_revision")
    return revision is not None and tag.commit.startswith(revision)


def _newest_rebuild(records: list[_IndexedPackage]) -> _IndexedPackage:
    """Pick the latest rebuild of one source commit for one platform.

    The archive holds several builds of the same version and commit --
    e.g. gel-cli 7.9.0+070b371 and 7.9.0+c84d665, both scm_revision
    feb7f8ccf, rebuilt weeks apart. They are not ambiguous, just dated,
    so the newest ``build_revision`` wins and the choice stays
    deterministic.
    """

    return max(records, key=lambda record: _metadata(record, "build_revision") or "")


def _one_record_per_platform(
    candidates: Iterable[_IndexedPackage], label: str
) -> tuple[tuple[str, _IndexedPackage], ...]:
    """Return one record per platform the archive actually has for a build.

    Required coverage is read from the capture, never from a fixed
    platform list: ``packages.edgedb.com`` never published a Windows
    server or language server, and never published a CLI for
    ``aarch64-unknown-linux-gnu``, so demanding a fixed matrix selected
    nothing at all. A platform the archive lacks is therefore not an
    error -- but two records for one platform still is, because that
    makes the choice of bytes ambiguous.
    """

    by_platform: dict[str, list[_IndexedPackage]] = {}
    for record in candidates:
        by_platform.setdefault(record.platform, []).append(record)
    if not by_platform:
        raise ValueError(f"{label}: no indexed platform records")
    ambiguous = sorted(
        platform
        for platform, items in by_platform.items()
        if len({_metadata(item, "build_revision") for item in items}) != len(items)
    )
    if ambiguous:
        raise ValueError(f"{label}: ambiguous platform record for {ambiguous[0]}")
    return tuple(
        (platform, _newest_rebuild(by_platform[platform]))
        for platform in sorted(by_platform)
    )


#: Trailing archive extensions, longest first so ``.tar.gz`` wins over ``.gz``.
_EXTENSIONS: Final = (
    ".tar.gz",
    ".tar.zst",
    ".tar.xz",
    ".tar.bz2",
    ".zip",
    ".exe",
    ".gz",
    ".zst",
)


def _extension(reference: str) -> str:
    """Return a file's real extension, not everything after its first dot.

    Legacy filenames embed a dotted version -- ``edgedb-server-5.6+adb9e77
    .tar.gz`` -- so splitting at the first dot produced the extension
    ``.6+adb9e77.tar.gz`` and named the asset
    ``gel-server-aarch64-apple-darwin.6+adb9e77.tar.gz``.
    """

    path = urlsplit(reference).path
    name = path.rsplit("/", 1)[-1]
    for extension in _EXTENSIONS:
        if name.endswith(extension):
            return extension
    return ""


def _cli_release(
    version: str, records: Iterable[_IndexedPackage], tag: UpstreamTag
) -> RescueRelease:
    candidates = [
        record
        for record in records
        if record.channel == "stable"
        and record.entry.basename in CLI_BASENAMES
        # Indexed CLI versions carry build metadata (7.9.0+070b371); the
        # tag does not.
        and record.entry.version.split("+", 1)[0] == version
        and _matches_tag_commit(record, tag)
    ]
    selected = _one_record_per_platform(candidates, f"gel-cli {version}")
    assets = [
        _asset(record, name=artifact_name("gel-cli", platform))
        for platform, record in selected
    ]
    return RescueRelease(
        repository="gelstable/gel-cli",
        tag=f"v{version}",
        target_commitish=tag.commit,
        assets=tuple(sorted(assets, key=lambda asset: asset.destination_name)),
    )


def _final_cli_tags(tags: Iterable[UpstreamTag]) -> tuple[tuple[str, UpstreamTag], ...]:
    final: list[tuple[str, UpstreamTag]] = []
    for tag in tags:
        if not tag.name.startswith("v"):
            continue
        version = tag.name[1:]
        parsed = parse_semver(version)
        if parsed is not None and not parsed.prerelease:
            final.append((version, tag))
    return tuple(sorted(final, key=lambda item: semver_key(item[0]), reverse=True))


def _server_release(
    tag: UpstreamTag, records: Iterable[_IndexedPackage]
) -> RescueRelease:
    matched = re.fullmatch(r"v([567])\.([0-9]+)", tag.name)
    if matched is None:
        raise ValueError(f"not a supported final server tag: {tag.name}")
    version = f"{matched.group(1)}.{matched.group(2)}"
    candidates = [
        record
        for record in records
        if record.channel == "stable"
        and record.entry.basename in SERVER_BASENAMES
        and record.entry.version.split("+", 1)[0] == version
        and _matches_tag_commit(record, tag)
    ]
    selected = _one_record_per_platform(candidates, tag.name)
    assets: list[RescueAsset] = []
    for platform, record in selected:
        for reference in record.entry.installrefs:
            assets.append(
                _reference_asset(
                    record,
                    reference,
                    name=f"gel-server-{platform}{_extension(reference.ref)}",
                )
            )
    return RescueRelease(
        repository="gelstable/gel",
        tag=tag.name,
        target_commitish=tag.commit,
        assets=tuple(sorted(assets, key=lambda asset: asset.destination_name)),
    )


def _server_tags(tags: Iterable[UpstreamTag]) -> tuple[UpstreamTag, ...]:
    selected = [tag for tag in tags if re.fullmatch(r"v[567]\.[0-9]+", tag.name)]
    return tuple(
        sorted(
            selected,
            key=lambda tag: tuple(int(value) for value in tag.name[1:].split(".")),
            reverse=True,
        )
    )


_LS_VERSION = re.compile(
    r"^(?P<major>[0-9]+)\.(?P<minor>[0-9]+)-dev\.(?P<dev>[0-9]+)\+(?P<build>[0-9A-Za-z.-]+)$"
)


def _ls_version_key(version: str) -> tuple[int, int, int, str]:
    matched = _LS_VERSION.fullmatch(version)
    if matched is None:
        raise ValueError(f"invalid language-server version: {version!r}")
    return (
        int(matched["major"]),
        int(matched["minor"]),
        int(matched["dev"]),
        matched["build"],
    )


def _ls_release(version: str, records: Iterable[_IndexedPackage]) -> RescueRelease:
    all_records = tuple(records)
    candidates = [
        record
        for record in all_records
        if record.channel == "nightly"
        and record.entry.basename in LS_BASENAMES
        and record.entry.version == version
    ]
    if not candidates:
        raise ValueError(f"no language-server build for {version}")
    assets: list[RescueAsset] = []
    for _platform, record in _one_record_per_platform(candidates, version):
        for reference in record.entry.installrefs:
            assets.append(
                _reference_asset(
                    record,
                    reference,
                    name=(
                        f"gel-ls-{record.entry.version}-{record.platform}"
                        f"{_extension(reference.ref)}"
                    ),
                )
            )
    return RescueRelease(
        repository="gelstable/gel",
        tag=f"gel-ls-v{version}",
        target_commitish=None,
        assets=tuple(sorted(assets, key=lambda asset: asset.destination_name)),
    )


def _ls_versions(records: Iterable[_IndexedPackage]) -> tuple[str, ...]:
    versions = {
        record.entry.version
        for record in records
        if record.channel == "nightly"
        and record.entry.basename in LS_BASENAMES
        and "-dev." in record.entry.version
    }
    return tuple(sorted(versions, key=_ls_version_key, reverse=True))


def _postgis_release(slot: str, records: Iterable[_IndexedPackage]) -> RescueRelease:
    if re.search(r"(?:dev|alpha|beta|rc)", slot, re.IGNORECASE):
        raise ValueError(f"postgis slot is not final stable: {slot}")
    candidates = [
        record
        for record in records
        if record.channel == "stable"
        and record.entry.tags.get("extension") == "postgis"
        and record.entry.tags.get("server_slot") == slot
        and not re.search(
            r"(?:dev|alpha|beta|rc)",
            f"{record.entry.basename} {record.entry.version} {record.entry.slot}",
            re.IGNORECASE,
        )
    ]
    if not candidates:
        raise ValueError(f"no stable PostGIS artifacts for server slot {slot}")
    assets: list[RescueAsset] = []
    for record in candidates:
        for reference in record.entry.installrefs:
            assets.append(
                _reference_asset(
                    record,
                    reference,
                    # Package version and platform, once each: the upstream
                    # filename already repeats the basename and version.
                    name=(
                        f"{record.entry.basename}-{record.entry.version}-"
                        f"{record.platform}{_extension(reference.ref)}"
                    ),
                )
            )
    return RescueRelease(
        repository="gelstable/gel-postgis",
        tag=f"legacy-gel-server-{slot}-ext-postgis",
        target_commitish=None,
        assets=tuple(sorted(assets, key=lambda asset: asset.destination_name)),
    )


def plan_rescue(
    repo: Path,
    *,
    capture: str,
    product: Product | None,
    version: str | None,
    slot: str | None,
    tags: tuple[UpstreamTag, ...] = (),
    tag_source: TagSource | None = None,
) -> RescuePlan:
    """Build a canonical plan without downloading package bodies or mutating GitHub."""

    if product is None and (version is not None or slot is not None):
        raise ValueError("bulk selection cannot include a version or slot")
    if product == "postgis" and (slot is None or version is not None):
        raise ValueError("PostGIS selection requires exactly one slot")
    if product in {"cli", "server", "ls"} and (version is None or slot is not None):
        raise ValueError(f"{product} selection requires exactly one version")
    records = _capture_packages(repo, capture)
    releases: list[RescueRelease] = []
    if product in {None, "cli"}:
        cli_tags = (
            tag_source.tags("geldata/gel-cli") if tag_source is not None else tags
        )
        selected = [
            item for item in _final_cli_tags(cli_tags) if version in (None, item[0])
        ]
        if product == "cli" and not selected:
            raise ValueError(f"no final gel-cli tag for {version}")
        for selected_version, tag in selected:
            try:
                releases.append(_cli_release(selected_version, records, tag))
            except ValueError:
                if product == "cli":
                    raise
            if (
                product is None
                and sum(item.repository == "gelstable/gel-cli" for item in releases)
                == 3
            ):
                break
    if product in {None, "server"}:
        server_tags = tag_source.tags("geldata/gel") if tag_source is not None else tags
        server_selected = [
            tag for tag in _server_tags(server_tags) if version in (None, tag.name[1:])
        ]
        if product == "server" and not server_selected:
            raise ValueError(f"no final gel server tag for {version}")
        selected_by_major: dict[str, list[UpstreamTag]] = {}
        for tag in server_selected:
            major = tag.name[1]
            selected_by_major.setdefault(major, []).append(tag)
        for tags_for_major in selected_by_major.values():
            complete = 0
            for tag in tags_for_major:
                try:
                    release = _server_release(tag, records)
                except ValueError:
                    # A one-off ``--product server --version X.Y`` request
                    # must still fail loudly; bulk selection keeps filling
                    # from older minors when the legacy capture predates a
                    # newer tag.
                    if product == "server":
                        raise
                    continue
                releases.append(release)
                complete += 1
                if complete == 3:
                    break
    if product in {None, "ls"}:
        selected_versions = [
            item for item in _ls_versions(records) if version in (None, item)
        ]
        if product == "ls" and not selected_versions:
            raise ValueError(f"no language-server build for {version}")
        for selected_version in selected_versions:
            try:
                releases.append(_ls_release(selected_version, records))
            except ValueError:
                if product == "ls":
                    raise
                continue
            break
    if product in {None, "postgis"}:
        slots = sorted(
            {
                str(record.entry.tags.get("server_slot"))
                for record in records
                if record.channel == "stable"
                and record.entry.tags.get("extension") == "postgis"
            }
        )
        for selected_slot in slots if product is None else (slot,):
            assert selected_slot is not None
            try:
                releases.append(_postgis_release(selected_slot, records))
            except ValueError:
                if product == "postgis":
                    raise
    return RescuePlan(
        schema_version=1,
        capture=capture,
        releases=tuple(
            sorted(releases, key=lambda release: (release.repository, release.tag))
        ),
    )


__all__ = [
    "GitHubTagSource",
    "GitTagSource",
    "Product",
    "TagSource",
    "UpstreamTag",
    "artifact_name",
    "plan_rescue",
]

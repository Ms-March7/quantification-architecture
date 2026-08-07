from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tarfile
import tempfile
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from .catalog import (
    CatalogError,
    DatasetCatalog,
    IntegrityError,
    InvalidRelease,
    Release,
    ReleaseSpec,
    _normalize_spec,
    _normalize_timestamp,
    _validate_identifier,
    canonical_json,
    strict_json_loads,
)


SHA256_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
    | {f"COM{number}" for number in "¹²³"}
    | {f"LPT{number}" for number in "¹²³"}
)
MAX_TAR_EXTENDED_HEADER_BYTES = 1024 * 1024
MAX_TAR_TOTAL_EXTENDED_HEADER_BYTES = 64 * 1024 * 1024


class _BoundedTarInfo(tarfile.TarInfo):
    """Reject oversized helper headers before stdlib tarfile allocates them."""

    max_extended_headers = 1_000_000

    def _check_extended_header(self, tar_file) -> None:  # type: ignore[no-untyped-def]
        if self.size < 0 or self.size > MAX_TAR_EXTENDED_HEADER_BYTES:
            raise tarfile.InvalidHeaderError(
                f"tar extended header exceeds {MAX_TAR_EXTENDED_HEADER_BYTES} bytes"
            )
        count = int(getattr(tar_file, "_quant_safe_extended_header_count", 0)) + 1
        if count > self.max_extended_headers:
            raise tarfile.InvalidHeaderError("tar exceeds extended-header count limit")
        total = int(
            getattr(tar_file, "_quant_safe_extended_header_bytes", 0)
        ) + self._block(self.size)
        if total > MAX_TAR_TOTAL_EXTENDED_HEADER_BYTES:
            raise tarfile.InvalidHeaderError(
                "tar exceeds cumulative extended-header byte limit"
            )
        tar_file._quant_safe_extended_header_count = count
        tar_file._quant_safe_extended_header_bytes = total

    def _proc_pax(self, tar_file):  # type: ignore[no-untyped-def]
        self._check_extended_header(tar_file)
        return super()._proc_pax(tar_file)

    def _proc_gnulong(self, tar_file):  # type: ignore[no-untyped-def]
        self._check_extended_header(tar_file)
        return super()._proc_gnulong(tar_file)


class GitHubMetadataError(InvalidRelease):
    """Saved GitHub API metadata does not satisfy the ingestion contract."""


class StagedAssetError(IntegrityError):
    """The staged archive does not match publisher metadata."""


class UnsafeArchiveError(IntegrityError):
    """The archive contains an unsafe or unsupported entry."""


class QlibValidationError(IntegrityError):
    """Extracted data does not satisfy the minimum Qlib binary contract."""


def _required_string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise GitHubMetadataError(f"GitHub metadata field {name!r} must be a non-empty string")
    return value.strip()


def _required_positive_int(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GitHubMetadataError(f"GitHub metadata field {name!r} must be a positive integer")
    return value


def _require_https_url(value: str, *, host: str, path_prefix: str, name: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise GitHubMetadataError(f"{name} contains an invalid port") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != host.casefold()
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise GitHubMetadataError(f"{name} must be a canonical HTTPS URL on {host}")
    decoded_path = unquote(parsed.path)
    if not decoded_path.casefold().startswith(path_prefix.casefold()):
        raise GitHubMetadataError(f"{name} does not belong to the expected repository")
    return value


@dataclass(frozen=True)
class GitHubAssetMetadata:
    asset_id: int
    name: str
    size: int
    publisher_sha256: str
    api_url: str
    browser_download_url: str


@dataclass(frozen=True)
class GitHubReleaseMetadata:
    owner: str
    repository: str
    release_id: int
    tag_name: str
    published_at: str
    api_url: str
    html_url: str
    assets: tuple[GitHubAssetMetadata, ...]
    metadata_sha256: str

    def select_asset(self, name: str) -> GitHubAssetMetadata:
        matches = [asset for asset in self.assets if asset.name == name]
        if len(matches) != 1:
            raise GitHubMetadataError(
                f"expected exactly one GitHub release asset named {name!r}; observed {len(matches)}"
            )
        return matches[0]


def parse_github_release_metadata(
    payload: Mapping[str, Any] | str | bytes,
    *,
    owner: str,
    repository: str,
) -> GitHubReleaseMetadata:
    """Validate saved GitHub REST release metadata without performing network I/O."""

    if isinstance(payload, (str, bytes)):
        parsed_payload = strict_json_loads(payload)
    else:
        parsed_payload = strict_json_loads(canonical_json(dict(payload)))
    if not isinstance(parsed_payload, dict):
        raise GitHubMetadataError("GitHub release metadata must be a JSON object")
    normalized_owner = str(owner).strip()
    normalized_repository = str(repository).strip()
    if (
        not normalized_owner
        or not normalized_repository
        or any(separator in normalized_owner + normalized_repository for separator in ("/", "\\"))
    ):
        raise GitHubMetadataError("owner and repository must be non-empty single path components")

    release_id = _required_positive_int(parsed_payload, "id")
    tag_name = _required_string(parsed_payload, "tag_name")
    if parsed_payload.get("draft") is not False:
        raise GitHubMetadataError("draft or missing GitHub releases are forbidden")
    if parsed_payload.get("prerelease") is not False:
        raise GitHubMetadataError("prerelease or missing GitHub releases are forbidden")
    try:
        published_at = _normalize_timestamp(
            _required_string(parsed_payload, "published_at"), "published_at"
        )
    except InvalidRelease as exc:
        raise GitHubMetadataError(str(exc)) from exc
    assert published_at is not None

    repository_api_prefix = f"/repos/{normalized_owner}/{normalized_repository}/"
    api_url = _require_https_url(
        _required_string(parsed_payload, "url"),
        host="api.github.com",
        path_prefix=repository_api_prefix,
        name="release url",
    )
    if urlsplit(api_url).path.casefold() != (
        f"/repos/{normalized_owner}/{normalized_repository}/releases/{release_id}"
    ).casefold():
        raise GitHubMetadataError("release API URL does not match the release id")
    html_url = _require_https_url(
        _required_string(parsed_payload, "html_url"),
        host="github.com",
        path_prefix=f"/{normalized_owner}/{normalized_repository}/releases/",
        name="release html_url",
    )

    raw_assets = parsed_payload.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise GitHubMetadataError("GitHub release metadata must contain a non-empty assets list")
    assets: list[GitHubAssetMetadata] = []
    seen_names: set[str] = set()
    seen_ids: set[int] = set()
    for index, raw_asset in enumerate(raw_assets):
        if not isinstance(raw_asset, dict):
            raise GitHubMetadataError(f"assets[{index}] must be an object")
        asset_id = _required_positive_int(raw_asset, "id")
        asset_name = _required_string(raw_asset, "name")
        if (
            asset_name in {".", ".."}
            or "/" in asset_name
            or "\\" in asset_name
            or len(asset_name) > 255
        ):
            raise GitHubMetadataError(f"unsafe GitHub asset name: {asset_name!r}")
        if asset_name in seen_names or asset_id in seen_ids:
            raise GitHubMetadataError("GitHub assets must have unique names and ids")
        seen_names.add(asset_name)
        seen_ids.add(asset_id)
        size = _required_positive_int(raw_asset, "size")
        digest_value = _required_string(raw_asset, "digest")
        digest_match = SHA256_DIGEST.fullmatch(digest_value)
        if digest_match is None:
            raise GitHubMetadataError(
                f"asset {asset_name!r} must carry a lowercase sha256 publisher digest"
            )
        if raw_asset.get("state") != "uploaded":
            raise GitHubMetadataError(f"asset {asset_name!r} is not in uploaded state")
        asset_api_url = _require_https_url(
            _required_string(raw_asset, "url"),
            host="api.github.com",
            path_prefix=repository_api_prefix,
            name=f"asset {asset_name!r} url",
        )
        if urlsplit(asset_api_url).path.casefold() != (
            f"/repos/{normalized_owner}/{normalized_repository}/releases/assets/{asset_id}"
        ).casefold():
            raise GitHubMetadataError(f"asset {asset_name!r} API URL does not match its id")
        browser_download_url = _require_https_url(
            _required_string(raw_asset, "browser_download_url"),
            host="github.com",
            path_prefix=f"/{normalized_owner}/{normalized_repository}/releases/download/",
            name=f"asset {asset_name!r} browser_download_url",
        )
        assets.append(
            GitHubAssetMetadata(
                asset_id=asset_id,
                name=asset_name,
                size=size,
                publisher_sha256=digest_match.group(1),
                api_url=asset_api_url,
                browser_download_url=browser_download_url,
            )
        )
    return GitHubReleaseMetadata(
        owner=normalized_owner,
        repository=normalized_repository,
        release_id=release_id,
        tag_name=tag_name,
        published_at=published_at,
        api_url=api_url,
        html_url=html_url,
        assets=tuple(assets),
        metadata_sha256=hashlib.sha256(canonical_json(parsed_payload)).hexdigest(),
    )


def validate_staged_asset(
    path: str | Path,
    asset: GitHubAssetMetadata,
    *,
    max_asset_bytes: int,
) -> str:
    archive = Path(os.path.abspath(os.fspath(path)))
    if (
        isinstance(max_asset_bytes, bool)
        or not isinstance(max_asset_bytes, int)
        or max_asset_bytes <= 0
    ):
        raise InvalidRelease("max_asset_bytes must be a positive integer")
    try:
        archive_lstat = archive.lstat()
    except FileNotFoundError as exc:
        raise StagedAssetError(f"staged archive is missing: {archive}") from exc
    if archive.is_symlink() or not stat.S_ISREG(archive_lstat.st_mode):
        raise StagedAssetError(f"staged archive must be a regular non-link file: {archive}")
    if asset.size > max_asset_bytes:
        raise StagedAssetError(
            f"publisher asset size {asset.size} exceeds configured limit {max_asset_bytes}"
        )
    if archive_lstat.st_size != asset.size:
        raise StagedAssetError(
            f"staged asset size mismatch: expected {asset.size}, observed {archive_lstat.st_size}"
        )
    hasher = hashlib.sha256()
    try:
        with archive.open("rb") as source:
            opened_stat = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or not os.path.samestat(archive_lstat, opened_stat)
                or opened_stat.st_size != asset.size
            ):
                raise StagedAssetError("staged archive changed before validation opened it")
            for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
                hasher.update(block)
            finished_stat = os.fstat(source.fileno())
            if (
                finished_stat.st_size != opened_stat.st_size
                or finished_stat.st_mtime_ns != opened_stat.st_mtime_ns
            ):
                raise StagedAssetError("staged archive changed while it was being validated")
        final_lstat = archive.lstat()
    except FileNotFoundError as exc:
        raise StagedAssetError("staged archive disappeared during validation") from exc
    if not os.path.samestat(archive_lstat, final_lstat):
        raise StagedAssetError("staged archive path was replaced during validation")
    observed = hasher.hexdigest()
    if observed != asset.publisher_sha256:
        raise StagedAssetError(
            f"staged asset digest mismatch: expected {asset.publisher_sha256}, observed {observed}"
        )
    return observed


def _copy_verified_staged_asset(
    path: str | Path,
    destination: Path,
    asset: GitHubAssetMetadata,
    *,
    max_asset_bytes: int,
) -> str:
    """Copy exactly the authenticated bytes used by extraction.

    Hashing a staging pathname and reopening that pathname later leaves a
    replace-after-check window.  This bounded copy is made inside the private
    work directory, authenticated while the source handle remains open, and is
    the only archive subsequently parsed.
    """

    archive = Path(os.path.abspath(os.fspath(path)))
    if (
        isinstance(max_asset_bytes, bool)
        or not isinstance(max_asset_bytes, int)
        or max_asset_bytes <= 0
    ):
        raise InvalidRelease("max_asset_bytes must be a positive integer")
    if asset.size > max_asset_bytes:
        raise StagedAssetError(
            f"publisher asset size {asset.size} exceeds configured limit {max_asset_bytes}"
        )
    try:
        initial_lstat = archive.lstat()
    except FileNotFoundError as exc:
        raise StagedAssetError(f"staged archive is missing: {archive}") from exc
    if archive.is_symlink() or not stat.S_ISREG(initial_lstat.st_mode):
        raise StagedAssetError(f"staged archive must be a regular non-link file: {archive}")
    if initial_lstat.st_size != asset.size:
        raise StagedAssetError(
            f"staged asset size mismatch: expected {asset.size}, observed {initial_lstat.st_size}"
        )

    hasher = hashlib.sha256()
    copied = 0
    try:
        with archive.open("rb") as source, destination.open("xb") as output:
            opened_stat = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or not os.path.samestat(initial_lstat, opened_stat)
                or opened_stat.st_size != asset.size
            ):
                raise StagedAssetError("staged archive changed before the verified copy opened it")
            while copied < asset.size:
                block = source.read(min(8 * 1024 * 1024, asset.size - copied))
                if not block:
                    raise StagedAssetError(
                        f"staged asset truncated during verified copy at byte {copied}"
                    )
                output.write(block)
                hasher.update(block)
                copied += len(block)
            if source.read(1):
                raise StagedAssetError("staged asset grew beyond its publisher-declared size")
            finished_stat = os.fstat(source.fileno())
            if (
                finished_stat.st_size != opened_stat.st_size
                or finished_stat.st_mtime_ns != opened_stat.st_mtime_ns
            ):
                raise StagedAssetError("staged archive changed during the verified copy")
            output.flush()
            os.fsync(output.fileno())
        final_lstat = archive.lstat()
        copied_lstat = destination.lstat()
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    if not os.path.samestat(initial_lstat, final_lstat):
        destination.unlink(missing_ok=True)
        raise StagedAssetError("staged archive path was replaced during the verified copy")
    if not stat.S_ISREG(copied_lstat.st_mode) or copied_lstat.st_size != asset.size:
        destination.unlink(missing_ok=True)
        raise StagedAssetError("private verified archive copy has an invalid type or size")
    observed = hasher.hexdigest()
    if observed != asset.publisher_sha256:
        destination.unlink(missing_ok=True)
        raise StagedAssetError(
            f"staged asset digest mismatch: expected {asset.publisher_sha256}, observed {observed}"
        )
    return observed


def _safe_archive_path(raw_name: str, *, is_directory: bool) -> PurePosixPath:
    if not isinstance(raw_name, str) or not raw_name or len(raw_name) > 4096:
        raise UnsafeArchiveError("archive member name is empty or too long")
    normalized = raw_name.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if is_directory:
        normalized = normalized.rstrip("/")
    if "\x00" in normalized or normalized.startswith("/") or WINDOWS_DRIVE.match(normalized):
        raise UnsafeArchiveError(f"absolute archive member path is forbidden: {raw_name!r}")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise UnsafeArchiveError(f"ambiguous or traversing archive member path: {raw_name!r}")
    for part in parts:
        if any(ord(character) < 32 for character in part):
            raise UnsafeArchiveError(f"control character in archive member path: {raw_name!r}")
        if any(character in WINDOWS_FORBIDDEN for character in part):
            raise UnsafeArchiveError(f"Windows-unsafe archive member path: {raw_name!r}")
        if part.endswith((" ", ".")):
            raise UnsafeArchiveError(f"Windows-ambiguous archive member path: {raw_name!r}")
        stem = part.split(".", 1)[0].upper()
        if stem in WINDOWS_RESERVED:
            raise UnsafeArchiveError(f"Windows-reserved archive member path: {raw_name!r}")
        try:
            utf16_units = len(part.encode("utf-16-le")) // 2
        except UnicodeEncodeError as exc:
            raise UnsafeArchiveError(
                f"invalid Unicode in archive member path: {raw_name!r}"
            ) from exc
        if utf16_units > 255:
            raise UnsafeArchiveError(
                f"Windows path component exceeds 255 UTF-16 code units: {raw_name!r}"
            )
    path = PurePosixPath(*parts)
    if path.is_absolute():
        raise UnsafeArchiveError(f"absolute archive member path is forbidden: {raw_name!r}")
    return path


@dataclass(frozen=True)
class ExtractionReceipt:
    member_count: int
    expanded_bytes: int


def safely_extract_tar(
    archive_path: str | Path,
    destination: str | Path,
    *,
    max_members: int,
    max_expanded_bytes: int,
) -> ExtractionReceipt:
    """Extract a tar archive manually after a complete validation pass."""

    if isinstance(max_members, bool) or not isinstance(max_members, int) or max_members <= 0:
        raise InvalidRelease("max_members must be a positive integer")
    if (
        isinstance(max_expanded_bytes, bool)
        or not isinstance(max_expanded_bytes, int)
        or max_expanded_bytes <= 0
    ):
        raise InvalidRelease("max_expanded_bytes must be a positive integer")
    raw_target_root = Path(os.path.abspath(os.fspath(destination)))
    if raw_target_root.exists():
        if raw_target_root.is_symlink() or not raw_target_root.is_dir() or any(raw_target_root.iterdir()):
            raise UnsafeArchiveError("safe extraction destination must be an empty real directory")
    else:
        raw_target_root.mkdir(parents=True)
    target_root = raw_target_root.resolve()

    planned: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
    known_kinds: dict[str, str] = {}
    canonical_names: dict[str, str] = {}
    expanded_bytes = 0
    observed_members = 0
    bounded_tar_info = type(
        "_RequestBoundedTarInfo",
        (_BoundedTarInfo,),
        {"max_extended_headers": max_members},
    )
    try:
        with tarfile.open(
            Path(archive_path), mode="r:*", tarinfo=bounded_tar_info
        ) as archive:
            for member in archive:
                observed_members += 1
                extended_headers = int(
                    getattr(archive, "_quant_safe_extended_header_count", 0)
                )
                if observed_members + extended_headers > max_members:
                    raise UnsafeArchiveError(f"archive exceeds member limit {max_members}")
                normalized_member_name = member.name.replace("\\", "/")
                if member.isdir() and normalized_member_name in {".", "./"}:
                    if member.size != 0:
                        raise UnsafeArchiveError(
                            f"directory archive entry has non-zero size: {member.name!r}"
                        )
                    continue
                path = _safe_archive_path(member.name, is_directory=member.isdir())
                if member.issym() or member.islnk():
                    raise UnsafeArchiveError(f"archive links are forbidden: {member.name!r}")
                if member.issparse() or getattr(member, "sparse", None):
                    raise UnsafeArchiveError(f"sparse archive entries are forbidden: {member.name!r}")
                if not (member.isdir() or member.isreg()):
                    raise UnsafeArchiveError(f"unsupported archive entry type: {member.name!r}")
                if member.isdir() and member.size != 0:
                    raise UnsafeArchiveError(
                        f"directory archive entry has non-zero size: {member.name!r}"
                    )
                path_text = path.as_posix()
                folded = path_text.casefold()
                existing_spelling = canonical_names.get(folded)
                if existing_spelling is not None and existing_spelling != path_text:
                    raise UnsafeArchiveError(
                        f"case-insensitive archive path collision: {existing_spelling!r}, {path_text!r}"
                    )
                canonical_names[folded] = path_text
                expected_kind = "dir" if member.isdir() else "file"
                existing_kind = known_kinds.get(path_text)
                if existing_kind is not None:
                    if expected_kind == "file" and existing_kind == "dir":
                        raise UnsafeArchiveError(
                            f"archive file replaces a directory: {path_text!r}"
                        )
                    raise UnsafeArchiveError(f"duplicate archive member path: {path_text!r}")
                for parent in path.parents:
                    if str(parent) == ".":
                        continue
                    parent_text = parent.as_posix()
                    parent_folded = parent_text.casefold()
                    parent_spelling = canonical_names.get(parent_folded)
                    if parent_spelling is not None and parent_spelling != parent_text:
                        raise UnsafeArchiveError(
                            f"case-insensitive archive path collision: {parent_spelling!r}, {parent_text!r}"
                        )
                    canonical_names[parent_folded] = parent_text
                    if known_kinds.get(parent_text) == "file":
                        raise UnsafeArchiveError(f"archive file is used as a parent: {parent_text!r}")
                    known_kinds.setdefault(parent_text, "dir")
                known_kinds[path_text] = expected_kind
                if member.isreg():
                    if member.size < 0:
                        raise UnsafeArchiveError(f"negative archive member size: {member.name!r}")
                    expanded_bytes += member.size
                    if expanded_bytes > max_expanded_bytes:
                        raise UnsafeArchiveError(
                            f"archive exceeds expanded-byte limit {max_expanded_bytes}"
                        )
                planned.append((member, path))

            for member, relative in planned:
                destination_path = target_root.joinpath(*relative.parts)
                resolved_destination = destination_path.resolve(strict=False)
                try:
                    resolved_destination.relative_to(target_root)
                except ValueError as exc:
                    raise UnsafeArchiveError(
                        f"archive member escapes extraction root: {member.name!r}"
                    ) from exc
                if member.isdir():
                    destination_path.mkdir(parents=True, exist_ok=True)
                    continue
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise UnsafeArchiveError(f"cannot read archive member: {member.name!r}")
                copied = 0
                with source, destination_path.open("xb") as output:
                    while copied < member.size:
                        block = source.read(min(8 * 1024 * 1024, member.size - copied))
                        if not block:
                            raise UnsafeArchiveError(
                                f"truncated archive member: {member.name!r}"
                            )
                        output.write(block)
                        copied += len(block)
                    if source.read(1):
                        raise UnsafeArchiveError(
                            f"archive member exceeds declared size: {member.name!r}"
                        )
                    output.flush()
    except (tarfile.TarError, OSError) as exc:
        if isinstance(exc, (UnsafeArchiveError, InvalidRelease)):
            raise
        raise UnsafeArchiveError(f"cannot safely extract tar archive: {exc}") from exc
    extended_headers = int(
        getattr(archive, "_quant_safe_extended_header_count", 0)
    )
    return ExtractionReceipt(
        member_count=observed_members + extended_headers,
        expanded_bytes=expanded_bytes,
    )


@dataclass(frozen=True)
class QlibValidationReceipt:
    root: Path
    calendar_start: str
    calendar_end: str
    calendar_rows: int
    instrument_rows: int
    feature_files: int
    feature_instruments: int


def _strict_iso_date(value: str, name: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise QlibValidationError(f"{name} must be an ISO date in YYYY-MM-DD form")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise QlibValidationError(f"{name} is not a valid calendar date") from exc
    if parsed.isoformat() != value:
        raise QlibValidationError(f"{name} is not a canonical ISO date")
    return parsed


def validate_qlib_directory(
    extraction_root: str | Path,
    *,
    expected_calendar_end: str,
    max_search_depth: int = 3,
) -> QlibValidationReceipt:
    raw_root = Path(os.path.abspath(os.fspath(extraction_root)))
    expected_end = _strict_iso_date(str(expected_calendar_end).strip(), "expected_calendar_end")
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise QlibValidationError(f"extraction root is not a real directory: {raw_root}")
    root = raw_root.resolve()
    if (
        isinstance(max_search_depth, bool)
        or not isinstance(max_search_depth, int)
        or max_search_depth < 0
    ):
        raise InvalidRelease("max_search_depth must be a non-negative integer")
    candidates: list[Path] = []
    queue = deque([root])
    while queue:
        candidate = queue.popleft()
        depth = len(candidate.relative_to(root).parts)
        if all(
            (candidate / component).is_dir()
            for component in ("calendars", "features", "instruments")
        ):
            candidates.append(candidate)
        if depth >= max_search_depth:
            continue
        for child in sorted(candidate.iterdir(), key=lambda item: item.name.casefold()):
            if child.is_symlink():
                raise QlibValidationError(f"symlink is forbidden in extracted Qlib data: {child}")
            if child.is_dir():
                queue.append(child)
    if len(candidates) != 1:
        raise QlibValidationError(
            f"expected exactly one Qlib root within depth {max_search_depth}; observed {len(candidates)}"
        )
    qlib_root = candidates[0]

    calendar_file = qlib_root / "calendars" / "day.txt"
    if calendar_file.is_symlink() or not calendar_file.is_file():
        raise QlibValidationError("Qlib calendars/day.txt is required")
    if calendar_file.stat().st_size > 16 * 1024 * 1024:
        raise QlibValidationError("Qlib day calendar exceeds 16 MiB safety limit")
    try:
        calendar_raw = calendar_file.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise QlibValidationError("Qlib day calendar must be UTF-8") from exc
    calendar_values: list[date] = []
    for row_number, raw_line in enumerate(calendar_raw.splitlines(), 1):
        line = raw_line.strip()
        if not line or line != raw_line:
            raise QlibValidationError(f"invalid blank or padded calendar row {row_number}")
        calendar_values.append(_strict_iso_date(line, f"calendar row {row_number}"))
    if not calendar_values:
        raise QlibValidationError("Qlib day calendar is empty")
    if any(current >= following for current, following in zip(calendar_values, calendar_values[1:])):
        raise QlibValidationError("Qlib day calendar must be strictly increasing and unique")
    if calendar_values[-1] != expected_end:
        raise QlibValidationError(
            f"Qlib calendar end mismatch: expected {expected_end}, observed {calendar_values[-1]}"
        )

    instruments_file = qlib_root / "instruments" / "all.txt"
    if instruments_file.is_symlink() or not instruments_file.is_file():
        raise QlibValidationError("Qlib instruments/all.txt is required")
    if instruments_file.stat().st_size > 64 * 1024 * 1024:
        raise QlibValidationError("Qlib instruments file exceeds 64 MiB safety limit")
    try:
        instrument_lines = [
            line for line in instruments_file.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
    except UnicodeDecodeError as exc:
        raise QlibValidationError("Qlib instruments file must be UTF-8") from exc
    if not instrument_lines or any("\x00" in line for line in instrument_lines):
        raise QlibValidationError("Qlib instruments file is empty or malformed")

    feature_root = qlib_root / "features"
    instrument_directories = [
        path for path in feature_root.iterdir() if path.is_dir() and not path.is_symlink()
    ]
    feature_files = [
        path
        for path in feature_root.rglob("*.day.bin")
        if path.is_file() and not path.is_symlink() and path.stat().st_size > 0
    ]
    if not instrument_directories or not feature_files:
        raise QlibValidationError(
            "Qlib features must contain at least one instrument directory and non-empty .day.bin file"
        )
    return QlibValidationReceipt(
        root=qlib_root,
        calendar_start=calendar_values[0].isoformat(),
        calendar_end=calendar_values[-1].isoformat(),
        calendar_rows=len(calendar_values),
        instrument_rows=len(instrument_lines),
        feature_files=len(feature_files),
        feature_instruments=len(instrument_directories),
    )


@dataclass(frozen=True)
class GitHubIngestRequest:
    owner: str
    repository: str
    metadata: Mapping[str, Any] | str | bytes
    asset_name: str
    staged_archive: str | Path
    publish_root: str | Path
    dataset_id: str
    snapshot_id: str
    expected_calendar_end: str
    source_tier: str
    frequency: str
    universe: str
    coverage: Mapping[str, Any]
    schema_version: str
    license: str
    ingested_at: str | datetime | date | None = None
    revised_at: str | datetime | date | None = None
    pit_verified: bool = False
    promotion_ceiling: str = "RESEARCH_ONLY"
    expected_tag: str | None = None
    expected_asset_bytes: int | None = None
    expected_publisher_sha256: str | None = None
    max_asset_bytes: int = 20 * 1024**3
    max_members: int = 1_000_000
    max_expanded_bytes: int = 200 * 1024**3
    max_qlib_search_depth: int = 3


def _already_registered(catalog: DatasetCatalog, dataset_id: str, snapshot_id: str) -> bool:
    return any(
        release.snapshot_id == snapshot_id
        for release in catalog.list_releases(dataset_id)
    )


def _registered_release(
    catalog: DatasetCatalog, dataset_id: str, snapshot_id: str
) -> Release | None:
    matches = [
        release
        for release in catalog.list_releases(dataset_id)
        if release.snapshot_id == snapshot_id
    ]
    if len(matches) > 1:
        raise IntegrityError(
            f"catalog contains duplicate release identity: {dataset_id}/{snapshot_id}"
        )
    return matches[0] if matches else None


def _validate_publish_identifier(value: str, name: str) -> str:
    identifier = _validate_identifier(value, name)
    if identifier.endswith((" ", ".")):
        raise InvalidRelease(f"{name} is ambiguous on Windows: {identifier!r}")
    if identifier.split(".", 1)[0].upper() in WINDOWS_RESERVED:
        raise InvalidRelease(f"{name} is reserved on Windows: {identifier!r}")
    return identifier


def _assert_no_casefold_child(parent: Path, requested: str, label: str) -> None:
    if not parent.exists():
        return
    if parent.is_symlink() or not parent.is_dir():
        raise InvalidRelease(f"{label} parent must be a real directory: {parent}")
    folded = requested.casefold()
    for child in parent.iterdir():
        if child.name.casefold() == folded and child.name != requested:
            raise IntegrityError(
                f"case-insensitive {label} collision: requested {requested!r}, "
                f"existing {child.name!r}"
            )


def _assert_no_catalog_casefold_collision(
    catalog: DatasetCatalog, dataset_id: str, snapshot_id: str
) -> None:
    dataset_folded = dataset_id.casefold()
    snapshot_folded = snapshot_id.casefold()
    for release in catalog.list_releases():
        if release.dataset_id.casefold() != dataset_folded:
            continue
        if release.dataset_id != dataset_id:
            raise IntegrityError(
                f"case-insensitive dataset_id collision: requested {dataset_id!r}, "
                f"catalog has {release.dataset_id!r}"
            )
        if (
            release.snapshot_id.casefold() == snapshot_folded
            and release.snapshot_id != snapshot_id
        ):
            raise IntegrityError(
                f"case-insensitive snapshot_id collision: requested {snapshot_id!r}, "
                f"catalog has {release.snapshot_id!r}"
            )


def _catalog_ingest_lock(
    catalog: DatasetCatalog, dataset_id: str, snapshot_id: str
) -> tuple[int, Path, Path]:
    lock_root = catalog.release_root / ".ingest-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    if lock_root.is_symlink() or not lock_root.is_dir():
        raise CatalogError(f"catalog ingest lock root must be a real directory: {lock_root}")
    identity = f"{dataset_id.casefold()}\0{snapshot_id.casefold()}".encode("utf-8")
    lock_path = lock_root / f"{hashlib.sha256(identity).hexdigest()}.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise CatalogError(
            f"another ingestion owns the catalog snapshot lock: {dataset_id}/{snapshot_id}"
        ) from exc
    return descriptor, lock_path, lock_root


def _release_points_to(release: Release | None, root: Path) -> bool:
    if release is None:
        return False
    try:
        registered_root = Path(release.root_path).resolve()
        expected_root = root.resolve()
    except OSError:
        return False
    return os.path.normcase(str(registered_root)) == os.path.normcase(str(expected_root))


def _remove_empty_parents(path: Path, stop: Path) -> None:
    current = path
    while current != stop:
        try:
            current.rmdir()
        except (FileNotFoundError, OSError):
            return
        current = current.parent


def ingest_staged_github_release(
    catalog: DatasetCatalog,
    request: GitHubIngestRequest,
) -> Release:
    """Validate, safely extract, atomically publish, and register a staged release.

    This function deliberately has no downloader. The caller must save both the
    GitHub API response and the asset first; ingestion then treats both as
    untrusted local inputs.
    """

    dataset_id = _validate_publish_identifier(request.dataset_id, "dataset_id")
    snapshot_id = _validate_publish_identifier(request.snapshot_id, "snapshot_id")
    metadata = parse_github_release_metadata(
        request.metadata, owner=request.owner, repository=request.repository
    )
    expected_tag = request.expected_tag if request.expected_tag is not None else snapshot_id
    if metadata.tag_name != expected_tag:
        raise GitHubMetadataError(
            f"GitHub tag mismatch: expected {expected_tag!r}, observed {metadata.tag_name!r}"
        )
    asset = metadata.select_asset(request.asset_name)
    if request.expected_asset_bytes is not None:
        if (
            isinstance(request.expected_asset_bytes, bool)
            or not isinstance(request.expected_asset_bytes, int)
            or request.expected_asset_bytes <= 0
        ):
            raise InvalidRelease("expected_asset_bytes must be a positive integer")
        if asset.size != request.expected_asset_bytes:
            raise GitHubMetadataError(
                f"GitHub asset size disagrees with out-of-band pin: "
                f"expected {request.expected_asset_bytes}, metadata says {asset.size}"
            )
    if request.expected_publisher_sha256 is not None:
        pinned_digest = str(request.expected_publisher_sha256).strip()
        if SHA256_DIGEST.fullmatch(pinned_digest):
            pinned_digest = pinned_digest[7:]
        if not re.fullmatch(r"[0-9a-f]{64}", pinned_digest):
            raise InvalidRelease(
                "expected_publisher_sha256 must be 64 lowercase hex characters, optionally prefixed by sha256:"
            )
        if asset.publisher_sha256 != pinned_digest:
            raise GitHubMetadataError(
                "GitHub publisher digest disagrees with the out-of-band SHA-256 pin"
            )
    if (
        isinstance(request.max_asset_bytes, bool)
        or not isinstance(request.max_asset_bytes, int)
        or request.max_asset_bytes <= 0
    ):
        raise InvalidRelease("max_asset_bytes must be a positive integer")
    if asset.size > request.max_asset_bytes:
        raise StagedAssetError(
            f"publisher asset size {asset.size} exceeds configured limit "
            f"{request.max_asset_bytes}"
        )
    raw_publish_root = Path(os.path.abspath(os.fspath(request.publish_root)))
    if raw_publish_root.exists() and (
        raw_publish_root.is_symlink() or not raw_publish_root.is_dir()
    ):
        raise InvalidRelease(f"publish_root must be a real directory: {raw_publish_root}")
    raw_publish_root.mkdir(parents=True, exist_ok=True)
    publish_root = raw_publish_root.resolve()
    lock_descriptor, lock_path, lock_root = _catalog_ingest_lock(
        catalog, dataset_id, snapshot_id
    )

    work_root: Path | None = None
    final_release_root: Path | None = None
    final_qlib_root: Path | None = None
    published_here = False
    published_lstat: os.stat_result | None = None
    manifest_path = catalog.release_root / dataset_id / snapshot_id / "manifest.json"
    manifest_existed_before = False
    try:
        _assert_no_catalog_casefold_collision(catalog, dataset_id, snapshot_id)
        _assert_no_casefold_child(publish_root, dataset_id, "publish dataset_id")
        _assert_no_casefold_child(catalog.release_root, dataset_id, "catalog dataset_id")
        dataset_root = publish_root / dataset_id
        if dataset_root.exists() and (
            dataset_root.is_symlink() or not dataset_root.is_dir()
        ):
            raise InvalidRelease(
                f"dataset publish root must be a real directory: {dataset_root}"
            )
        dataset_root.mkdir(parents=True, exist_ok=True)
        _assert_no_casefold_child(dataset_root, snapshot_id, "publish snapshot_id")
        catalog_dataset_root = catalog.release_root / dataset_id
        _assert_no_casefold_child(
            catalog_dataset_root, snapshot_id, "catalog snapshot_id"
        )
        final_release_root = dataset_root / snapshot_id
        manifest_existed_before = manifest_path.exists()
        if final_release_root.exists():
            raise IntegrityError(f"immutable publish target already exists: {final_release_root}")
        if _already_registered(catalog, dataset_id, snapshot_id):
            raise IntegrityError(f"catalog release already exists: {dataset_id}/{snapshot_id}")
        work_root = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}.ingest-", dir=dataset_root))
        verified_archive = work_root / "verified-release-asset.tar"
        observed_asset_hash = _copy_verified_staged_asset(
            request.staged_archive,
            verified_archive,
            asset,
            max_asset_bytes=request.max_asset_bytes,
        )
        extraction_root = work_root / "extracted"
        receipt = safely_extract_tar(
            verified_archive,
            extraction_root,
            max_members=request.max_members,
            max_expanded_bytes=request.max_expanded_bytes,
        )
        qlib = validate_qlib_directory(
            extraction_root,
            expected_calendar_end=request.expected_calendar_end,
            max_search_depth=request.max_qlib_search_depth,
        )
        staged_release_root = work_root / "release"
        staged_release_root.mkdir()
        staged_qlib_root = staged_release_root / "qlib_bin"
        os.rename(qlib.root, staged_qlib_root)
        ingested_at = request.ingested_at or datetime.now(timezone.utc)
        provenance_payload = {
            "asset": {
                "api_url": asset.api_url,
                "browser_download_url": asset.browser_download_url,
                "id": asset.asset_id,
                "name": asset.name,
                "publisher_digest": f"sha256:{asset.publisher_sha256}",
                "sha256": observed_asset_hash,
                "size": asset.size,
            },
            "calendar": {
                "end": qlib.calendar_end,
                "rows": qlib.calendar_rows,
                "start": qlib.calendar_start,
            },
            "extraction": {
                "expanded_bytes": receipt.expanded_bytes,
                "member_count": receipt.member_count,
                "links_allowed": False,
            },
            "github_release": {
                "api_url": metadata.api_url,
                "html_url": metadata.html_url,
                "id": metadata.release_id,
                "metadata_sha256": metadata.metadata_sha256,
                "published_at": metadata.published_at,
                "repository": f"{metadata.owner}/{metadata.repository}",
                "tag_name": metadata.tag_name,
            },
            "hash_verified": True,
            "license": request.license,
            "qlib": {
                "feature_files": qlib.feature_files,
                "feature_instruments": qlib.feature_instruments,
                "instrument_rows": qlib.instrument_rows,
            },
            "research_only": request.promotion_ceiling.upper() == "RESEARCH_ONLY",
            "snapshot_id": snapshot_id,
        }
        provenance_path = staged_release_root / "PROVENANCE.json"
        with provenance_path.open("xb") as output:
            output.write(canonical_json(provenance_payload))
            output.flush()
            os.fsync(output.fileno())
        preflight_spec = ReleaseSpec(
            dataset_id=dataset_id,
            snapshot_id=snapshot_id,
            source=f"github:{metadata.owner}/{metadata.repository}@{metadata.tag_name}",
            source_tier=request.source_tier,
            frequency=request.frequency,
            universe=request.universe,
            coverage=request.coverage,
            schema_version=request.schema_version,
            license=request.license,
            published_at=metadata.published_at,
            available_at=metadata.published_at,
            ingested_at=ingested_at,
            revised_at=request.revised_at,
            pit_verified=request.pit_verified,
            promotion_ceiling=request.promotion_ceiling,
            root_path=staged_qlib_root,
        )
        _normalize_spec(preflight_spec)
        if final_release_root.exists():
            raise IntegrityError(f"immutable publish target appeared concurrently: {final_release_root}")
        os.rename(staged_release_root, final_release_root)
        published_here = True
        published_lstat = final_release_root.lstat()
        final_qlib_root = final_release_root / "qlib_bin"
        final_provenance_path = final_release_root / "PROVENANCE.json"
        final_spec = ReleaseSpec(
            **{
                **preflight_spec.__dict__,
                "root_path": final_qlib_root,
            }
        )
        return catalog.register_release(final_spec, provenance_path=final_provenance_path)
    except Exception:
        registered = _registered_release(catalog, dataset_id, snapshot_id)
        if (
            published_here
            and final_release_root is not None
            and final_qlib_root is not None
            and not _release_points_to(registered, final_qlib_root)
        ):
            if final_release_root.exists():
                current_lstat = final_release_root.lstat()
                if (
                    published_lstat is None
                    or final_release_root.is_symlink()
                    or not os.path.samestat(published_lstat, current_lstat)
                ):
                    raise IntegrityError(
                        "published release path changed during rollback; refusing unsafe deletion"
                    )
                shutil.rmtree(final_release_root)
            published_here = False
        if (
            registered is None
            and not manifest_existed_before
            and manifest_path.is_file()
        ):
            manifest_path.unlink()
            _remove_empty_parents(manifest_path.parent, catalog.release_root)
        raise
    finally:
        if work_root is not None and work_root.exists():
            shutil.rmtree(work_root, ignore_errors=True)
        os.close(lock_descriptor)
        lock_path.unlink(missing_ok=True)
        try:
            lock_root.rmdir()
        except OSError:
            pass

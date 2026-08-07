"""Fail-closed qdata contract for future formal factor-research inputs.

This module deliberately stops at resolution.  It never opens the resolved
dataset and therefore cannot start factor generation, IC evaluation, backtests,
or Validation as a side effect.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_PURPOSE_RANK = {
    "DO_NOT_USE": 0,
    "ENGINEERING_ONLY": 1,
    "SMOKE_ONLY": 2,
    "RESEARCH_ONLY": 3,
    "SHADOW_ONLY": 4,
    "LIVE_READY": 5,
}
_RESEARCH_RANK = _PURPOSE_RANK["RESEARCH_ONLY"]
_RESEARCH_LIFECYCLES = frozenset({"active", "frozen"})
_RESEARCH_LICENSES = frozenset({"VERIFIED", "DECLARED", "NOT_APPLICABLE"})
_FORBIDDEN_RESOLVED_PATH_TOKENS = (
    "local-dataset-build",
    "csmar_pilot",
    "csmar-pilot",
    "d2a03-official-pit-core-slice",
)

R0_PV_CORE_DATASET_IDS = (
    "cn-a-share-security-master-pit",
    "cn-trading-calendar",
    "cn-a-share-daily-raw",
    "cn-a-share-corporate-actions-pit",
    "cn-a-share-daily-adjusted",
    "cn-a-share-tradability-daily-pit",
)
_R0_PV_DATASET_ROLES = {
    "cn-a-share-security-master-pit": "CORE-01",
    "cn-trading-calendar": "CORE-02",
    "cn-a-share-daily-raw": "CORE-03",
    "cn-a-share-corporate-actions-pit": "CORE-04",
    "cn-a-share-daily-adjusted": "CORE-05",
    "cn-a-share-tradability-daily-pit": "CORE-06",
}
_SNAPSHOT_AUTHORITY_RELATIVE_PATHS = frozenset(
    {
        "request.json",
        "manifest.json",
        "provenance.json",
        "partition-manifest.json",
        "quality/summary.json",
    }
)
_SNAPSHOT_CONTRACT_HASH_ALGORITHM = "sha256-snapshot-contract-v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_R0_PV_BUNDLE_CONTRACT_ID = "r0-pv-core01-06-v1"
_R0_PV_COMMON_SEMANTICS = {
    "schema_version": 1,
    "research_tier": "R0_PV",
    "market_scope": "CN_A_SHARE",
    "timezone": "Asia/Shanghai",
    "security_identity_dataset_id": "cn-a-share-security-master-pit",
    "calendar_dataset_id": "cn-trading-calendar",
    "missing_value_policy": "NULL_NEVER_ZERO_FILL",
    "available_at_semantics": "PIT_REVISION_AWARE",
}
_R0_PV_VALUE_CONVENTIONS = {
    "cn-a-share-security-master-pit": "EVENT_OR_REFERENCE",
    "cn-trading-calendar": "EVENT_OR_REFERENCE",
    "cn-a-share-daily-raw": "RAW_UNADJUSTED",
    "cn-a-share-corporate-actions-pit": "EVENT_OR_REFERENCE",
    "cn-a-share-daily-adjusted": "FORWARD_ADJUSTED",
    "cn-a-share-tradability-daily-pit": "EVENT_OR_REFERENCE",
}


class QDataInputError(RuntimeError):
    """Raised when qdata cannot prove a snapshot is research-admissible."""


class _ProcessResult(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], _ProcessResult]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class ResolvedResearchInput:
    """Immutable descriptor returned after qdata and local contract checks."""

    dataset_id: str
    snapshot_id: str
    resolved_path: Path
    purpose_ceiling: str
    lifecycle: str
    quality_status: str
    license_status: str
    available_at: datetime
    content_hash: str | None
    hash_algorithm: str | None
    authority_paths: tuple[Path, ...]
    known_limits: tuple[str, ...]

    @property
    def identity(self) -> str:
        return f"{self.dataset_id}@{self.snapshot_id}"


@dataclass(frozen=True, slots=True)
class ResearchSnapshotRef:
    """One exact catalog identity requested for an R0 bundle preflight."""

    dataset_id: str
    snapshot_id: str


@dataclass(frozen=True, slots=True)
class R0PVSnapshotDescriptor:
    """Manifest-bound metadata for one R0 input; no payload has been opened."""

    dataset_id: str
    role: str
    snapshot_id: str
    resolved_path: Path
    content_hash: str
    contract_id: str
    dataset_schema_version: str
    available_at: str
    bundle_contract_id: str
    bundle_as_of_date: str
    coverage_start_date: str
    coverage_end_date: str
    value_convention: str

    @property
    def identity(self) -> str:
        return f"{self.dataset_id}@{self.snapshot_id}"


@dataclass(frozen=True, slots=True)
class R0PVInputBundleDescriptor:
    """Frozen hand-off to a future production PanelData adapter.

    ``payload_opened`` is deliberately fixed to ``False``.  Producing this
    descriptor proves only metadata and cross-snapshot compatibility; it does
    not build PanelData or authorize factor research.
    """

    schema_version: int
    research_tier: str
    research_start_date: str
    research_end_date: str
    bundle_contract_id: str
    bundle_as_of_date: str
    snapshots: tuple[R0PVSnapshotDescriptor, ...]
    descriptor_hash: str
    payload_opened: bool = False
    production_paneldata_status: str = "BLOCKED__PAYLOAD_NOT_OPENED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "research_tier": self.research_tier,
            "research_start_date": self.research_start_date,
            "research_end_date": self.research_end_date,
            "bundle_contract_id": self.bundle_contract_id,
            "bundle_as_of_date": self.bundle_as_of_date,
            "snapshots": [
                {
                    "dataset_id": item.dataset_id,
                    "role": item.role,
                    "snapshot_id": item.snapshot_id,
                    "resolved_path": str(item.resolved_path),
                    "content_hash": item.content_hash,
                    "contract_id": item.contract_id,
                    "dataset_schema_version": item.dataset_schema_version,
                    "available_at": item.available_at,
                    "bundle_contract_id": item.bundle_contract_id,
                    "bundle_as_of_date": item.bundle_as_of_date,
                    "coverage_start_date": item.coverage_start_date,
                    "coverage_end_date": item.coverage_end_date,
                    "value_convention": item.value_convention,
                }
                for item in self.snapshots
            ],
            "descriptor_hash": self.descriptor_hash,
            "payload_opened": self.payload_opened,
            "production_paneldata_status": self.production_paneldata_status,
        }


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Execute a logical qdata command without exposing a general shell API."""

    logical_command = tuple(str(part) for part in command)
    if os.name == "nt":
        command_line = subprocess.list2cmdline(logical_command)
        process_command: Sequence[str] = (
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            command_line,
        )
    else:
        process_command = logical_command
    return subprocess.run(
        process_command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _identity(label: str, value: str) -> str:
    if not isinstance(value, str):
        raise QDataInputError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized:
        raise QDataInputError(f"{label} must be non-empty")
    if normalized != value:
        raise QDataInputError(f"{label} must not contain surrounding whitespace")
    if not _IDENTITY_PATTERN.fullmatch(normalized):
        raise QDataInputError(
            f"{label} is not a catalog identity; paths and shell syntax are forbidden"
        )
    return normalized


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve(strict=False))))


def _is_within(path: Path, directory: Path) -> bool:
    path_key = _path_key(path)
    directory_key = _path_key(directory)
    try:
        return os.path.commonpath((path_key, directory_key)) == directory_key
    except ValueError:
        return False


def _mapping(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, dict):
        raise QDataInputError("qdata JSON result must be one object")
    return payload


def _non_empty_string(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise QDataInputError(f"qdata result is missing {key}")
    return value


class QDataResearchResolver:
    """Resolve one exact immutable snapshot through the workspace qdata CLI."""

    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        runner: Runner | None = None,
        now_provider: Clock | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root or _workspace_root()).resolve(strict=False)
        self.qdata_command = self.workspace_root / "data" / "catalog" / "qdata.cmd"
        self._runner = runner or _default_runner
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def resolve(self, dataset_id: str, snapshot_id: str) -> ResolvedResearchInput:
        """Return metadata for an exact research snapshot; never read its payload."""

        dataset_id = _identity("dataset_id", dataset_id)
        snapshot_id = _identity("snapshot_id", snapshot_id)
        if "latest" in snapshot_id.casefold():
            raise QDataInputError("research input forbids latest snapshots")
        if not self.qdata_command.is_file():
            raise QDataInputError(f"workspace qdata command is missing: {self.qdata_command}")

        command = (
            str(self.qdata_command),
            "resolve",
            dataset_id,
            snapshot_id,
            "--intent",
            "research",
            "--json",
        )
        try:
            result = self._runner(command)
        except OSError as error:
            raise QDataInputError(f"qdata execution failed: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "no diagnostic").strip()
            raise QDataInputError(f"qdata research resolve was rejected: {detail}")
        try:
            record = _mapping(json.loads(result.stdout))
        except (json.JSONDecodeError, TypeError) as error:
            raise QDataInputError("qdata did not return valid JSON") from error

        return self._validate_record(record, dataset_id=dataset_id, snapshot_id=snapshot_id)

    def _validate_record(
        self,
        record: Mapping[str, Any],
        *,
        dataset_id: str,
        snapshot_id: str,
    ) -> ResolvedResearchInput:
        if record.get("dataset_id") != dataset_id or record.get("snapshot_id") != snapshot_id:
            raise QDataInputError("qdata returned a different dataset identity")
        if record.get("requested_intent") != "research":
            raise QDataInputError("qdata result is not a research-intent resolution")
        if record.get("mutability") != "immutable":
            raise QDataInputError("factor research requires an immutable snapshot")

        purpose_ceiling = _non_empty_string(record, "purpose_ceiling")
        purpose_rank = _PURPOSE_RANK.get(purpose_ceiling)
        if purpose_rank is None or purpose_rank < _RESEARCH_RANK:
            raise QDataInputError(
                f"purpose ceiling {purpose_ceiling!r} does not permit research"
            )

        lifecycle = _non_empty_string(record, "lifecycle")
        if lifecycle not in _RESEARCH_LIFECYCLES:
            raise QDataInputError(f"lifecycle {lifecycle!r} is not open to new research")

        quality_status = _non_empty_string(record, "quality_status")
        if quality_status != "OK":
            raise QDataInputError(
                f"quality_status={quality_status!r}; qdata resolution alone is not a quality gate"
            )

        license_status = _non_empty_string(record, "license_status")
        if license_status not in _RESEARCH_LICENSES:
            raise QDataInputError(
                f"license_status={license_status!r} is not cleared for research"
            )

        raw_available_at = _non_empty_string(record, "available_at")
        try:
            available_at = datetime.fromisoformat(
                raw_available_at.replace("Z", "+00:00").replace("z", "+00:00")
            )
        except ValueError as error:
            raise QDataInputError("qdata available_at must be RFC 3339") from error
        if available_at.tzinfo is None or available_at.utcoffset() is None:
            raise QDataInputError("qdata available_at must include an explicit UTC offset")
        now = self._now_provider()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise QDataInputError("research access clock must be timezone-aware")
        if available_at.astimezone(timezone.utc) > now.astimezone(timezone.utc):
            raise QDataInputError(
                f"qdata snapshot is not available until {available_at.isoformat()}"
            )

        if record.get("path_exists") is not True:
            raise QDataInputError("qdata reports that the registered snapshot path is missing")
        resolved_path = Path(_non_empty_string(record, "resolved_path"))
        physical_path = Path(_non_empty_string(record, "physical_path"))
        if not resolved_path.is_absolute() or not physical_path.is_absolute():
            raise QDataInputError("qdata returned a non-absolute snapshot path")
        if _path_key(resolved_path) != _path_key(physical_path):
            raise QDataInputError("qdata physical_path and resolved_path disagree")
        self._reject_candidate_path(resolved_path)

        authority_status = record.get("authority_status")
        if not isinstance(authority_status, list) or not authority_status:
            raise QDataInputError("qdata returned no authority status")
        if any(
            not isinstance(item, dict)
            or item.get("exists") is not True
            or not isinstance(item.get("path"), str)
            or not item["path"].strip()
            for item in authority_status
        ):
            raise QDataInputError("one or more qdata authority paths are missing")

        authority_paths_raw = record.get("authority_paths")
        if not isinstance(authority_paths_raw, list) or not authority_paths_raw:
            raise QDataInputError("qdata returned no authority paths")
        authority_paths = tuple(Path(str(path)) for path in authority_paths_raw)
        if any(not path.is_absolute() for path in authority_paths):
            raise QDataInputError("qdata returned a non-absolute authority path")

        known_limits_raw = record.get("known_limits", [])
        if not isinstance(known_limits_raw, list) or any(
            not isinstance(value, str) for value in known_limits_raw
        ):
            raise QDataInputError("qdata known_limits must be a string list")

        content_hash = record.get("content_hash")
        hash_algorithm = record.get("hash_algorithm")
        if content_hash is not None and not isinstance(content_hash, str):
            raise QDataInputError("qdata content_hash must be a string when present")
        if hash_algorithm is not None and not isinstance(hash_algorithm, str):
            raise QDataInputError("qdata hash_algorithm must be a string when present")

        return ResolvedResearchInput(
            dataset_id=dataset_id,
            snapshot_id=snapshot_id,
            resolved_path=resolved_path,
            purpose_ceiling=purpose_ceiling,
            lifecycle=lifecycle,
            quality_status=quality_status,
            license_status=license_status,
            available_at=available_at,
            content_hash=content_hash,
            hash_algorithm=hash_algorithm,
            authority_paths=authority_paths,
            known_limits=tuple(known_limits_raw),
        )

    def _reject_candidate_path(self, resolved_path: Path) -> None:
        for directory in (
            self.workspace_root / "tmp",
            self.workspace_root / "inbox",
            self.workspace_root / "data" / "market" / "raw",
            self.workspace_root / "data" / "market" / "curated",
            self.workspace_root / "data" / "market" / "quarantine",
        ):
            if _is_within(resolved_path, directory):
                raise QDataInputError(
                    "qdata resolved a staging/candidate path, not a research snapshot"
                )
        normalized = _path_key(resolved_path).replace("\\", "/").casefold()
        if any(token in normalized for token in _FORBIDDEN_RESOLVED_PATH_TOKENS):
            raise QDataInputError("qdata resolved a known candidate-build path")


def resolve_research_input(
    dataset_id: str,
    snapshot_id: str,
    *,
    workspace_root: str | Path | None = None,
) -> ResolvedResearchInput:
    """Convenience wrapper for one exact qdata research resolution."""

    return QDataResearchResolver(workspace_root=workspace_root).resolve(
        dataset_id,
        snapshot_id,
    )


AuthorityLoader = Callable[[Path], Mapping[str, Any]]


def _strict_json_object(path: Path) -> Mapping[str, Any]:
    """Read one authority JSON object and reject duplicate keys."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise QDataInputError(f"authority JSON has duplicate key {key!r}: {path}")
            value[key] = item
        return value

    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=reject_duplicates)
    except QDataInputError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise QDataInputError(f"cannot read authority JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise QDataInputError(f"authority JSON must be one object: {path}")
    return value


def _date_value(label: str, value: Any) -> date:
    if not isinstance(value, str):
        raise QDataInputError(f"{label} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise QDataInputError(f"{label} must be an ISO date") from error
    if parsed.isoformat() != value:
        raise QDataInputError(f"{label} must use canonical YYYY-MM-DD form")
    return parsed


def _rfc3339_value(label: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise QDataInputError(f"{label} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as error:
        raise QDataInputError(f"{label} must be an RFC 3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QDataInputError(f"{label} must include an explicit UTC offset")
    return value


def _canonical_descriptor_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class R0PVInputBundlePreflight:
    """Validate the exact CORE-01..06 metadata bundle before payload access.

    The preflight calls qdata with ``--intent research`` for every exact
    identity, validates the five authority documents, and checks a manifest
    ``research_input_contract`` shared by all six snapshots.  It intentionally
    has no payload reader and no PanelData dependency.
    """

    def __init__(
        self,
        *,
        resolver: QDataResearchResolver | None = None,
        authority_loader: AuthorityLoader | None = None,
    ) -> None:
        self.resolver = resolver or QDataResearchResolver()
        self._authority_loader = authority_loader or _strict_json_object

    def preflight(
        self,
        refs: Sequence[ResearchSnapshotRef],
        *,
        research_start_date: str,
        research_end_date: str,
    ) -> R0PVInputBundleDescriptor:
        research_start = _date_value("research_start_date", research_start_date)
        research_end = _date_value("research_end_date", research_end_date)
        if research_start > research_end:
            raise QDataInputError("research_start_date must not follow research_end_date")

        normalized_refs = self._validate_refs(refs)
        snapshots = tuple(
            self._resolve_and_validate(
                ref,
                research_start=research_start,
                research_end=research_end,
            )
            for ref in normalized_refs
        )
        bundle_contract_ids = {item.bundle_contract_id for item in snapshots}
        bundle_as_of_dates = {item.bundle_as_of_date for item in snapshots}
        if len(bundle_contract_ids) != 1:
            raise QDataInputError("R0 snapshots use inconsistent bundle_contract_id values")
        if len(bundle_as_of_dates) != 1:
            raise QDataInputError("R0 snapshots use inconsistent bundle_as_of_date values")
        bundle_contract_id = next(iter(bundle_contract_ids))
        bundle_as_of_date = next(iter(bundle_as_of_dates))
        if _date_value("bundle_as_of_date", bundle_as_of_date) < research_end:
            raise QDataInputError("bundle_as_of_date is earlier than research_end_date")

        descriptor_body = {
            "schema_version": 1,
            "research_tier": "R0_PV",
            "research_start_date": research_start_date,
            "research_end_date": research_end_date,
            "bundle_contract_id": bundle_contract_id,
            "bundle_as_of_date": bundle_as_of_date,
            "snapshots": [
                {
                    "dataset_id": item.dataset_id,
                    "role": item.role,
                    "snapshot_id": item.snapshot_id,
                    "resolved_path": str(item.resolved_path),
                    "content_hash": item.content_hash,
                    "contract_id": item.contract_id,
                    "dataset_schema_version": item.dataset_schema_version,
                    "available_at": item.available_at,
                    "bundle_contract_id": item.bundle_contract_id,
                    "bundle_as_of_date": item.bundle_as_of_date,
                    "coverage_start_date": item.coverage_start_date,
                    "coverage_end_date": item.coverage_end_date,
                    "value_convention": item.value_convention,
                }
                for item in snapshots
            ],
            "payload_opened": False,
            "production_paneldata_status": "BLOCKED__PAYLOAD_NOT_OPENED",
        }
        return R0PVInputBundleDescriptor(
            schema_version=1,
            research_tier="R0_PV",
            research_start_date=research_start_date,
            research_end_date=research_end_date,
            bundle_contract_id=bundle_contract_id,
            bundle_as_of_date=bundle_as_of_date,
            snapshots=snapshots,
            descriptor_hash=_canonical_descriptor_hash(descriptor_body),
        )

    @staticmethod
    def _validate_refs(
        refs: Sequence[ResearchSnapshotRef],
    ) -> tuple[ResearchSnapshotRef, ...]:
        if isinstance(refs, (str, bytes)):
            raise QDataInputError("R0 bundle refs must be a sequence of exact identities")
        normalized: list[ResearchSnapshotRef] = []
        seen: set[str] = set()
        for index, ref in enumerate(refs):
            if not isinstance(ref, ResearchSnapshotRef):
                raise QDataInputError(f"R0 bundle refs[{index}] is not ResearchSnapshotRef")
            dataset_id = _identity(f"refs[{index}].dataset_id", ref.dataset_id)
            snapshot_id = _identity(f"refs[{index}].snapshot_id", ref.snapshot_id)
            if "latest" in snapshot_id.casefold():
                raise QDataInputError("research input forbids latest snapshots")
            if dataset_id in seen:
                raise QDataInputError(f"duplicate R0 dataset identity: {dataset_id}")
            seen.add(dataset_id)
            normalized.append(ResearchSnapshotRef(dataset_id, snapshot_id))
        expected = set(R0_PV_CORE_DATASET_IDS)
        actual = {ref.dataset_id for ref in normalized}
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise QDataInputError(
                "R0 bundle must contain exactly CORE-01..06; "
                f"missing={missing}, unexpected={unexpected}"
            )
        by_id = {ref.dataset_id: ref for ref in normalized}
        return tuple(by_id[dataset_id] for dataset_id in R0_PV_CORE_DATASET_IDS)

    def _resolve_and_validate(
        self,
        ref: ResearchSnapshotRef,
        *,
        research_start: date,
        research_end: date,
    ) -> R0PVSnapshotDescriptor:
        resolved = self.resolver.resolve(ref.dataset_id, ref.snapshot_id)
        if resolved.dataset_id != ref.dataset_id or resolved.snapshot_id != ref.snapshot_id:
            raise QDataInputError("qdata returned a different R0 snapshot identity")
        if not resolved.content_hash or not _SHA256_PATTERN.fullmatch(resolved.content_hash):
            raise QDataInputError(f"{resolved.identity} has no canonical lowercase content_hash")
        if resolved.hash_algorithm != _SNAPSHOT_CONTRACT_HASH_ALGORITHM:
            raise QDataInputError(
                f"{resolved.identity} does not use {_SNAPSHOT_CONTRACT_HASH_ALGORITHM}"
            )

        authorities = self._authority_documents(resolved)
        documents = {
            relative: self._authority_loader(path)
            for relative, path in authorities.items()
        }
        for relative, document in documents.items():
            if document.get("dataset_id") != resolved.dataset_id:
                raise QDataInputError(
                    f"{resolved.identity} {relative} has a different dataset_id"
                )
            if document.get("snapshot_id") != resolved.snapshot_id:
                raise QDataInputError(
                    f"{resolved.identity} {relative} has a different snapshot_id"
                )

        request = documents["request.json"]
        manifest = documents["manifest.json"]
        provenance = documents["provenance.json"]
        quality = documents["quality/summary.json"]
        if request.get("mode") == "synthetic_fixture" or manifest.get("synthetic_fixture") is True:
            raise QDataInputError(f"{resolved.identity} is a synthetic fixture")
        if provenance.get("mode") == "synthetic_fixture" or quality.get("synthetic_fixture") is True:
            raise QDataInputError(f"{resolved.identity} has synthetic authority evidence")
        if manifest.get("status") != "SEALED_SNAPSHOT":
            raise QDataInputError(f"{resolved.identity} manifest is not SEALED_SNAPSHOT")
        if manifest.get("hash_algorithm") != _SNAPSHOT_CONTRACT_HASH_ALGORITHM:
            raise QDataInputError(f"{resolved.identity} manifest hash algorithm is unsupported")
        if quality.get("catalog_quality_status") != "OK":
            raise QDataInputError(f"{resolved.identity} quality authority is not OK")
        if quality.get("purpose_ceiling") not in {"RESEARCH_ONLY", "SHADOW_ONLY", "LIVE_READY"}:
            raise QDataInputError(f"{resolved.identity} quality authority forbids research")

        contract_id = _non_empty_string(manifest, "contract_id")
        dataset_schema_version = _non_empty_string(manifest, "dataset_schema_version")
        available_at = _rfc3339_value(
            f"{resolved.identity}.manifest.available_at",
            manifest.get("available_at"),
        )
        manifest_available_at = datetime.fromisoformat(
            available_at.replace("Z", "+00:00").replace("z", "+00:00")
        )
        if manifest_available_at != resolved.available_at:
            raise QDataInputError(
                f"{resolved.identity} manifest.available_at disagrees with qdata"
            )
        semantic = manifest.get("research_input_contract")
        if not isinstance(semantic, dict):
            raise QDataInputError(
                f"{resolved.identity} lacks formal manifest.research_input_contract; "
                "production PanelData remains blocked"
            )
        for key, expected in _R0_PV_COMMON_SEMANTICS.items():
            if semantic.get(key) != expected:
                raise QDataInputError(
                    f"{resolved.identity} research_input_contract.{key} is inconsistent"
                )
        role = _R0_PV_DATASET_ROLES[resolved.dataset_id]
        if semantic.get("role") != role:
            raise QDataInputError(f"{resolved.identity} has the wrong CORE role")
        value_convention = _non_empty_string(semantic, "value_convention")
        if value_convention != _R0_PV_VALUE_CONVENTIONS[resolved.dataset_id]:
            raise QDataInputError(f"{resolved.identity} has an incompatible value_convention")
        coverage_start = _date_value(
            f"{resolved.identity}.coverage_start_date",
            semantic.get("coverage_start_date"),
        )
        coverage_end = _date_value(
            f"{resolved.identity}.coverage_end_date",
            semantic.get("coverage_end_date"),
        )
        if coverage_start > research_start or coverage_end < research_end:
            raise QDataInputError(
                f"{resolved.identity} does not cover the requested research window"
            )
        if coverage_start > coverage_end:
            raise QDataInputError(f"{resolved.identity} has an inverted coverage window")
        bundle_contract_id = _non_empty_string(semantic, "bundle_contract_id")
        if bundle_contract_id != _R0_PV_BUNDLE_CONTRACT_ID:
            raise QDataInputError(
                f"{resolved.identity} uses an unsupported bundle_contract_id"
            )
        bundle_as_of_date = _date_value(
            f"{resolved.identity}.bundle_as_of_date",
            semantic.get("bundle_as_of_date"),
        ).isoformat()

        return R0PVSnapshotDescriptor(
            dataset_id=resolved.dataset_id,
            role=role,
            snapshot_id=resolved.snapshot_id,
            resolved_path=resolved.resolved_path,
            content_hash=resolved.content_hash,
            contract_id=contract_id,
            dataset_schema_version=dataset_schema_version,
            available_at=available_at,
            bundle_contract_id=bundle_contract_id,
            bundle_as_of_date=bundle_as_of_date,
            coverage_start_date=coverage_start.isoformat(),
            coverage_end_date=coverage_end.isoformat(),
            value_convention=value_convention,
        )

    @staticmethod
    def _authority_documents(
        resolved: ResolvedResearchInput,
    ) -> dict[str, Path]:
        root = resolved.resolved_path.resolve(strict=False)
        authorities: dict[str, Path] = {}
        for path in resolved.authority_paths:
            candidate = path.resolve(strict=False)
            if not _is_within(candidate, root):
                raise QDataInputError(f"{resolved.identity} authority path escapes snapshot root")
            try:
                relative = candidate.relative_to(root).as_posix()
            except ValueError as error:
                raise QDataInputError(
                    f"{resolved.identity} authority path is not inside snapshot root"
                ) from error
            if relative in authorities:
                raise QDataInputError(f"{resolved.identity} has duplicate authority paths")
            authorities[relative] = candidate
        if set(authorities) != _SNAPSHOT_AUTHORITY_RELATIVE_PATHS:
            raise QDataInputError(
                f"{resolved.identity} must expose exactly the five snapshot authority documents"
            )
        if any(not path.is_file() for path in authorities.values()):
            raise QDataInputError(f"{resolved.identity} has a missing authority document")
        return authorities

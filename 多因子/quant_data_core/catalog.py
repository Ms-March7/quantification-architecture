from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping


CATALOG_SCHEMA_VERSION = 1
TREE_HASH_ALGORITHM = "sha256-tree-v1"
SOURCE_TIERS = frozenset({"CANONICAL", "VERIFIED_SECONDARY", "RESEARCH_ONLY", "BROKER_LIVE"})
PROMOTION_LEVELS = {
    "ENGINEERING_ONLY": 0,
    "RESEARCH_ONLY": 1,
    "SHADOW_ONLY": 2,
    "LIVE_READY": 3,
}
PURPOSE_LEVELS = {"engineering": 0, "research": 1, "shadow": 2, "live": 3}
SOURCE_TIER_MAX_LEVEL = {
    "RESEARCH_ONLY": 1,
    "VERIFIED_SECONDARY": 2,
    "CANONICAL": 3,
    "BROKER_LIVE": 3,
}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class CatalogError(RuntimeError):
    pass


class InvalidRelease(CatalogError):
    pass


class ReleaseNotFound(CatalogError):
    pass


class IntegrityError(CatalogError):
    pass


class PromotionDenied(CatalogError):
    pass


class ImplicitLatestForbidden(CatalogError):
    pass


def _reject_constant(value: str) -> None:
    raise InvalidRelease(f"non-finite JSON constant is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidRelease(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_loads(raw: str | bytes) -> Any:
    try:
        return json.loads(
            raw,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidRelease(f"invalid JSON: {exc}") from exc


def canonical_json(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InvalidRelease(f"value is not strict JSON: {exc}") from exc


def sha256_file(path: str | Path, block_size: int = 8 * 1024 * 1024) -> str:
    target = Path(path)
    if not target.is_file() or target.is_symlink():
        raise IntegrityError(f"expected a regular file: {target}")
    hasher = hashlib.sha256()
    with target.open("rb") as handle:
        for block in iter(lambda: handle.read(block_size), b""):
            hasher.update(block)
    return hasher.hexdigest()


def hash_directory(root: str | Path, block_size: int = 8 * 1024 * 1024) -> str:
    """Hash names, directory structure, file sizes, and bytes deterministically.

    Symlinks are rejected so a release cannot silently escape its declared root.
    Market data bytes are treated as opaque content; this function does not parse
    or materialize any feature value.
    """

    base = Path(root).resolve()
    if not base.is_dir():
        raise IntegrityError(f"dataset root is not a directory: {base}")
    entries = sorted(base.rglob("*"), key=lambda item: item.relative_to(base).as_posix())
    hasher = hashlib.sha256(TREE_HASH_ALGORITHM.encode("ascii"))
    for item in entries:
        relative = item.relative_to(base).as_posix().encode("utf-8")
        if item.is_symlink():
            raise IntegrityError(f"symlink is forbidden in an immutable release: {item}")
        if item.is_dir():
            hasher.update(b"D")
            hasher.update(len(relative).to_bytes(8, "big"))
            hasher.update(relative)
            continue
        if not item.is_file():
            raise IntegrityError(f"unsupported filesystem entry: {item}")
        size = item.stat().st_size
        hasher.update(b"F")
        hasher.update(len(relative).to_bytes(8, "big"))
        hasher.update(relative)
        hasher.update(size.to_bytes(8, "big"))
        with item.open("rb") as handle:
            for block in iter(lambda: handle.read(block_size), b""):
                hasher.update(block)
    return hasher.hexdigest()


def _normalize_timestamp(value: str | datetime | date | None, name: str, *, optional: bool = False) -> str | None:
    if value is None:
        if optional:
            return None
        raise InvalidRelease(f"{name} is required")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            if optional:
                return None
            raise InvalidRelease(f"{name} is required")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            parsed = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        else:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise InvalidRelease(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidRelease(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_identifier(value: str, name: str) -> str:
    text = str(value).strip()
    if not IDENTIFIER.fullmatch(text):
        raise InvalidRelease(f"{name} must match {IDENTIFIER.pattern}")
    return text


def _validate_coverage(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise InvalidRelease("coverage must be a non-empty JSON object")
    normalized = strict_json_loads(canonical_json(dict(value)))

    def walk(node: Any) -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise InvalidRelease("coverage cannot contain non-finite numbers")
        if isinstance(node, dict):
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(normalized)
    return normalized


@dataclass(frozen=True)
class ReleaseSpec:
    dataset_id: str
    snapshot_id: str
    source: str
    source_tier: str
    frequency: str
    universe: str
    coverage: Mapping[str, Any]
    schema_version: str
    license: str
    published_at: str | datetime | date
    available_at: str | datetime | date
    ingested_at: str | datetime | date
    revised_at: str | datetime | date | None
    pit_verified: bool
    promotion_ceiling: str
    root_path: str | Path


@dataclass(frozen=True)
class Release:
    dataset_id: str
    snapshot_id: str
    source: str
    source_tier: str
    frequency: str
    universe: str
    coverage: dict[str, Any]
    schema_version: str
    license: str
    published_at: str
    available_at: str
    ingested_at: str
    revised_at: str | None
    content_hash: str
    pit_verified: bool
    promotion_ceiling: str
    root_path: str
    provenance_path: str
    provenance_hash: str
    manifest_path: str
    manifest_hash: str
    hash_algorithm: str = TREE_HASH_ALGORITHM

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_spec(spec: ReleaseSpec) -> dict[str, Any]:
    dataset_id = _validate_identifier(spec.dataset_id, "dataset_id")
    snapshot_id = _validate_identifier(spec.snapshot_id, "snapshot_id")
    source = str(spec.source).strip()
    frequency = str(spec.frequency).strip()
    universe = str(spec.universe).strip()
    schema_version = str(spec.schema_version).strip()
    license_name = str(spec.license).strip()
    if not all((source, frequency, universe, schema_version, license_name)):
        raise InvalidRelease("source, frequency, universe, schema_version, and license are required")
    source_tier = str(spec.source_tier).strip().upper()
    if source_tier not in SOURCE_TIERS:
        raise InvalidRelease(f"unknown source_tier: {source_tier}")
    promotion_ceiling = str(spec.promotion_ceiling).strip().upper()
    if promotion_ceiling not in PROMOTION_LEVELS:
        raise InvalidRelease(f"unknown promotion_ceiling: {promotion_ceiling}")
    if PROMOTION_LEVELS[promotion_ceiling] > SOURCE_TIER_MAX_LEVEL[source_tier]:
        raise InvalidRelease(
            f"source_tier {source_tier} cannot claim promotion ceiling {promotion_ceiling}"
        )
    if not isinstance(spec.pit_verified, bool):
        raise InvalidRelease("pit_verified must be boolean")
    root_path = Path(spec.root_path).resolve()
    if not root_path.is_dir():
        raise InvalidRelease(f"root_path is not a directory: {root_path}")
    published_at = _normalize_timestamp(spec.published_at, "published_at")
    available_at = _normalize_timestamp(spec.available_at, "available_at")
    ingested_at = _normalize_timestamp(spec.ingested_at, "ingested_at")
    revised_at = _normalize_timestamp(spec.revised_at, "revised_at", optional=True)
    assert published_at is not None and available_at is not None and ingested_at is not None
    if available_at < published_at:
        raise InvalidRelease("available_at cannot precede published_at")
    if ingested_at < available_at:
        raise InvalidRelease("ingested_at cannot precede available_at")
    if revised_at is not None and revised_at < published_at:
        raise InvalidRelease("revised_at cannot precede published_at")
    return {
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "source": source,
        "source_tier": source_tier,
        "frequency": frequency,
        "universe": universe,
        "coverage": _validate_coverage(spec.coverage),
        "schema_version": schema_version,
        "license": license_name,
        "published_at": published_at,
        "available_at": available_at,
        "ingested_at": ingested_at,
        "revised_at": revised_at,
        "pit_verified": spec.pit_verified,
        "promotion_ceiling": promotion_ceiling,
        "root_path": str(root_path),
    }


def _locate_provenance(root: Path, supplied: str | Path | None) -> Path:
    if supplied is not None:
        candidate = Path(supplied).resolve()
    else:
        candidates = (root / "PROVENANCE.json", root.parent / "PROVENANCE.json")
        candidate = next((item.resolve() for item in candidates if item.is_file()), Path())
    if not candidate.is_file() or candidate.is_symlink():
        raise InvalidRelease(f"PROVENANCE.json is required: {candidate}")
    return candidate


def _read_and_validate_provenance(path: Path, snapshot_id: str, ceiling: str) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    payload = strict_json_loads(raw)
    if not isinstance(payload, dict):
        raise InvalidRelease("PROVENANCE.json must contain an object")
    provenance_snapshot = payload.get("snapshot_date") or payload.get("snapshot_id")
    if provenance_snapshot is not None and str(provenance_snapshot) != snapshot_id:
        raise InvalidRelease(
            f"PROVENANCE snapshot {provenance_snapshot!r} does not match {snapshot_id!r}"
        )
    if payload.get("research_only") is True and PROMOTION_LEVELS[ceiling] > PROMOTION_LEVELS["RESEARCH_ONLY"]:
        raise InvalidRelease("research-only provenance cannot be promoted above RESEARCH_ONLY")
    return payload, hashlib.sha256(raw).hexdigest()


def _atomic_write_manifest(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise IntegrityError(f"immutable manifest already exists with different bytes: {path}")
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            if path.read_bytes() != payload:
                raise IntegrityError(f"immutable manifest appeared with different bytes: {path}")
            return
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class DatasetCatalog:
    def __init__(self, database: str | Path) -> None:
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.release_root = self.database.parent / "releases"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database), timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _session(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._session() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalog_meta (
                    schema_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS releases (
                    dataset_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_tier TEXT NOT NULL,
                    frequency TEXT NOT NULL,
                    universe TEXT NOT NULL,
                    coverage TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    license TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    revised_at TEXT,
                    content_hash TEXT NOT NULL,
                    pit_verified INTEGER NOT NULL CHECK (pit_verified IN (0, 1)),
                    promotion_ceiling TEXT NOT NULL,
                    root_path TEXT NOT NULL,
                    provenance_path TEXT NOT NULL,
                    provenance_hash TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    hash_algorithm TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, snapshot_id)
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS releases_available
                    ON releases(dataset_id, available_at DESC, snapshot_id DESC);
                CREATE TRIGGER IF NOT EXISTS releases_immutable_update
                    BEFORE UPDATE ON releases
                    BEGIN SELECT RAISE(ABORT, 'dataset releases are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS releases_immutable_delete
                    BEFORE DELETE ON releases
                    BEGIN SELECT RAISE(ABORT, 'dataset releases are immutable'); END;
                """
            )
            rows = connection.execute("SELECT schema_version FROM catalog_meta").fetchall()
            if not rows:
                connection.execute(
                    "INSERT INTO catalog_meta(schema_version) VALUES (?)",
                    (CATALOG_SCHEMA_VERSION,),
                )
            elif len(rows) != 1 or int(rows[0][0]) != CATALOG_SCHEMA_VERSION:
                raise CatalogError("unsupported or corrupt catalog schema")

    def _manifest_path(self, dataset_id: str, snapshot_id: str) -> Path:
        return self.release_root / dataset_id / snapshot_id / "manifest.json"

    @staticmethod
    def _row_to_release(row: sqlite3.Row) -> Release:
        return Release(
            dataset_id=row["dataset_id"],
            snapshot_id=row["snapshot_id"],
            source=row["source"],
            source_tier=row["source_tier"],
            frequency=row["frequency"],
            universe=row["universe"],
            coverage=strict_json_loads(row["coverage"]),
            schema_version=row["schema_version"],
            license=row["license"],
            published_at=row["published_at"],
            available_at=row["available_at"],
            ingested_at=row["ingested_at"],
            revised_at=row["revised_at"],
            content_hash=row["content_hash"],
            pit_verified=bool(row["pit_verified"]),
            promotion_ceiling=row["promotion_ceiling"],
            root_path=row["root_path"],
            provenance_path=row["provenance_path"],
            provenance_hash=row["provenance_hash"],
            manifest_path=row["manifest_path"],
            manifest_hash=row["manifest_hash"],
            hash_algorithm=row["hash_algorithm"],
        )

    def register_release(
        self,
        spec: ReleaseSpec,
        *,
        provenance_path: str | Path | None = None,
        expected_content_hash: str | None = None,
    ) -> Release:
        normalized = _normalize_spec(spec)
        root = Path(normalized["root_path"])
        provenance = _locate_provenance(root, provenance_path)
        _, provenance_hash = _read_and_validate_provenance(
            provenance,
            normalized["snapshot_id"],
            normalized["promotion_ceiling"],
        )
        content_hash = hash_directory(root)
        if expected_content_hash is not None:
            expected = str(expected_content_hash).strip().lower()
            if not HEX64.fullmatch(expected):
                raise InvalidRelease("expected_content_hash must be 64 lowercase hexadecimal characters")
            if content_hash != expected:
                raise IntegrityError(
                    f"directory content hash mismatch: expected {expected}, observed {content_hash}"
                )
        manifest_path = self._manifest_path(normalized["dataset_id"], normalized["snapshot_id"])
        manifest_payload = {
            **normalized,
            "catalog_schema_version": CATALOG_SCHEMA_VERSION,
            "content_hash": content_hash,
            "hash_algorithm": TREE_HASH_ALGORITHM,
            "provenance_path": str(provenance),
            "provenance_hash": provenance_hash,
        }
        manifest_bytes = canonical_json(manifest_payload)
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        complete = {
            **manifest_payload,
            "manifest_path": str(manifest_path.resolve()),
            "manifest_hash": manifest_hash,
        }
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                "SELECT * FROM releases WHERE dataset_id=? AND snapshot_id=?",
                (normalized["dataset_id"], normalized["snapshot_id"]),
            ).fetchone()
            if existing_row is not None:
                existing = self._row_to_release(existing_row)
                if existing.to_dict() != Release(
                    dataset_id=complete["dataset_id"],
                    snapshot_id=complete["snapshot_id"],
                    source=complete["source"],
                    source_tier=complete["source_tier"],
                    frequency=complete["frequency"],
                    universe=complete["universe"],
                    coverage=complete["coverage"],
                    schema_version=complete["schema_version"],
                    license=complete["license"],
                    published_at=complete["published_at"],
                    available_at=complete["available_at"],
                    ingested_at=complete["ingested_at"],
                    revised_at=complete["revised_at"],
                    content_hash=complete["content_hash"],
                    pit_verified=complete["pit_verified"],
                    promotion_ceiling=complete["promotion_ceiling"],
                    root_path=complete["root_path"],
                    provenance_path=complete["provenance_path"],
                    provenance_hash=complete["provenance_hash"],
                    manifest_path=complete["manifest_path"],
                    manifest_hash=complete["manifest_hash"],
                    hash_algorithm=complete["hash_algorithm"],
                ).to_dict():
                    raise IntegrityError("dataset_id/snapshot_id is already registered with different immutable content")
                return existing
            _atomic_write_manifest(manifest_path, manifest_bytes)
            connection.execute(
                """
                INSERT INTO releases(
                    dataset_id, snapshot_id, source, source_tier, frequency, universe,
                    coverage, schema_version, license, published_at, available_at,
                    ingested_at, revised_at, content_hash, pit_verified,
                    promotion_ceiling, root_path, provenance_path, provenance_hash,
                    manifest_path, manifest_hash, hash_algorithm
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    complete["dataset_id"], complete["snapshot_id"], complete["source"],
                    complete["source_tier"], complete["frequency"], complete["universe"],
                    canonical_json(complete["coverage"]).decode("utf-8").strip(),
                    complete["schema_version"], complete["license"], complete["published_at"],
                    complete["available_at"], complete["ingested_at"], complete["revised_at"],
                    complete["content_hash"], int(complete["pit_verified"]),
                    complete["promotion_ceiling"], complete["root_path"],
                    complete["provenance_path"], complete["provenance_hash"],
                    complete["manifest_path"], complete["manifest_hash"],
                    complete["hash_algorithm"],
                ),
            )
        return Release(
            dataset_id=complete["dataset_id"],
            snapshot_id=complete["snapshot_id"],
            source=complete["source"],
            source_tier=complete["source_tier"],
            frequency=complete["frequency"],
            universe=complete["universe"],
            coverage=complete["coverage"],
            schema_version=complete["schema_version"],
            license=complete["license"],
            published_at=complete["published_at"],
            available_at=complete["available_at"],
            ingested_at=complete["ingested_at"],
            revised_at=complete["revised_at"],
            content_hash=complete["content_hash"],
            pit_verified=complete["pit_verified"],
            promotion_ceiling=complete["promotion_ceiling"],
            root_path=complete["root_path"],
            provenance_path=complete["provenance_path"],
            provenance_hash=complete["provenance_hash"],
            manifest_path=complete["manifest_path"],
            manifest_hash=complete["manifest_hash"],
            hash_algorithm=complete["hash_algorithm"],
        )

    def list_releases(self, dataset_id: str | None = None) -> list[Release]:
        with self._session() as connection:
            if dataset_id is None:
                rows = connection.execute(
                    "SELECT * FROM releases ORDER BY dataset_id, available_at, snapshot_id"
                ).fetchall()
            else:
                identifier = _validate_identifier(dataset_id, "dataset_id")
                rows = connection.execute(
                    "SELECT * FROM releases WHERE dataset_id=? ORDER BY available_at, snapshot_id",
                    (identifier,),
                ).fetchall()
        return [self._row_to_release(row) for row in rows]

    def verify_release(self, release: Release, *, verify_content: bool = True) -> None:
        manifest = Path(release.manifest_path)
        provenance = Path(release.provenance_path)
        root = Path(release.root_path)
        if not manifest.is_file() or manifest.is_symlink():
            raise IntegrityError(f"release manifest is missing: {manifest}")
        raw_manifest = manifest.read_bytes()
        if hashlib.sha256(raw_manifest).hexdigest() != release.manifest_hash:
            raise IntegrityError("release manifest hash mismatch")
        payload = strict_json_loads(raw_manifest)
        expected_manifest = {
            key: value
            for key, value in release.to_dict().items()
            if key not in {"manifest_path", "manifest_hash"}
        }
        expected_manifest["catalog_schema_version"] = CATALOG_SCHEMA_VERSION
        if payload != expected_manifest:
            raise IntegrityError("release manifest does not match the catalog row")
        if not provenance.is_file() or provenance.is_symlink():
            raise IntegrityError(f"PROVENANCE.json is missing: {provenance}")
        if sha256_file(provenance) != release.provenance_hash:
            raise IntegrityError("PROVENANCE.json content hash mismatch")
        _read_and_validate_provenance(provenance, release.snapshot_id, release.promotion_ceiling)
        if verify_content:
            observed = hash_directory(root)
            if observed != release.content_hash:
                raise IntegrityError(
                    f"dataset directory content hash mismatch: expected {release.content_hash}, observed {observed}"
                )

    def resolve(
        self,
        dataset_id: str,
        purpose: str,
        as_of: str | datetime | date | None = None,
        snapshot_id: str | None = None,
    ) -> Release:
        identifier = _validate_identifier(dataset_id, "dataset_id")
        normalized_purpose = str(purpose).strip().lower()
        if normalized_purpose not in PURPOSE_LEVELS:
            raise PromotionDenied(f"unknown purpose: {purpose!r}")
        if normalized_purpose != "engineering" and snapshot_id is None and as_of is None:
            raise ImplicitLatestForbidden(
                f"purpose={normalized_purpose} forbids implicit latest; provide snapshot_id or as_of"
            )
        normalized_snapshot = (
            _validate_identifier(snapshot_id, "snapshot_id") if snapshot_id is not None else None
        )
        normalized_as_of = _normalize_timestamp(as_of, "as_of", optional=True)
        with self._session() as connection:
            if normalized_snapshot is not None:
                row = connection.execute(
                    "SELECT * FROM releases WHERE dataset_id=? AND snapshot_id=?",
                    (identifier, normalized_snapshot),
                ).fetchone()
            elif normalized_as_of is not None:
                row = connection.execute(
                    """
                    SELECT * FROM releases
                    WHERE dataset_id=? AND available_at<=?
                    ORDER BY available_at DESC, snapshot_id DESC LIMIT 1
                    """,
                    (identifier, normalized_as_of),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM releases WHERE dataset_id=?
                    ORDER BY available_at DESC, snapshot_id DESC LIMIT 1
                    """,
                    (identifier,),
                ).fetchone()
        if row is None:
            raise ReleaseNotFound(
                f"no eligible release for dataset_id={identifier!r}, snapshot_id={normalized_snapshot!r}, as_of={normalized_as_of!r}"
            )
        release = self._row_to_release(row)
        if normalized_as_of is not None and release.available_at > normalized_as_of:
            raise ReleaseNotFound("explicit snapshot was not available at as_of")
        requested_level = PURPOSE_LEVELS[normalized_purpose]
        ceiling_level = PROMOTION_LEVELS.get(release.promotion_ceiling, -1)
        if requested_level > ceiling_level:
            raise PromotionDenied(
                f"dataset release ceiling={release.promotion_ceiling} denies purpose={normalized_purpose}"
            )
        if normalized_purpose == "live" and not release.pit_verified:
            raise PromotionDenied("live purpose requires pit_verified=true")
        self.verify_release(release, verify_content=True)
        return release

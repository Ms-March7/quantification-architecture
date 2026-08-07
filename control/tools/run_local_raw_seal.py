#!/usr/bin/env python3
"""Seal explicitly named local probe raw files below ``tmp/``.

This is an inspection-only bridge between an already-landed probe response and
later normalization work.  It never discovers inputs, performs network access,
mutates a source file, writes the formal catalog, or grants research use.

Each ``--input-spec`` is a four-part, hash-bound allowlist entry::

    SOURCE_ID|workspace/relative/path|sha256|bytes

The output is a write-once, immutable-style evidence directory below
``tmp/local-raw-seal/``.  "Immutable-style" means no overwrite, byte-for-byte
sealed copies, a canonical manifest, and a manifest digest; it is not a formal
immutable market-data snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
ENGINE_ID = "local_raw_seal/v1"
CONTRACT_ID = "local_raw_seal_v1"
CONTRACT_PATH = (
    ROOT / "data" / "catalog" / "contracts" / "local_raw_seal_v1" / "contract.json"
)
INPUT_ROOT = ROOT / "tmp" / "free-web-daily-probe"
OUTPUT_ROOT = ROOT / "tmp" / "local-raw-seal"
PURPOSE_CEILING = "INSPECTION_ONLY"
MAX_INPUTS = 20
SOURCE_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RawSealError(RuntimeError):
    """Fail-closed error for invalid requests, paths, or bytes."""


@dataclass(frozen=True)
class InputSpec:
    source_id: str
    relative_path: str
    expected_sha256: str
    expected_bytes: int


@dataclass(frozen=True)
class VerifiedInput:
    spec: InputSpec
    source_path: Path
    actual_sha256: str
    actual_bytes: int
    mtime_ns: int
    device: int
    inode: int


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _assert_existing_chain_is_plain(base: Path, target: Path) -> None:
    try:
        relative = target.relative_to(base)
    except ValueError as exc:
        raise RawSealError("path escapes its allowed root") from exc
    current = base
    if current.exists() and _is_reparse_or_symlink(current):
        raise RawSealError(f"reparse/symlink path component is forbidden: {current}")
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_reparse_or_symlink(current):
            raise RawSealError(f"reparse/symlink path component is forbidden: {current}")


def validate_run_id(value: str, field: str) -> str:
    if not RUN_ID_RE.fullmatch(value):
        raise RawSealError(f"{field} has an invalid format")
    return value


def parse_input_specs(values: Iterable[str]) -> list[InputSpec]:
    specs: list[InputSpec] = []
    source_ids: set[str] = set()
    paths: set[str] = set()
    for value in values:
        parts = value.split("|")
        if len(parts) != 4:
            raise RawSealError(
                "input spec must be SOURCE_ID|relative_path|sha256|bytes"
            )
        source_id, relative_path, expected_sha256, expected_bytes_raw = parts
        if not SOURCE_ID_RE.fullmatch(source_id):
            raise RawSealError(f"invalid source_id: {source_id!r}")
        relative = Path(relative_path)
        if (
            not relative_path
            or relative.is_absolute()
            or relative.drive
            or ".." in relative.parts
        ):
            raise RawSealError("input path must be a safe workspace-relative path")
        normalized = relative.as_posix()
        if not SHA256_RE.fullmatch(expected_sha256):
            raise RawSealError("expected SHA-256 must be 64 lowercase hex characters")
        try:
            expected_bytes = int(expected_bytes_raw)
        except ValueError as exc:
            raise RawSealError("expected bytes must be an integer") from exc
        if expected_bytes < 0:
            raise RawSealError("expected bytes must be nonnegative")
        if source_id in source_ids:
            raise RawSealError(f"duplicate source_id: {source_id}")
        if normalized in paths:
            raise RawSealError(f"duplicate input path: {normalized}")
        source_ids.add(source_id)
        paths.add(normalized)
        specs.append(
            InputSpec(
                source_id=source_id,
                relative_path=normalized,
                expected_sha256=expected_sha256,
                expected_bytes=expected_bytes,
            )
        )
    if not specs:
        raise RawSealError("at least one explicit input spec is required")
    if len(specs) > MAX_INPUTS:
        raise RawSealError(f"at most {MAX_INPUTS} explicit inputs are allowed")
    return specs


def validate_output_path(path: Path, seal_run_id: str) -> Path:
    lexical = Path(os.path.abspath(path))
    allowed = Path(os.path.abspath(OUTPUT_ROOT))
    if lexical.parent != allowed or lexical.name != seal_run_id:
        raise RawSealError(
            "output must be exactly tmp/local-raw-seal/<seal_run_id>"
        )
    _assert_existing_chain_is_plain(allowed, lexical.parent)
    if lexical.exists():
        raise RawSealError("output already exists; sealed runs are never overwritten")
    return lexical


def _load_contract() -> tuple[dict[str, Any], str]:
    payload = CONTRACT_PATH.read_bytes()
    try:
        contract = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RawSealError("local raw seal contract is unreadable") from exc
    if contract.get("contract_id") != CONTRACT_ID:
        raise RawSealError("local raw seal contract identity mismatch")
    if contract.get("purpose_ceiling") != PURPOSE_CEILING:
        raise RawSealError("local raw seal contract purpose ceiling mismatch")
    return contract, sha256_bytes(payload)


def _verify_input(spec: InputSpec, source_probe_run_id: str) -> VerifiedInput:
    allowed = Path(os.path.abspath(INPUT_ROOT))
    lexical = Path(os.path.abspath(ROOT / Path(spec.relative_path)))
    try:
        relative = lexical.relative_to(allowed)
    except ValueError as exc:
        raise RawSealError("input must stay below tmp/free-web-daily-probe") from exc
    if len(relative.parts) != 3:
        raise RawSealError(
            "input must be a direct file in tmp/free-web-daily-probe/<run>/raw/"
        )
    if relative.parts[0] != source_probe_run_id or relative.parts[1] != "raw":
        raise RawSealError("input path does not match source_probe_run_id/raw")
    _assert_existing_chain_is_plain(allowed, lexical)
    if not lexical.exists() or not lexical.is_file():
        raise RawSealError(f"input is not an ordinary file: {spec.relative_path}")
    info = lexical.stat()
    actual_bytes = info.st_size
    if actual_bytes != spec.expected_bytes:
        raise RawSealError(
            f"byte-count mismatch for {spec.source_id}: "
            f"expected {spec.expected_bytes}, got {actual_bytes}"
        )
    actual_sha256 = sha256_file(lexical)
    if actual_sha256 != spec.expected_sha256:
        raise RawSealError(
            f"SHA-256 mismatch for {spec.source_id}: "
            f"expected {spec.expected_sha256}, got {actual_sha256}"
        )
    return VerifiedInput(
        spec=spec,
        source_path=lexical,
        actual_sha256=actual_sha256,
        actual_bytes=actual_bytes,
        mtime_ns=info.st_mtime_ns,
        device=info.st_dev,
        inode=info.st_ino,
    )


def prepare(
    seal_run_id: str,
    source_probe_run_id: str,
    input_specs: Iterable[str],
    output: Path,
) -> dict[str, Any]:
    seal_run_id = validate_run_id(seal_run_id, "seal_run_id")
    source_probe_run_id = validate_run_id(
        source_probe_run_id, "source_probe_run_id"
    )
    specs = parse_input_specs(input_specs)
    verified = [_verify_input(spec, source_probe_run_id) for spec in specs]
    output_path = validate_output_path(output, seal_run_id)
    contract, contract_sha256 = _load_contract()
    return {
        "seal_run_id": seal_run_id,
        "source_probe_run_id": source_probe_run_id,
        "verified": verified,
        "output_path": output_path,
        "contract": contract,
        "contract_sha256": contract_sha256,
    }


def boundary(status: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine_id": ENGINE_ID,
        "contract_id": CONTRACT_ID,
        "status": status,
        "purpose_ceiling": PURPOSE_CEILING,
        "network_used": False,
        "input_discovery_used": False,
        "source_files_modified": False,
        "source_files_deleted": False,
        "formal_source_admission": False,
        "formal_catalog_written": False,
        "quality_promotion": False,
        "production_raw_store_written": False,
        "scheduler_created": False,
        "research_ready": False,
        "formal_effect": "NONE",
    }


def _copy_verified(source: VerifiedInput, target: Path) -> tuple[int, str]:
    before = source.source_path.stat()
    digest = hashlib.sha256()
    copied = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.source_path.open("rb") as reader, target.open("xb") as writer:
        opened = os.fstat(reader.fileno())
        if (
            opened.st_size != source.actual_bytes
            or opened.st_mtime_ns != source.mtime_ns
            or opened.st_dev != source.device
            or opened.st_ino != source.inode
        ):
            raise RawSealError(f"input changed before copy: {source.spec.source_id}")
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            writer.write(chunk)
            digest.update(chunk)
            copied += len(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    after = source.source_path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
    ):
        raise RawSealError(f"input changed during copy: {source.spec.source_id}")
    copied_hash = digest.hexdigest()
    if copied != source.actual_bytes or copied_hash != source.actual_sha256:
        raise RawSealError(f"copied bytes changed for {source.spec.source_id}")
    if target.stat().st_size != copied or sha256_file(target) != copied_hash:
        raise RawSealError(f"sealed copy verification failed for {source.spec.source_id}")
    return copied, copied_hash


def _cleanup_owned_staging(stage: Path) -> None:
    allowed = Path(os.path.abspath(OUTPUT_ROOT))
    lexical = Path(os.path.abspath(stage))
    if lexical.parent != allowed or not lexical.name.startswith("."):
        raise RawSealError("refusing to clean a staging path outside the owned root")
    if lexical.exists():
        shutil.rmtree(lexical)


def execute(prepared: dict[str, Any]) -> dict[str, Any]:
    output_path: Path = prepared["output_path"]
    if output_path.exists():
        raise RawSealError("output already exists; sealed runs are never overwritten")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    _assert_existing_chain_is_plain(OUTPUT_ROOT, OUTPUT_ROOT)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{prepared['seal_run_id']}.staging-", dir=OUTPUT_ROOT
        )
    )
    try:
        entries: list[dict[str, Any]] = []
        for index, verified in enumerate(prepared["verified"], start=1):
            sealed_relative = (
                Path("sealed") / f"{index:02d}-{verified.spec.source_id}.raw"
            )
            target = stage / sealed_relative
            copied_bytes, copied_sha256 = _copy_verified(verified, target)
            entries.append(
                {
                    "source_id": verified.spec.source_id,
                    "source_relative_path": verified.spec.relative_path,
                    "source_bytes": verified.actual_bytes,
                    "source_sha256": verified.actual_sha256,
                    "sealed_relative_path": sealed_relative.as_posix(),
                    "sealed_bytes": copied_bytes,
                    "sealed_sha256": copied_sha256,
                    "byte_identity_verified": True,
                }
            )

        manifest = {
            **boundary("PASS_LOCAL_RAW_SEAL_INSPECTION_ONLY"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "seal_run_id": prepared["seal_run_id"],
            "source_probe_run_id": prepared["source_probe_run_id"],
            "contract_relative_path": CONTRACT_PATH.relative_to(ROOT).as_posix(),
            "contract_sha256": prepared["contract_sha256"],
            "source_input_mode": "EXPLICIT_HASH_AND_BYTE_ALLOWLIST_ONLY",
            "input_count": len(entries),
            "inputs": entries,
            "immutability_guards": [
                "FINAL_OUTPUT_MUST_NOT_EXIST",
                "SAME_VOLUME_STAGING_THEN_RENAME",
                "SOURCE_AND_COPY_SHA256_EQUALITY",
                "SOURCE_AND_COPY_BYTE_COUNT_EQUALITY",
                "CANONICAL_MANIFEST_WITH_SEPARATE_DIGEST",
            ],
            "known_limitations": [
                "TMP_INSPECTION_STORE_NOT_FORMAL_IMMUTABLE_MARKET_STORE",
                "SOURCE_RIGHTS_AND_RETENTION_NOT_DECIDED_BY_THIS_SEAL",
                "NO_SOURCE_SEMANTICS_OR_PIT_CLOSURE",
                "NO_CATALOG_OR_RESEARCH_GRANT",
            ],
        }
        manifest_payload = (canonical_json(manifest) + "\n").encode("utf-8")
        manifest_path = stage / "SEAL_MANIFEST.json"
        manifest_path.write_bytes(manifest_payload)
        manifest_sha256 = sha256_bytes(manifest_payload)
        (stage / "SEAL_MANIFEST.sha256").write_text(
            f"{manifest_sha256}  SEAL_MANIFEST.json\n", encoding="ascii"
        )

        # Verify every output by exact known path.  No directory discovery is
        # used to select source inputs or to decide what was sealed.
        if sha256_file(manifest_path) != manifest_sha256:
            raise RawSealError("manifest digest verification failed")
        for entry in entries:
            sealed_path = stage / entry["sealed_relative_path"]
            if (
                sealed_path.stat().st_size != entry["sealed_bytes"]
                or sha256_file(sealed_path) != entry["sealed_sha256"]
            ):
                raise RawSealError("post-manifest sealed copy verification failed")

        if output_path.exists():
            raise RawSealError("output appeared during sealing; refusing overwrite")
        os.rename(stage, output_path)
        result = {
            **boundary("PASS_LOCAL_RAW_SEAL_INSPECTION_ONLY"),
            "seal_run_id": prepared["seal_run_id"],
            "source_probe_run_id": prepared["source_probe_run_id"],
            "output_relative_path": output_path.relative_to(ROOT).as_posix(),
            "input_count": len(entries),
            "sealed_bytes": sum(entry["sealed_bytes"] for entry in entries),
            "seal_manifest_sha256": manifest_sha256,
        }
        return result
    except Exception:
        _cleanup_owned_staging(stage)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seal-run-id", required=True)
    parser.add_argument("--source-probe-run-id", required=True)
    parser.add_argument(
        "--input-spec",
        action="append",
        required=True,
        help="SOURCE_ID|workspace-relative-path|lowercase-sha256|bytes",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        prepared = prepare(
            args.seal_run_id,
            args.source_probe_run_id,
            args.input_spec,
            args.output,
        )
        if not args.execute:
            plan = {
                **boundary("PLAN_HASH_BOUND_LOCAL_RAW_SEAL"),
                "seal_run_id": prepared["seal_run_id"],
                "source_probe_run_id": prepared["source_probe_run_id"],
                "output_relative_path": prepared["output_path"]
                .relative_to(ROOT)
                .as_posix(),
                "input_count": len(prepared["verified"]),
                "inputs": [
                    {
                        "source_id": item.spec.source_id,
                        "source_relative_path": item.spec.relative_path,
                        "source_bytes": item.actual_bytes,
                        "source_sha256": item.actual_sha256,
                    }
                    for item in prepared["verified"]
                ],
                "write_performed": False,
            }
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        result = execute(prepared)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    **boundary("FAIL_CLOSED"),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=os.sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

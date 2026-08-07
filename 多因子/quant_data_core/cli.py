from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import Any

from .catalog import DatasetCatalog, InvalidRelease, ReleaseSpec, strict_json_loads
from .github_ingest import GitHubIngestRequest, ingest_staged_github_release


MAX_JSON_FILE_BYTES = 16 * 1024 * 1024


def _read_bounded_file(
    value: str | Path,
    *,
    label: str,
    max_bytes: int = MAX_JSON_FILE_BYTES,
) -> bytes:
    path = Path(os.path.abspath(os.fspath(value)))
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise InvalidRelease("max_bytes must be a positive integer")
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise InvalidRelease(f"{label} is missing: {path}") from exc
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise InvalidRelease(f"{label} must be a regular non-link file: {path}")
    if before.st_size > max_bytes:
        raise InvalidRelease(f"{label} exceeds {max_bytes} bytes: {path}")
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
                raise InvalidRelease(f"{label} changed before it was opened: {path}")
            payload = handle.read(max_bytes + 1)
            finished = os.fstat(handle.fileno())
        after = path.lstat()
    except FileNotFoundError as exc:
        raise InvalidRelease(f"{label} disappeared while being read: {path}") from exc
    if len(payload) > max_bytes:
        raise InvalidRelease(f"{label} exceeds {max_bytes} bytes: {path}")
    if (
        finished.st_size != opened.st_size
        or finished.st_mtime_ns != opened.st_mtime_ns
        or not os.path.samestat(before, after)
    ):
        raise InvalidRelease(f"{label} changed while it was being read: {path}")
    return payload


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Immutable quant dataset catalog")
    parser.add_argument("--catalog", required=True, help="SQLite catalog path")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="initialize an empty catalog")

    register = commands.add_parser("register", help="hash and register an immutable release")
    register.add_argument("--dataset-id", required=True)
    register.add_argument("--snapshot-id", required=True)
    register.add_argument("--source", required=True)
    register.add_argument("--source-tier", required=True)
    register.add_argument("--frequency", required=True)
    register.add_argument("--universe", required=True)
    coverage = register.add_mutually_exclusive_group(required=True)
    coverage.add_argument("--coverage-json")
    coverage.add_argument("--coverage-file")
    register.add_argument("--schema-version", required=True)
    register.add_argument("--license", required=True)
    register.add_argument("--published-at", required=True)
    register.add_argument("--available-at", required=True)
    register.add_argument("--ingested-at", required=True)
    register.add_argument("--revised-at")
    register.add_argument("--pit-verified", action="store_true")
    register.add_argument("--promotion-ceiling", required=True)
    register.add_argument("--root", required=True)
    register.add_argument("--provenance")
    register.add_argument("--expected-content-hash")

    ingest = commands.add_parser(
        "ingest-github-staged",
        help="validate a saved GitHub API response and local tar asset, then publish atomically",
    )
    ingest.add_argument("--owner", required=True)
    ingest.add_argument("--repository", required=True)
    ingest.add_argument("--metadata-file", required=True)
    ingest.add_argument("--asset-file", required=True)
    ingest.add_argument("--asset-name", required=True)
    ingest.add_argument("--publish-root", required=True)
    ingest.add_argument("--dataset-id", required=True)
    ingest.add_argument("--snapshot-id", required=True)
    ingest.add_argument("--expected-tag")
    ingest.add_argument("--expected-asset-bytes", type=int)
    ingest.add_argument("--expected-publisher-sha256")
    ingest.add_argument("--expected-calendar-end", required=True)
    ingest.add_argument("--source-tier", required=True)
    ingest.add_argument("--frequency", required=True)
    ingest.add_argument("--universe", required=True)
    ingest_coverage = ingest.add_mutually_exclusive_group(required=True)
    ingest_coverage.add_argument("--coverage-json")
    ingest_coverage.add_argument("--coverage-file")
    ingest.add_argument("--schema-version", required=True)
    ingest.add_argument("--license", required=True)
    ingest.add_argument("--ingested-at")
    ingest.add_argument("--revised-at")
    ingest.add_argument("--pit-verified", action="store_true")
    ingest.add_argument("--promotion-ceiling", default="RESEARCH_ONLY")
    ingest.add_argument("--max-asset-bytes", type=int, default=20 * 1024**3)
    ingest.add_argument("--max-members", type=int, default=1_000_000)
    ingest.add_argument("--max-expanded-bytes", type=int, default=200 * 1024**3)
    ingest.add_argument("--max-qlib-search-depth", type=int, default=3)

    resolve = commands.add_parser("resolve", help="strictly resolve and verify one release")
    resolve.add_argument("--dataset-id", required=True)
    resolve.add_argument("--purpose", required=True, choices=("engineering", "research", "shadow", "live"))
    resolve.add_argument("--as-of")
    resolve.add_argument("--snapshot-id")

    verify = commands.add_parser("verify", help="verify one explicit release")
    verify.add_argument("--dataset-id", required=True)
    verify.add_argument("--snapshot-id", required=True)

    listing = commands.add_parser("list", help="list catalog releases without resolving latest")
    listing.add_argument("--dataset-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    catalog = DatasetCatalog(args.catalog)
    if args.command == "init":
        _emit({"status": "CATALOG_READY", "catalog": str(Path(args.catalog).resolve())})
        return 0
    if args.command == "register":
        coverage_raw = (
            _read_bounded_file(args.coverage_file, label="coverage file")
            if args.coverage_file is not None
            else args.coverage_json
        )
        coverage = strict_json_loads(coverage_raw)
        release = catalog.register_release(
            ReleaseSpec(
                dataset_id=args.dataset_id,
                snapshot_id=args.snapshot_id,
                source=args.source,
                source_tier=args.source_tier,
                frequency=args.frequency,
                universe=args.universe,
                coverage=coverage,
                schema_version=args.schema_version,
                license=args.license,
                published_at=args.published_at,
                available_at=args.available_at,
                ingested_at=args.ingested_at,
                revised_at=args.revised_at,
                pit_verified=args.pit_verified,
                promotion_ceiling=args.promotion_ceiling,
                root_path=args.root,
            ),
            provenance_path=args.provenance,
            expected_content_hash=args.expected_content_hash,
        )
        _emit({"status": "REGISTERED", "release": release.to_dict()})
        return 0
    if args.command == "ingest-github-staged":
        coverage_raw = (
            _read_bounded_file(args.coverage_file, label="coverage file")
            if args.coverage_file is not None
            else args.coverage_json
        )
        release = ingest_staged_github_release(
            catalog,
            GitHubIngestRequest(
                owner=args.owner,
                repository=args.repository,
                metadata=_read_bounded_file(
                    args.metadata_file, label="GitHub metadata file"
                ),
                asset_name=args.asset_name,
                staged_archive=args.asset_file,
                publish_root=args.publish_root,
                dataset_id=args.dataset_id,
                snapshot_id=args.snapshot_id,
                expected_tag=args.expected_tag,
                expected_asset_bytes=args.expected_asset_bytes,
                expected_publisher_sha256=args.expected_publisher_sha256,
                expected_calendar_end=args.expected_calendar_end,
                source_tier=args.source_tier,
                frequency=args.frequency,
                universe=args.universe,
                coverage=strict_json_loads(coverage_raw),
                schema_version=args.schema_version,
                license=args.license,
                ingested_at=args.ingested_at,
                revised_at=args.revised_at,
                pit_verified=args.pit_verified,
                promotion_ceiling=args.promotion_ceiling,
                max_asset_bytes=args.max_asset_bytes,
                max_members=args.max_members,
                max_expanded_bytes=args.max_expanded_bytes,
                max_qlib_search_depth=args.max_qlib_search_depth,
            ),
        )
        _emit({"status": "GITHUB_STAGED_RELEASE_INGESTED", "release": release.to_dict()})
        return 0
    if args.command == "resolve":
        release = catalog.resolve(
            args.dataset_id,
            args.purpose,
            as_of=args.as_of,
            snapshot_id=args.snapshot_id,
        )
        _emit({"status": "RESOLVED_AND_VERIFIED", "release": release.to_dict()})
        return 0
    if args.command == "verify":
        release = catalog.resolve(
            args.dataset_id,
            "engineering",
            snapshot_id=args.snapshot_id,
        )
        _emit({"status": "VERIFIED", "release": release.to_dict()})
        return 0
    releases = [release.to_dict() for release in catalog.list_releases(args.dataset_id)]
    _emit({"status": "CATALOG_LIST", "count": len(releases), "releases": releases})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

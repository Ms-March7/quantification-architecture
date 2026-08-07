from __future__ import annotations

import hashlib
import io
import shutil
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from quant_data_core.catalog import (
    CatalogError,
    DatasetCatalog,
    IntegrityError,
    InvalidRelease,
    ReleaseSpec,
)
from quant_data_core import github_ingest
from quant_data_core.cli import _read_bounded_file
from quant_data_core.github_ingest import (
    GitHubIngestRequest,
    GitHubMetadataError,
    QlibValidationError,
    StagedAssetError,
    UnsafeArchiveError,
    ingest_staged_github_release,
    parse_github_release_metadata,
    safely_extract_tar,
)


OWNER = "example-owner"
REPOSITORY = "a-share-data"
SNAPSHOT = "2026-07-14"
ASSET_NAME = "qlib-cn-2026-07-14.tar.gz"


class GitHubIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.catalog = DatasetCatalog(self.base / "catalog" / "catalog.sqlite3")
        self.publish_root = self.base / "published"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_archive(
        self,
        *,
        calendar_end: str = SNAPSHOT,
        malicious: tuple[str, bytes, str] | None = None,
        explicit_directories: bool = False,
    ) -> Path:
        archive_path = self.base / ASSET_NAME
        with tarfile.open(archive_path, "w:gz") as archive:
            if explicit_directories:
                for name in (
                    "./bundle/",
                    "./bundle/qlib_bin/",
                    "./bundle/qlib_bin/calendars/",
                    "./bundle/qlib_bin/instruments/",
                    "./bundle/qlib_bin/features/",
                    "./bundle/qlib_bin/features/sh600000/",
                ):
                    info = tarfile.TarInfo(name)
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
            members = {
                "./bundle/qlib_bin/calendars/day.txt": (
                    f"2026-07-11\n{calendar_end}\n".encode("utf-8")
                ),
                "./bundle/qlib_bin/instruments/all.txt": (
                    b"sh600000\t2026-07-11\t2026-07-14\n"
                ),
                "./bundle/qlib_bin/features/sh600000/close.day.bin": b"opaque-qlib-bytes",
            }
            for name, value in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
            if malicious is not None:
                name, value, kind = malicious
                info = tarfile.TarInfo(name)
                if kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "bundle/qlib_bin/calendars/day.txt"
                    archive.addfile(info)
                else:
                    info.size = len(value)
                    archive.addfile(info, io.BytesIO(value))
        return archive_path

    def make_metadata(
        self,
        archive_path: Path,
        *,
        digest: str | None = None,
        size: int | None = None,
        draft: bool = False,
        prerelease: bool = False,
    ) -> dict:
        observed_digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        return {
            "id": 41,
            "tag_name": SNAPSHOT,
            "draft": draft,
            "prerelease": prerelease,
            "published_at": "2026-07-15T00:30:00Z",
            "url": f"https://api.github.com/repos/{OWNER}/{REPOSITORY}/releases/41",
            "html_url": f"https://github.com/{OWNER}/{REPOSITORY}/releases/tag/{SNAPSHOT}",
            "assets": [
                {
                    "id": 73,
                    "name": ASSET_NAME,
                    "size": archive_path.stat().st_size if size is None else size,
                    "digest": f"sha256:{digest or observed_digest}",
                    "state": "uploaded",
                    "url": (
                        f"https://api.github.com/repos/{OWNER}/{REPOSITORY}/"
                        "releases/assets/73"
                    ),
                    "browser_download_url": (
                        f"https://github.com/{OWNER}/{REPOSITORY}/releases/download/"
                        f"{SNAPSHOT}/{ASSET_NAME}"
                    ),
                }
            ],
        }

    def make_request(self, archive: Path, metadata: dict) -> GitHubIngestRequest:
        return GitHubIngestRequest(
            owner=OWNER,
            repository=REPOSITORY,
            metadata=metadata,
            asset_name=ASSET_NAME,
            staged_archive=archive,
            publish_root=self.publish_root,
            dataset_id="cn-a-share-qlib-daily",
            snapshot_id=SNAPSHOT,
            expected_calendar_end=SNAPSHOT,
            source_tier="VERIFIED_SECONDARY",
            frequency="1d",
            universe="SH,SZ",
            coverage={"start": "2026-07-11", "end": SNAPSHOT, "symbols": 1},
            schema_version="qlib-bin-v1",
            license="fixture-license",
            ingested_at="2026-07-19T00:00:00Z",
            pit_verified=False,
            promotion_ceiling="RESEARCH_ONLY",
            max_asset_bytes=10 * 1024 * 1024,
            max_members=100,
            max_expanded_bytes=10 * 1024 * 1024,
        )

    def assert_no_publication(self) -> None:
        self.assertEqual(self.catalog.list_releases(), [])
        self.assertFalse(
            (self.publish_root / "cn-a-share-qlib-daily" / SNAPSHOT).exists()
        )
        manifests = list(self.catalog.release_root.rglob("manifest.json"))
        self.assertEqual(manifests, [])
        self.assertEqual(list(self.publish_root.rglob("*.ingest-*")), [])
        self.assertEqual(list(self.catalog.release_root.rglob("*.lock")), [])

    def test_valid_fake_release_is_atomically_published_and_registered(self) -> None:
        archive = self.make_archive(explicit_directories=True)
        metadata = self.make_metadata(archive)
        release = ingest_staged_github_release(
            self.catalog, self.make_request(archive, metadata)
        )
        final = self.publish_root / "cn-a-share-qlib-daily" / SNAPSHOT
        self.assertEqual(Path(release.root_path), final / "qlib_bin")
        self.assertTrue((final / "PROVENANCE.json").is_file())
        self.assertEqual(len(self.catalog.list_releases()), 1)
        resolved = self.catalog.resolve(
            "cn-a-share-qlib-daily", "research", snapshot_id=SNAPSHOT
        )
        self.assertEqual(resolved.content_hash, release.content_hash)
        provenance = (final / "PROVENANCE.json").read_text(encoding="utf-8")
        self.assertIn(f'"publisher_digest":"sha256:{metadata["assets"][0]["digest"][7:]}"', provenance)
        self.assertIn('"hash_verified":true', provenance)

    def test_catalog_registration_failure_rolls_back_published_directory(self) -> None:
        archive = self.make_archive()
        metadata = self.make_metadata(archive)
        with patch.object(
            self.catalog, "register_release", side_effect=RuntimeError("injected failure")
        ), self.assertRaisesRegex(RuntimeError, "injected failure"):
            ingest_staged_github_release(
                self.catalog, self.make_request(archive, metadata)
            )
        self.assert_no_publication()

    def test_size_or_digest_mismatch_never_publishes_or_registers(self) -> None:
        archive = self.make_archive()
        bad_size = self.make_metadata(archive, size=archive.stat().st_size + 1)
        with self.assertRaises(StagedAssetError):
            ingest_staged_github_release(
                self.catalog, self.make_request(archive, bad_size)
            )
        self.assert_no_publication()
        bad_digest = self.make_metadata(archive, digest="0" * 64)
        with self.assertRaises(StagedAssetError):
            ingest_staged_github_release(
                self.catalog, self.make_request(archive, bad_digest)
            )
        self.assert_no_publication()

    def test_out_of_band_size_and_digest_pins_must_match_api_metadata(self) -> None:
        archive = self.make_archive()
        metadata = self.make_metadata(archive)
        request = self.make_request(archive, metadata)
        bad_size_pin = GitHubIngestRequest(
            **{**request.__dict__, "expected_asset_bytes": archive.stat().st_size + 1}
        )
        with self.assertRaises(GitHubMetadataError):
            ingest_staged_github_release(self.catalog, bad_size_pin)
        self.assert_no_publication()
        bad_digest_pin = GitHubIngestRequest(
            **{**request.__dict__, "expected_publisher_sha256": "f" * 64}
        )
        with self.assertRaises(GitHubMetadataError):
            ingest_staged_github_release(self.catalog, bad_digest_pin)
        self.assert_no_publication()

    def test_path_traversal_absolute_path_and_link_are_rejected(self) -> None:
        for malicious in (
            ("../escape.txt", b"escape", "file"),
            ("/absolute.txt", b"escape", "file"),
            ("bundle/qlib_bin/link", b"", "symlink"),
        ):
            with self.subTest(member=malicious[0]):
                archive = self.make_archive(malicious=malicious)
                metadata = self.make_metadata(archive)
                with self.assertRaises(UnsafeArchiveError):
                    ingest_staged_github_release(
                        self.catalog, self.make_request(archive, metadata)
                    )
                self.assert_no_publication()
                self.assertFalse((self.base / "escape.txt").exists())

    def test_windows_reserved_long_and_case_colliding_paths_are_rejected(self) -> None:
        unsafe_names = (
            "bundle/qlib_bin/features/CONIN$/close.day.bin",
            "bundle/qlib_bin/features/COM¹/close.day.bin",
            f"bundle/qlib_bin/features/{'a' * 256}/close.day.bin",
            "bundle/QLIB_BIN/calendars/day.txt",
        )
        for name in unsafe_names:
            with self.subTest(member=name):
                archive = self.make_archive(malicious=(name, b"unsafe", "file"))
                metadata = self.make_metadata(archive)
                with self.assertRaises(UnsafeArchiveError):
                    ingest_staged_github_release(
                        self.catalog, self.make_request(archive, metadata)
                    )
                self.assert_no_publication()

    def test_hardlinks_devices_fifos_and_sparse_entries_are_rejected(self) -> None:
        special_types = (
            (tarfile.LNKTYPE, "hardlink"),
            (tarfile.CHRTYPE, "character-device"),
            (tarfile.FIFOTYPE, "fifo"),
            (tarfile.GNUTYPE_SPARSE, "sparse"),
        )
        for type_code, label in special_types:
            with self.subTest(kind=label):
                archive_path = self.base / f"{label}.tar"
                with tarfile.open(archive_path, "w") as archive:
                    info = tarfile.TarInfo(f"unsafe-{label}")
                    info.type = type_code
                    if type_code == tarfile.LNKTYPE:
                        info.linkname = "outside"
                    archive.addfile(info)
                with self.assertRaises(UnsafeArchiveError):
                    safely_extract_tar(
                        archive_path,
                        self.base / f"out-{label}",
                        max_members=10,
                        max_expanded_bytes=1024,
                    )

    def test_skipped_root_entries_still_count_and_sized_directories_are_rejected(self) -> None:
        root_bomb = self.base / "root-bomb.tar"
        with tarfile.open(root_bomb, "w") as archive:
            for _ in range(3):
                info = tarfile.TarInfo("./")
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
        with self.assertRaisesRegex(UnsafeArchiveError, "member limit"):
            safely_extract_tar(
                root_bomb,
                self.base / "root-bomb-out",
                max_members=2,
                max_expanded_bytes=1,
            )

        sized_directory = self.base / "sized-directory.tar"
        with tarfile.open(sized_directory, "w") as archive:
            info = tarfile.TarInfo("padding/")
            info.type = tarfile.DIRTYPE
            info.size = 1024
            archive.addfile(info, io.BytesIO(b"x" * info.size))
        with self.assertRaisesRegex(UnsafeArchiveError, "non-zero size"):
            safely_extract_tar(
                sized_directory,
                self.base / "sized-directory-out",
                max_members=10,
                max_expanded_bytes=1,
            )

    def test_oversized_pax_metadata_is_rejected_before_stdlib_allocation(self) -> None:
        archive_path = self.base / "pax-metadata-bomb.tar"
        with tarfile.open(archive_path, "w", format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo("safe.bin")
            info.size = 0
            info.pax_headers = {
                "comment": "x" * (github_ingest.MAX_TAR_EXTENDED_HEADER_BYTES + 1)
            }
            archive.addfile(info)
        with self.assertRaises(UnsafeArchiveError):
            safely_extract_tar(
                archive_path,
                self.base / "pax-metadata-bomb-out",
                max_members=10,
                max_expanded_bytes=1,
            )

    def test_hidden_pax_helper_headers_count_toward_member_limit(self) -> None:
        archive_path = self.base / "pax-helper-count.tar"
        with tarfile.open(archive_path, "w", format=tarfile.PAX_FORMAT) as archive:
            for index in range(3):
                info = tarfile.TarInfo(f"safe-{index}.bin")
                info.size = 0
                info.pax_headers = {"comment": f"entry-{index}"}
                archive.addfile(info)
        with self.assertRaisesRegex(UnsafeArchiveError, "member limit"):
            safely_extract_tar(
                archive_path,
                self.base / "pax-helper-count-out",
                max_members=5,
                max_expanded_bytes=1,
            )

    def test_large_preflight_path_conflict_check_is_near_linear(self) -> None:
        archive_path = self.base / "large-preflight.tar"
        member_count = 12_000
        with tarfile.open(archive_path, "w") as archive:
            for index in range(member_count):
                info = tarfile.TarInfo(f"tree/member-{index:05d}.bin")
                info.size = 0
                archive.addfile(info)
            replacement = tarfile.TarInfo("tree")
            replacement.size = 0
            archive.addfile(replacement)

        started = time.perf_counter()
        with self.assertRaisesRegex(UnsafeArchiveError, "replaces a directory"):
            safely_extract_tar(
                archive_path,
                self.base / "large-preflight-out",
                max_members=member_count + 1,
                max_expanded_bytes=1,
            )
        elapsed = time.perf_counter() - started
        self.assertLess(
            elapsed,
            8.0,
            f"12k-member structural preflight took {elapsed:.3f}s; possible O(n^2) regression",
        )

    def test_extraction_uses_private_verified_copy_after_source_path_changes(self) -> None:
        archive = self.make_archive()
        metadata = self.make_metadata(archive)
        real_copy = github_ingest._copy_verified_staged_asset

        def copy_then_replace(*args, **kwargs):
            digest = real_copy(*args, **kwargs)
            archive.write_bytes(b"replaced-after-verified-copy")
            return digest

        with patch.object(
            github_ingest,
            "_copy_verified_staged_asset",
            side_effect=copy_then_replace,
        ):
            release = ingest_staged_github_release(
                self.catalog, self.make_request(archive, metadata)
            )
        self.assertTrue(Path(release.root_path).is_dir())
        self.catalog.verify_release(release)

    def test_catalog_scoped_lock_blocks_same_identity_across_publish_roots(self) -> None:
        archive = self.make_archive()
        metadata = self.make_metadata(archive)
        request = self.make_request(archive, metadata)
        competing = GitHubIngestRequest(
            **{**request.__dict__, "publish_root": self.base / "other-published"}
        )
        real_register = self.catalog.register_release

        def register_while_competing(*args, **kwargs):
            with self.assertRaisesRegex(CatalogError, "catalog snapshot lock"):
                ingest_staged_github_release(self.catalog, competing)
            return real_register(*args, **kwargs)

        with patch.object(
            self.catalog,
            "register_release",
            side_effect=register_while_competing,
        ):
            release = ingest_staged_github_release(self.catalog, request)
        self.assertTrue(Path(release.root_path).is_dir())
        self.assertFalse(
            (self.base / "other-published" / request.dataset_id / request.snapshot_id).exists()
        )
        self.assertEqual(len(self.catalog.list_releases()), 1)

    def test_competing_catalog_winner_does_not_leave_losing_publish_orphan(self) -> None:
        archive = self.make_archive()
        metadata = self.make_metadata(archive)
        request = self.make_request(archive, metadata)
        real_register = self.catalog.register_release
        competitor_release_root = self.base / "competitor-release"
        competitor_qlib = competitor_release_root / "qlib_bin"
        competitor_provenance = competitor_release_root / "PROVENANCE.json"

        def register_competitor_then_fail(spec, *, provenance_path=None, **kwargs):
            shutil.copytree(Path(spec.root_path), competitor_qlib)
            competitor_release_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(provenance_path), competitor_provenance)
            competitor_spec = ReleaseSpec(
                **{**spec.__dict__, "root_path": competitor_qlib}
            )
            real_register(
                competitor_spec,
                provenance_path=competitor_provenance,
            )
            raise RuntimeError("competitor committed first")

        with patch.object(
            self.catalog,
            "register_release",
            side_effect=register_competitor_then_fail,
        ), self.assertRaisesRegex(RuntimeError, "competitor committed first"):
            ingest_staged_github_release(self.catalog, request)

        losing_final = self.publish_root / request.dataset_id / request.snapshot_id
        self.assertFalse(losing_final.exists())
        releases = self.catalog.list_releases()
        self.assertEqual(len(releases), 1)
        self.assertEqual(Path(releases[0].root_path), competitor_qlib.resolve())
        self.catalog.verify_release(releases[0])

    def test_windows_reserved_identifiers_and_catalog_case_collisions_are_rejected(self) -> None:
        archive = self.make_archive()
        metadata = self.make_metadata(archive)
        request = self.make_request(archive, metadata)
        for unsafe_dataset in ("CON", "dataset."):
            bad = GitHubIngestRequest(
                **{**request.__dict__, "dataset_id": unsafe_dataset}
            )
            with self.subTest(dataset_id=unsafe_dataset), self.assertRaises(InvalidRelease):
                ingest_staged_github_release(self.catalog, bad)

        release = ingest_staged_github_release(self.catalog, request)
        self.assertTrue(Path(release.root_path).is_dir())
        case_variant = GitHubIngestRequest(
            **{**request.__dict__, "dataset_id": request.dataset_id.upper()}
        )
        with self.assertRaisesRegex(IntegrityError, "case-insensitive dataset_id collision"):
            ingest_staged_github_release(self.catalog, case_variant)
        self.assertEqual(len(self.catalog.list_releases()), 1)

    def test_wrong_calendar_end_is_rejected_without_catalog_pollution(self) -> None:
        archive = self.make_archive(calendar_end="2026-07-13")
        metadata = self.make_metadata(archive)
        with self.assertRaises(QlibValidationError):
            ingest_staged_github_release(
                self.catalog, self.make_request(archive, metadata)
            )
        self.assert_no_publication()

    def test_metadata_contract_rejects_untrusted_release_shapes(self) -> None:
        archive = self.make_archive()
        for mutation in ("missing_digest", "draft", "prerelease", "duplicate"):
            metadata = self.make_metadata(archive)
            if mutation == "missing_digest":
                del metadata["assets"][0]["digest"]
            elif mutation == "draft":
                metadata["draft"] = True
            elif mutation == "prerelease":
                metadata["prerelease"] = True
            else:
                metadata["assets"].append(dict(metadata["assets"][0]))
            with self.subTest(mutation=mutation), self.assertRaises(GitHubMetadataError):
                parse_github_release_metadata(
                    metadata, owner=OWNER, repository=REPOSITORY
                )

    def test_cli_json_file_reads_are_regular_and_bounded(self) -> None:
        small = self.base / "small.json"
        small.write_bytes(b"{}")
        self.assertEqual(
            _read_bounded_file(small, label="fixture", max_bytes=2),
            b"{}",
        )
        oversized = self.base / "oversized.json"
        oversized.write_bytes(b"123")
        with self.assertRaisesRegex(InvalidRelease, "exceeds 2 bytes"):
            _read_bounded_file(oversized, label="fixture", max_bytes=2)
        with self.assertRaisesRegex(InvalidRelease, "regular non-link file"):
            _read_bounded_file(self.base, label="fixture", max_bytes=2)


if __name__ == "__main__":
    unittest.main()

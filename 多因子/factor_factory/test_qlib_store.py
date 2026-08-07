from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from factor_factory.qlib_store import InstrumentInterval, QlibBinaryStore


class QlibStoreIntervalBoundaryTests(unittest.TestCase):
    @staticmethod
    def _make_store(
        directory: Path,
        dates: tuple[str, ...],
        universe: str,
        intervals: tuple[str, ...],
    ) -> QlibBinaryStore:
        root = directory / "qlib"
        (root / "calendars").mkdir(parents=True)
        (root / "features").mkdir()
        (root / "instruments").mkdir()
        (root / "calendars" / "day.txt").write_text(
            "\n".join(dates) + "\n", encoding="utf-8"
        )
        (root / "instruments" / f"{universe}.txt").write_text(
            "\n".join(intervals) + "\n", encoding="utf-8"
        )
        return QlibBinaryStore(root)

    def test_index_weekend_bounds_snap_inward_to_trading_days(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._make_store(
                Path(directory),
                (
                    "2020-01-03",
                    "2020-01-06",
                    "2020-01-07",
                    "2020-01-10",
                    "2020-01-13",
                ),
                "csi300",
                ("SH600000\t2020-01-04\t2020-01-12",),
            )
            self.assertEqual(
                store.intervals("csi300"),
                (InstrumentInterval("SH600000", "2020-01-06", "2020-01-10"),),
            )

    def test_index_holiday_bounds_snap_inward_in_both_directions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._make_store(
                Path(directory),
                (
                    "2020-01-22",
                    "2020-01-23",
                    "2020-02-03",
                    "2020-02-04",
                    "2020-02-05",
                ),
                "csi500",
                (
                    "SH600000\t2020-01-24\t2020-02-04",
                    "SZ000001\t2020-01-22\t2020-02-02",
                ),
            )
            rows = store.intervals("csi500")
            self.assertEqual((rows[0].start, rows[0].end), ("2020-02-03", "2020-02-04"))
            self.assertEqual((rows[1].start, rows[1].end), ("2020-01-22", "2020-01-23"))

    def test_index_interval_with_no_trading_day_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._make_store(
                Path(directory),
                ("2020-01-23", "2020-02-03"),
                "csi800",
                ("SH600000\t2020-01-24\t2020-02-02",),
            )
            with self.assertRaisesRegex(ValueError, "invalid Qlib interval bounds"):
                store.intervals("csi800")

    def test_index_interval_completely_outside_calendar_is_rejected(self) -> None:
        for label, interval in (
            ("before", "SH600000\t2019-12-01\t2019-12-31"),
            ("after", "SH600000\t2020-02-06\t2020-03-01"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                store = self._make_store(
                    Path(directory),
                    ("2020-01-23", "2020-02-03", "2020-02-05"),
                    "csi1000",
                    (interval,),
                )
                with self.assertRaisesRegex(ValueError, "invalid Qlib interval bounds"):
                    store.intervals("csi1000")

    def test_all_universe_still_requires_exact_trading_day_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._make_store(
                Path(directory),
                ("2020-01-03", "2020-01-06"),
                "all",
                ("SH600000\t2020-01-04\t2020-01-05",),
            )
            with self.assertRaisesRegex(ValueError, "invalid Qlib interval bounds"):
                store.intervals("all")

    def test_calendar_remains_strictly_unique_and_sorted(self) -> None:
        for label, dates in (
            ("duplicate", ("2020-01-03", "2020-01-03")),
            ("unsorted", ("2020-01-06", "2020-01-03")),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, "unique, and strictly increasing"):
                    self._make_store(
                        Path(directory),
                        dates,
                        "all",
                        (f"SH600000\t{dates[0]}\t{dates[-1]}",),
                    )


class QlibStoreCausalityTests(unittest.TestCase):
    @staticmethod
    def _write_feature(root: Path, symbol: str, field: str, values: np.ndarray) -> None:
        directory = root / "features" / symbol.lower()
        directory.mkdir(parents=True, exist_ok=True)
        payload = np.concatenate((np.array([0.0], dtype="<f4"), np.asarray(values, dtype="<f4")))
        payload.tofile(directory / f"{field}.day.bin")

    @classmethod
    def _make_store(cls, directory: str) -> tuple[Path, tuple[str, ...]]:
        root = Path(directory) / "qlib"
        dates = tuple(str(date(2020, 1, 1) + timedelta(days=index)) for index in range(90))
        (root / "calendars").mkdir(parents=True)
        (root / "instruments").mkdir(parents=True)
        (root / "calendars" / "day.txt").write_text("\n".join(dates) + "\n", encoding="utf-8")
        symbols = ("SH600000", "SZ000001")
        (root / "instruments" / "all.txt").write_text(
            "".join(f"{symbol}\t{dates[0]}\t{dates[-1]}\n" for symbol in symbols),
            encoding="utf-8",
        )
        for symbol in symbols:
            close = np.ones(90, dtype=np.float32)
            if symbol == "SZ000001":
                close[30:] = 1.20
            open_price = close.copy()
            high = close * 1.01
            low = close * 0.99
            change = np.zeros(90, dtype=np.float32)
            volume = np.full(90, 1_000_000.0, dtype=np.float32)
            amount = volume * close
            for field, values in {
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
                "change": change,
            }.items():
                cls._write_feature(root, symbol, field, values)
        return root, dates

    @staticmethod
    def _replace_value(path: Path, global_index: int, value: float) -> None:
        payload = np.fromfile(path, dtype="<f4")
        payload[global_index + 1] = value
        payload.tofile(path)

    def test_same_day_close_high_low_change_do_not_change_open_execution_masks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, dates = self._make_store(directory)
            store = QlibBinaryStore(root)
            baseline = store.load_panel(
                start_date=dates[20], end_date=dates[60], minimum_history_dates=5
            )
            symbol = "SH600000"
            global_index = 40
            local_index = global_index - 20
            feature_dir = root / "features" / symbol.lower()
            for field, value in (("close", 2.0), ("high", 2.1), ("low", 1.9), ("change", 0.5)):
                self._replace_value(feature_dir / f"{field}.day.bin", global_index, value)
            changed = QlibBinaryStore(root).load_panel(
                start_date=dates[20], end_date=dates[60], minimum_history_dates=5
            )
            column = baseline.symbols.index(symbol)
            self.assertEqual(baseline.buyable[local_index, column], changed.buyable[local_index, column])
            self.assertEqual(baseline.sellable[local_index, column], changed.sellable[local_index, column])

    def test_history_and_anomaly_state_are_stable_across_request_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, dates = self._make_store(directory)
            store = QlibBinaryStore(root)
            early = store.load_panel(
                start_date=dates[20], end_date=dates[70], minimum_history_dates=25
            )
            late = store.load_panel(
                start_date=dates[40], end_date=dates[70], minimum_history_dates=25
            )
            common_offset = 40 - 20
            for symbol in early.symbols:
                early_column = early.symbols.index(symbol)
                late_column = late.symbols.index(symbol)
                np.testing.assert_array_equal(
                    early.eligible[common_offset:, early_column], late.eligible[:, late_column]
                )
                np.testing.assert_array_equal(
                    early.buyable[common_offset:, early_column], late.buyable[:, late_column]
                )
                np.testing.assert_array_equal(
                    early.sellable[common_offset:, early_column], late.sellable[:, late_column]
                )

    def test_locked_limit_proxy_cannot_be_silently_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, dates = self._make_store(directory)
            with self.assertRaisesRegex(ValueError, "locked-limit proxy is forbidden"):
                QlibBinaryStore(root).load_panel(
                    start_date=dates[20],
                    end_date=dates[60],
                    minimum_history_dates=5,
                    apply_locked_limit_proxy=True,
                )

    def test_open_history_gate_never_reads_same_day_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, dates = self._make_store(directory)
            store = QlibBinaryStore(root)
            baseline = store.load_panel(
                start_date=dates[0], end_date=dates[20], minimum_history_dates=5
            )
            symbol = "SH600000"
            column = baseline.symbols.index(symbol)
            threshold_day = 4
            self.assertFalse(baseline.buyable[threshold_day, column])
            self.assertTrue(baseline.buyable[threshold_day + 1, column])

            path = root / "features" / symbol.lower() / "close.day.bin"
            self._replace_value(path, threshold_day, float("nan"))
            changed = QlibBinaryStore(root).load_panel(
                start_date=dates[0], end_date=dates[20], minimum_history_dates=5
            )
            self.assertEqual(
                baseline.buyable[threshold_day, column],
                changed.buyable[threshold_day, column],
            )
            self.assertFalse(changed.buyable[threshold_day + 1, column])

    def test_validation_boundary_content_seal_leaves_future_features_unmaterialized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, dates = self._make_store(directory)
            panel = QlibBinaryStore(root).load_panel(
                start_date=dates[20],
                end_date=dates[60],
                minimum_history_dates=5,
                seal_after_date=dates[40],
            )
            first_sealed_row = 40 - 20 + 1
            for values in panel.fields.values():
                self.assertTrue(np.all(~np.isfinite(values[first_sealed_row:])))
            self.assertFalse(np.any(panel.eligible[first_sealed_row:]))
            self.assertFalse(np.any(panel.buyable[first_sealed_row:]))
            self.assertFalse(panel.metadata["holdout_content_loaded"])
            self.assertEqual(panel.metadata["content_loaded_through"], dates[40])
            before_fingerprint = panel.metadata["raw_tree_fingerprint"]
            path = root / "features" / "sh600000" / "close.day.bin"
            self._replace_value(path, 55, 3.0)
            changed = QlibBinaryStore(root).load_panel(
                start_date=dates[20],
                end_date=dates[60],
                minimum_history_dates=5,
                seal_after_date=dates[40],
            )
            np.testing.assert_array_equal(
                np.nan_to_num(panel.fields["close"][:first_sealed_row], nan=-1.0),
                np.nan_to_num(changed.fields["close"][:first_sealed_row], nan=-1.0),
            )
            self.assertNotEqual(
                before_fingerprint, changed.metadata["raw_tree_fingerprint"]
            )


if __name__ == "__main__":
    unittest.main()

from pathlib import Path

import pytest

from app.fundamental import main


PROFILE = Path(__file__).resolve().parents[2] / "config/examples/inference_memory.yaml"


def test_two_information_windows(tmp_path):
    march, may = tmp_path / "march.md", tmp_path / "may.md"
    first = "2026-03-26T18:00:00+08:00"
    main([str(PROFILE), "--as-of", first, "--output", str(march)])
    main([str(PROFILE), "--as-of", "2026-05-21T18:00:00+08:00", "--since", first,
          "--output", str(may)])
    early = march.read_text(encoding="utf-8")
    later = may.read_text(encoding="utf-8")
    assert "reducing the key value memory size" in early
    assert "3.2 quadrillion" not in early and "3.2 quadrillion" in later
    assert "flowchart LR" in later and "下游待复核" in later
    assert "特定存储供应商收入增长" in later


def test_existing_report_is_preserved(tmp_path):
    existing = tmp_path / "brief.md"
    existing.write_text("previous report", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        main([str(PROFILE), "--output", str(existing)])
    assert error.value.code == 2
    assert existing.read_text(encoding="utf-8") == "previous report"


def test_explicit_history_rejects_network_refresh():
    with pytest.raises(SystemExit) as error:
        main([str(PROFILE), "--as-of", "2026-03-26T18:00:00+08:00", "--refresh"])
    assert error.value.code == 2

#!/usr/bin/env python3
"""Bounded SSE/SZSE official calendar and rule evidence intake.

This collector writes only INSPECTION_ONLY/NOT_ADMITTED evidence under the
2026-08-01 R0 quarantine/report roots.  It never writes the formal catalog,
source registry, Roadmap, snapshots, or published data.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests


ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "r0-official-calendar-rules-intake-20260801-v1"
CANDIDATE = ROOT / "data/market/quarantine/r0-official-calendar-rules/intake-20260801-v1"
REPORT = ROOT / "reports/data-governance/r0-official-calendar-rules-intake-20260801-v1"
PURPOSE = "INSPECTION_ONLY"
QUALITY = "NOT_ADMITTED"
TZ = timezone(timedelta(hours=8))
ALLOWED_HOSTS = {"www.sse.com.cn", "www.szse.cn", "investor.szse.cn", "www.bse.cn"}
GUARDED_FILES = [
    ROOT / "data/catalog/datasets.json",
    ROOT / "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    ROOT / "control/ROADMAP.md",
    ROOT / "control/projects.yaml",
]
GUARDED_TREES = [ROOT / "data/market/snapshots", ROOT / "data/published"]


def item(item_id: str, venue: str, year: int | None, category: str, title: str,
         url: str, rel: str, max_bytes: int = 16 * 1024 * 1024) -> dict:
    return dict(item_id=item_id, venue=venue, year=year, category=category,
                title=title, url=url, relative_path=rel, max_bytes=max_bytes)


SPECS = [
    # SSE annual holiday notices.  2005-2008 remain explicit first-batch gaps.
    item("sse-calendar-2009", "SSE", 2009, "annual_calendar", "2009 annual closure notice", "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988267.shtml", "raw/sse/calendar/2009.html"),
    item("sse-calendar-2010", "SSE", 2010, "annual_calendar", "2010 annual closure notice", "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988350.shtml", "raw/sse/calendar/2010.html"),
    item("sse-calendar-2011", "SSE", 2011, "annual_calendar", "2011 annual closure notice", "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988440.shtml", "raw/sse/calendar/2011.html"),
    item("sse-calendar-2012", "SSE", 2012, "annual_calendar", "2012 annual closure notice", "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988508.shtml", "raw/sse/calendar/2012.html"),
    item("sse-calendar-2013", "SSE", 2013, "annual_calendar", "2013 annual closure notice", "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988635.shtml", "raw/sse/calendar/2013.html"),
    item("sse-calendar-2014", "SSE", 2014, "annual_calendar", "2014 annual closure notice", "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988725.shtml", "raw/sse/calendar/2014.html"),
    item("sse-calendar-2015", "SSE", 2015, "annual_calendar", "2015 annual closure notice", "https://www.sse.com.cn/disclosure/dealinstruc/closed/list/c/c_20160104_3979217.shtml", "raw/sse/calendar/2015.html"),
    item("sse-calendar-2016", "SSE", 2016, "annual_calendar", "2016 annual closure notice", "https://www.sse.com.cn/disclosure/dealinstruc/closed/list/c/c_20160104_4028872.shtml", "raw/sse/calendar/2016.html"),
    item("sse-calendar-2017", "SSE", 2017, "annual_calendar", "2017 annual closure notice", "https://www.sse.com.cn/disclosure/announcement/general/c/c_20161222_4218613.shtml", "raw/sse/calendar/2017.html"),
    item("sse-calendar-2018", "SSE", 2018, "annual_calendar", "2018 annual closure notice", "https://www.sse.com.cn/disclosure/announcement/general/c/c_20171222_4438363.shtml", "raw/sse/calendar/2018.html"),
    item("sse-calendar-2019", "SSE", 2019, "annual_calendar", "2019 annual closure notice", "https://www.sse.com.cn/disclosure/announcement/general/c/c_20181220_4696473.shtml", "raw/sse/calendar/2019.html"),
    item("sse-calendar-2020", "SSE", 2020, "annual_calendar", "2020 annual closure notice", "https://www.sse.com.cn/disclosure/announcement/general/c/c_20191220_4969627.shtml", "raw/sse/calendar/2020.html"),
    item("sse-calendar-2021", "SSE", 2021, "annual_calendar", "2021 annual closure notice", "https://www.sse.com.cn/disclosure/announcement/general/c/c_20201224_5286949.shtml", "raw/sse/calendar/2021.html"),
    item("sse-calendar-2022", "SSE", 2022, "annual_calendar", "2022 annual closure notice", "https://www.sse.com.cn/disclosure/announcement/general/c/c_20211220_5662606.shtml", "raw/sse/calendar/2022.html"),
    item("sse-calendar-2023", "SSE", 2023, "annual_calendar", "2023 annual closure notice", "https://www.sse.com.cn/disclosure/announcement/general/c/c_20221227_5714458.shtml", "raw/sse/calendar/2023.html"),
    item("sse-calendar-2024", "SSE", 2024, "annual_calendar", "2024 annual closure notice", "https://www.sse.com.cn/disclosure/announcement/general/c/c_20231226_5733939.shtml", "raw/sse/calendar/2024.html"),
    item("sse-revision-2019-labor", "SSE", 2019, "calendar_revision", "2019 Labor Day adjustment", "https://www.sse.com.cn/disclosure/announcement/general/c/c_20190418_4771364.shtml", "raw/sse/calendar-revisions/2019-labor.html"),
    # SZSE annual holiday notices, complete for the requested 2005-2024 range.
    item("szse-calendar-2005", "SZSE", 2005, "annual_calendar", "2005 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20041223_499349.html", "raw/szse/calendar/2005.html"),
    item("szse-calendar-2006", "SZSE", 2006, "annual_calendar", "2006 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20051223_499514.html", "raw/szse/calendar/2006.html"),
    item("szse-calendar-2007", "SZSE", 2007, "annual_calendar", "2007 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20061221_499734.html", "raw/szse/calendar/2007.html"),
    item("szse-calendar-2008", "SZSE", 2008, "annual_calendar", "2008 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20071225_499949.html", "raw/szse/calendar/2008.html"),
    item("szse-calendar-2009", "SZSE", 2009, "annual_calendar", "2009 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20081223_500177.html", "raw/szse/calendar/2009.html"),
    item("szse-calendar-2010", "SZSE", 2010, "annual_calendar", "2010 annual closure notice", "https://www.szse.cn/disclosure/notice/t20091223_500314.html", "raw/szse/calendar/2010.html"),
    item("szse-calendar-2011", "SZSE", 2011, "annual_calendar", "2011 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20101227_500628.html", "raw/szse/calendar/2011.html"),
    item("szse-calendar-2012", "SZSE", 2012, "annual_calendar", "2012 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20111226_500868.html", "raw/szse/calendar/2012.html"),
    item("szse-calendar-2013", "SZSE", 2013, "annual_calendar", "2013 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20121225_501056.html", "raw/szse/calendar/2013.html"),
    item("szse-calendar-2014", "SZSE", 2014, "annual_calendar", "2014 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20131225_501163.html", "raw/szse/calendar/2014.html"),
    item("szse-calendar-2015", "SZSE", 2015, "annual_calendar", "2015 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20141224_501340.html", "raw/szse/calendar/2015.html"),
    item("szse-calendar-2016", "SZSE", 2016, "annual_calendar", "2016 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20151224_501645.html", "raw/szse/calendar/2016.html"),
    item("szse-calendar-2017", "SZSE", 2017, "annual_calendar", "2017 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20161222_501883.html", "raw/szse/calendar/2017.html"),
    item("szse-calendar-2018", "SZSE", 2018, "annual_calendar", "2018 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20171222_502255.html", "raw/szse/calendar/2018.html"),
    item("szse-calendar-2019", "SZSE", 2019, "annual_calendar", "2019 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20181220_563695.html", "raw/szse/calendar/2019.html"),
    item("szse-calendar-2020", "SZSE", 2020, "annual_calendar", "2020 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20191220_572766.html", "raw/szse/calendar/2020.html"),
    item("szse-calendar-2021", "SZSE", 2021, "annual_calendar", "2021 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20201224_583950.html", "raw/szse/calendar/2021.html"),
    item("szse-calendar-2022", "SZSE", 2022, "annual_calendar", "2022 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20211220_590321.html", "raw/szse/calendar/2022.html"),
    item("szse-calendar-2023", "SZSE", 2023, "annual_calendar", "2023 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20221227_598022.html", "raw/szse/calendar/2023.html"),
    item("szse-calendar-2024", "SZSE", 2024, "annual_calendar", "2024 annual closure notice", "https://www.szse.cn/disclosure/notice/general/t20231226_605108.html", "raw/szse/calendar/2024.html"),
    item("szse-revision-2019-labor", "SZSE", 2019, "calendar_revision", "2019 Labor Day adjustment", "https://www.szse.cn/disclosure/notice/general/t20190418_566376.html", "raw/szse/calendar-revisions/2019-labor.html"),
    item("szse-revision-2020-spring", "SZSE", 2020, "calendar_revision", "2020 Spring Festival extension", "https://www.szse.cn/disclosure/notice/general/t20200127_573917.html", "raw/szse/calendar-revisions/2020-spring-extension.html"),
    # Rule/version evidence.  These receipts establish source documents, not
    # complete historical rule coverage by themselves.
    item("sse-trading-rule-2006", "SSE", 2006, "trading_rule", "SSE trading rule 2006", "https://www.sse.com.cn/lawandrules/sselawsrules/repeal/rules/c/c_20230418_5720136.shtml", "raw/sse/rules/trading-rule-2006.html"),
    item("sse-trading-rule-amendment-2014", "SSE", 2014, "trading_rule", "SSE trading rule amendment 2014", "https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20250612_10781699.shtml", "raw/sse/rules/trading-rule-amendment-2014.html"),
    item("sse-trading-rule-amendment-2018", "SSE", 2018, "trading_rule", "SSE trading rule amendment 2018", "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/rules/c/c_20180806_10784917.shtml", "raw/sse/rules/trading-rule-amendment-2018.html"),
    item("sse-trading-rule-amendment-2020", "SSE", 2020, "trading_rule", "SSE trading rule amendment 2020", "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/rules/c/c_20200313_10785127.shtml", "raw/sse/rules/trading-rule-amendment-2020.html"),
    item("sse-trading-rule-2023-doc", "SSE", 2023, "trading_rule", "SSE trading rule 2023 document", "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/rules/c/10824490/files/dcbe58edb194451d93f19b1f7dd8fb4c.docx", "raw/sse/rules/trading-rule-2023.docx"),
    item("sse-star-special-rule-2019", "SSE", 2019, "price_limit_ipo", "STAR trading special rule 2019", "https://www.sse.com.cn/lawandrules/sselawsrules/repeal/rules/c/10118601/files/f6fc4a1d4c1f469183a013c4dc36a535.pdf", "raw/sse/rules/star-special-rule-2019.pdf"),
    item("sse-relisting-rule-2013", "SSE", 2013, "delisting_relisting", "SSE relisting rule 2013", "https://www.sse.com.cn/lawandrules/sselawsrules/repeal/rules/c/c_20230421_5720457.shtml", "raw/sse/rules/relisting-rule-2013.html"),
    item("sse-relisting-notice-2020", "SSE", 2020, "delisting_relisting", "SSE relisting amendment 2020", "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20200529_5114434.shtml", "raw/sse/rules/relisting-amendment-2020.html"),
    item("szse-trading-rule-2011", "SZSE", 2011, "trading_rule", "SZSE trading rule 2011", "https://www.szse.cn/lawrules/rule/repeal/rules/P020231230545287939887.pdf", "raw/szse/rules/trading-rule-2011.pdf"),
    item("szse-trading-rule-2020", "SZSE", 2020, "trading_rule", "SZSE trading rule 2020", "https://www.szse.cn/lawrules/rule/repeal/rules/P020231230545293899442.pdf", "raw/szse/rules/trading-rule-2020.pdf"),
    item("szse-chinext-special-rule-2020", "SZSE", 2020, "price_limit_ipo", "ChiNext trading special rule 2020", "https://www.szse.cn/lawrules/rule/repeal/rules/P020231230545310237980.pdf", "raw/szse/rules/chinext-special-rule-2020.pdf"),
    item("szse-chinext-effective-2020", "SZSE", 2020, "price_limit_ipo", "ChiNext registration trading effective event", "https://www.szse.cn/aboutus/trends/news/t20200821_580924.html", "raw/szse/rules/chinext-effective-2020.html"),
    item("szse-mainboard-ipo-faq-2023", "SZSE", 2023, "price_limit_ipo", "Main-board IPO trading FAQ 2023", "https://investor.szse.cn/knowledge/stock/deal/t20230525_600599.html", "raw/szse/rules/mainboard-ipo-faq-2023.html"),
    # One reachability probe only; BSE is not required for this R0 batch.
    item("bse-reachability-2021", "BSE", 2021, "reachability_only", "BSE trading rule page reachability", "https://www.bse.cn/jygl_list/200010919.html", "raw/bse/reachability/trading-rule-2021.html"),
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def tree_fingerprint(path: Path) -> str:
    rows = []
    if path.exists():
        for p in sorted(x for x in path.rglob("*") if x.is_file()):
            rows.append(f"{p.relative_to(path).as_posix()}\t{p.stat().st_size}\t{sha256_file(p)}")
    return sha256_bytes("\n".join(rows).encode("utf-8"))


def guards() -> dict:
    return {
        "files": {str(p.relative_to(ROOT)): sha256_file(p) for p in GUARDED_FILES},
        "trees": {str(p.relative_to(ROOT)): tree_fingerprint(p) for p in GUARDED_TREES},
    }


def dump(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    if CANDIDATE.exists() or REPORT.exists():
        raise SystemExit("output already exists; fail closed")
    before = guards()
    CANDIDATE.mkdir(parents=True)
    REPORT.mkdir(parents=True)
    receipts = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Quantification-R0-Official-Evidence/1.0"})
    for n, spec in enumerate(SPECS):
        host = (urlparse(spec["url"]).hostname or "").lower()
        if host not in ALLOWED_HOSTS:
            raise RuntimeError(f"host not allow-listed: {host}")
        started = datetime.now(TZ).isoformat()
        status = None
        error = None
        headers = {}
        body = b""
        try:
            response = session.get(spec["url"], timeout=(10, 30), allow_redirects=False)
            status = response.status_code
            headers = {k.lower(): v for k, v in response.headers.items() if k.lower() in {"content-type", "content-length", "last-modified", "etag", "date", "location"}}
            body = response.content
            if len(body) > spec["max_bytes"]:
                error = "MAX_BYTES_EXCEEDED"
            elif status != 200:
                error = f"HTTP_{status}"
            elif not body:
                error = "EMPTY_BODY"
        except Exception as exc:  # failure evidence is part of the intake
            error = f"{type(exc).__name__}: {exc}"
        ok = error is None
        rel = Path(spec["relative_path"] if ok else f"raw/failed/{spec['item_id']}.bin")
        target = CANDIDATE / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        receipts.append({
            **spec,
            "request_sequence": n + 1,
            "started_at": started,
            "completed_at": datetime.now(TZ).isoformat(),
            "http_status": status,
            "response_headers_safe": headers,
            "success": ok,
            "error": error,
            "bytes": len(body),
            "sha256": sha256_bytes(body),
            "stored_path": target.relative_to(ROOT).as_posix(),
        })
        time.sleep(0.75)

    coverage = []
    for venue in ("SSE", "SZSE"):
        for year in range(2005, 2025):
            hits = [r for r in receipts if r["venue"] == venue and r["year"] == year and r["category"] == "annual_calendar" and r["success"]]
            coverage.append({"venue": venue, "year": year, "status": "PRESENT" if hits else "MISSING", "item_ids": [h["item_id"] for h in hits]})
    missing = [x for x in coverage if x["status"] == "MISSING"]
    rule_matrix = []
    for venue in ("SSE", "SZSE"):
        for category in ("trading_rule", "price_limit_ipo", "delisting_relisting", "st_special_treatment"):
            hits = [r for r in receipts if r["venue"] == venue and r["category"] == category and r["success"]]
            rule_matrix.append({
                "venue": venue,
                "category": category,
                "status": "PARTIAL_EVIDENCE_PRESENT" if hits else "MISSING",
                "years": sorted({h["year"] for h in hits if h["year"] is not None}),
                "item_ids": [h["item_id"] for h in hits],
                "complete_version_chain": False,
            })
    after = guards()
    guard_pass = before == after
    manifest = {
        "run_id": RUN_ID,
        "created_at": datetime.now(TZ).isoformat(),
        "scope": "SSE/SZSE A-share official calendar and rule evidence, 2005-01-01 through 2024-12-31; BSE one reachability probe only",
        "purpose_ceiling": PURPOSE,
        "quality_status": QUALITY,
        "formal_admission": False,
        "official_sources_only": True,
        "third_party_gap_fill": False,
        "calendar_inference_from_statutory_holidays": False,
        "request_policy": "serial one-attempt GET; 0.75 second inter-request delay; no authentication, cookies, retries, redirects, or WAF bypass",
        "receipt_count": len(receipts),
        "success_count": sum(r["success"] for r in receipts),
        "failure_count": sum(not r["success"] for r in receipts),
        "receipts": receipts,
        "calendar_coverage": coverage,
        "missing_calendar_years": missing,
        "rule_version_coverage": rule_matrix,
        "known_unclosed_items": [
            "Complete temporary suspension/emergency closure archive is not established.",
            "Complete historical trading-session publication/version chain is not established.",
            "ST/special-treatment rule history is not established by this batch.",
            "Price-limit, IPO/no-limit, delisting/relisting rule chains remain partial.",
            "BSE is reachability-only and is not an R0 required venue in this batch.",
            "Official document presence does not itself satisfy semantic parsing, PIT normalization, or formal admission.",
        ],
        "guard_before": before,
        "guard_after": after,
        "guard_pass": guard_pass,
    }
    dump(CANDIDATE / "MANIFEST.json", manifest)
    dump(REPORT / "SOURCE_RECEIPTS.json", receipts)
    dump(REPORT / "CALENDAR_COVERAGE_MATRIX.json", coverage)
    dump(REPORT / "RULE_VERSION_COVERAGE_MATRIX.json", rule_matrix)
    dump(REPORT / "MISSING_ITEMS.json", {"calendar_years": missing, "known_unclosed_items": manifest["known_unclosed_items"]})
    report_manifest = {
        "run_id": RUN_ID,
        "purpose_ceiling": PURPOSE,
        "quality_status": QUALITY,
        "formal_effect": "NONE",
        "candidate_manifest": (CANDIDATE / "MANIFEST.json").relative_to(ROOT).as_posix(),
        "candidate_manifest_sha256": sha256_file(CANDIDATE / "MANIFEST.json"),
        "guard_pass": guard_pass,
        "success_count": manifest["success_count"],
        "failure_count": manifest["failure_count"],
        "missing_calendar_years": missing,
    }
    dump(REPORT / "REPORT_MANIFEST.json", report_manifest)
    (REPORT / "README.md").write_text(
        "# R0 official calendar/rules intake (2026-08-01 v1)\n\n"
        f"- Official raw receipts: {manifest['success_count']} success / {manifest['failure_count']} failure.\n"
        f"- Purpose: `{PURPOSE}`; quality: `{QUALITY}`; formal effect: `NONE`.\n"
        "- No formal catalog, source registry, Roadmap, snapshot, published data, or quality state was changed.\n"
        "- Coverage is evidence availability only. It is not a parsed exchange calendar and is not research-admitted.\n"
        "- Missing years and incomplete rule chains are listed in `MISSING_ITEMS.json`.\n",
        encoding="utf-8",
    )
    if not guard_pass:
        raise SystemExit("guard drift detected")
    print(json.dumps(report_manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

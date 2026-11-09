"""The markdown estate report and the `report` CLI command."""
import re
from functools import lru_cache
from pathlib import Path

from deltabound import cli, graph, report
from deltabound.ir import ObjectKind
from deltabound.parsers import bods

ESTATE_DIR = Path(__file__).parent.parent / "samples" / "bods"


@lru_cache(maxsize=1)
def estate():
    return bods.parse_files(sorted(ESTATE_DIR.glob("*.xml")))


@lru_cache(maxsize=1)
def md() -> str:
    return report.render_markdown(estate())


def section(title: str) -> str:
    text = md()
    start = text.index(f"## {title}")
    nxt = text.find("\n## ", start + 1)
    return text[start: nxt if nxt != -1 else None]


def test_has_all_sections():
    for title in ("Counts", "Parser coverage", "Dead code", "Most complex jobs (top 5)", "Migration waves"):
        assert f"## {title}" in md()


def test_counts():
    s = section("Counts")
    assert "| Jobs | 15 |" in s and "| Workflows | 30 |" in s and "| Dataflows | 60 |" in s


def test_coverage_lists_every_unparsed_item():
    s = section("Parser coverage")
    assert re.search(r"\*\*97%\*\* of transforms modeled \(220 of 226; 6 opaque\)", s)
    for item in estate().unparsed:
        assert f"- `{item}`" in s
    assert "| `DIPivot` | 1 |" in s


def test_dead_code_and_missing_reference():
    s = section("Dead code")
    for name in graph.unreachable(estate()):
        assert f"| {name} |" in s
    assert "| WF_PURGE_AUDIT_LOG | WF_HOUSEKEEPING |" in s


def test_top_five_in_descending_complexity():
    rows = re.findall(r"^\| (\d) \| (\w+) \| (\d+) \| (\d+) \| (\d+) \|$", section("Most complex jobs"), re.M)
    assert len(rows) == 5
    expected = sorted(((graph.complexity(estate(), j.name), j.name) for j in estate().by_kind(ObjectKind.JOB)),
                      key=lambda x: (-x[0], x[1]))[:5]
    assert [(int(r[2]), r[1]) for r in rows] == expected
    assert rows[0][1] == "JOB_SALES_DAILY"
    assert rows[0][4] == "6"  # call depth


def test_waves_cover_every_job_once():
    s = section("Migration waves")
    assert "### Wave 1" in s and "### Wave 2" in s
    jobs = re.findall(r"^\| (JOB_\w+) \| \d+ \|$", s, re.M)
    assert sorted(jobs) == sorted(j.name for j in estate().by_kind(ObjectKind.JOB))


def test_options_change_the_report():
    text = report.render_markdown(estate(), top=3, wave_size=5)
    assert "## Most complex jobs (top 3)" in text
    assert "### Wave 3" in text


def test_pipe_characters_are_escaped():
    assert report._cell("a|b") == "a\\|b"


def test_cli_writes_report(tmp_path):
    out = tmp_path / "estate_report.md"
    cli.main(["report", str(ESTATE_DIR), "--out", str(out)])
    assert out.read_text() == md()


def test_report_on_mini_sample(tmp_path):
    out = tmp_path / "mini.md"
    cli.main(["report", str(ESTATE_DIR.parent / "bods_mini"), "--out", str(out), "--top", "5"])
    text = out.read_text()
    assert "## Most complex jobs (top 2)" in text
    assert "| WF_OLD_RETURNS_2019 | workflow |" in text

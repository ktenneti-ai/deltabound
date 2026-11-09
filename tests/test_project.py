"""Source projects: one folder per source with a deltabound.toml holding all settings."""
import tomllib
from pathlib import Path

import pytest

from deltabound import bridge, cli
from deltabound.convert import load_convert_config
from deltabound.explain import load_config
from deltabound.project import SOURCE_TYPES, ProjectError, find_manifest, load_project

ROOT = Path(__file__).parent.parent
DEMO = ROOT / "demo"


def test_every_demo_source_is_complete():
    folders = sorted(p for p in DEMO.iterdir() if p.is_dir())
    assert {p.name for p in folders} >= {"sap_bods", "sql_server"}
    for folder in folders:
        data = tomllib.loads((folder / "deltabound.toml").read_text())
        assert data["source"]["type"] in SOURCE_TYPES, folder
        assert (folder / "README.md").exists() and (folder / "exports").is_dir(), folder
        assert {"source", "convert", "explain", "validate"} <= set(data), folder


def test_load_sap_project():
    p = load_project(DEMO / "sap_bods")
    assert p.source.type == "sap_bods" and len(p.export_files()) == 7
    e = p.load_estate()
    assert len(e.objects) == 105
    assert find_manifest(DEMO / "sap_bods" / "deltabound.toml") == DEMO / "sap_bods" / "deltabound.toml"
    assert find_manifest(DEMO / "sap_bods" / "exports") is None


def test_planned_source_is_refused_clearly(capsys):
    with pytest.raises(ProjectError, match="this source type is planned"):
        load_project(DEMO / "sql_server").load_estate()
    with pytest.raises(SystemExit, match="planned"):
        cli.main(["summary", str(DEMO / "sql_server")])


def test_bad_manifests(tmp_path):
    (tmp_path / "deltabound.toml").write_text('[source]\nname = "x"\ntype = "informatica"\n')
    with pytest.raises(ProjectError, match="unknown source type"):
        load_project(tmp_path)
    with pytest.raises(ProjectError, match="not a source folder"):
        load_project(ROOT / "samples")
    (tmp_path / "deltabound.toml").write_text('[source]\nname = "x"\ntype = "sap_bods"\nexports = ["none"]\n')
    with pytest.raises(ProjectError, match="no export files"):
        load_project(tmp_path).load_estate()


def test_settings_come_from_the_source_manifest():
    manifest = DEMO / "sap_bods" / "deltabound.toml"
    cfg = load_convert_config(manifest)
    assert cfg.datastores["DS_DWH.MART"] == "gold.mart" and cfg.bundle_prefix == "fernhill"
    ex = load_config(manifest)
    assert ex.cache_dir == str((DEMO / "sap_bods" / ".deltabound" / "explain").resolve())


def test_cli_commands_take_the_folder(tmp_path, capsys):
    cli.main(["summary", str(DEMO / "sap_bods")])
    assert "jobs       15" in capsys.readouterr().out
    cli.main(["convert", str(DEMO / "sap_bods"), "--job", "JOB_CUSTOMER_SYNC", "--out", str(tmp_path / "b")])
    nb = (tmp_path / "b" / "notebooks" / "dataflows" / "DF_DIM_CUSTOMER.py").read_text()
    assert 'spark.read.table("silver.stg.stg_customer")' in nb  # mapping from the source manifest
    assert 'name: "fernhill_job_customer_sync"' in (tmp_path / "b" / "databricks.yml").read_text()


def test_validate_run_takes_the_folder(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["validate", "run", str(DEMO / "sap_bods"), "--out", str(tmp_path)])
    assert e.value.code == 1  # the demo has planted failures
    assert (tmp_path / "JOB_SALES_DAILY" / "report.html").exists()


def test_viewer_bridge_recognises_a_source_manifest():
    r = bridge.load([("deltabound.toml", (DEMO / "sap_bods" / "deltabound.toml").read_bytes())])
    assert r["load_report"][0]["format"] == "source_manifest" and r["load_report"][0]["status"] == "skipped"

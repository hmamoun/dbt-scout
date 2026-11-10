"""Validate complete portable artifacts through the real package loader."""

import importlib.util
from pathlib import Path
import tempfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
spec = importlib.util.spec_from_file_location("scout_streamlit_loader", REPO / "scripts" / "scr_scout_stl.py")
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)


def test_complete_artifact_map_and_nearest_manifest():
    app = loader.load_streamlit(ROOT / "snowflake.yml")
    assert app["runtime_name"] == "SYSTEM$ST_CONTAINER_RUNTIME_PY3_11"
    files = app["source_files"]
    assert all(Path(source["uri"][7:]).is_file() for source in files)
    assert any(source["directory"] == "common" and source["uri"].endswith("comparison_ui.py") for source in files)
    manifests = [source for source in files if Path(source["uri"]).name in {"requirements.txt", "environment.yml", "pyproject.toml"}]
    assert len(manifests) == 1
    assert manifests[0]["directory"] == ""
    assert manifests[0]["uri"].endswith("requirements.txt")
    requirements = (ROOT / "app" / "requirements.txt").read_text()
    for dependency in ("streamlit==", "sqlglot==", "pandas==", "snowflake-snowpark-python=="):
        assert dependency in requirements
    assert "pytest==" not in requirements


@pytest.mark.parametrize("bad", ["../outside.py", "absent.py", "secrets.toml", "environment.yml", "pyproject.toml"])
def test_invalid_or_shadowing_artifacts_fail_closed(bad):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "app"
        source.mkdir()
        for name in ("streamlit_app.py", "requirements.txt", "secrets.toml", "environment.yml", "pyproject.toml"):
            (source / name).write_text("", encoding="utf-8")
        config = root / "snowflake.yml"
        config.write_text(yaml.safe_dump({
            "runtime_name": "SYSTEM$ST_CONTAINER_RUNTIME_PY3_11",
            "artifacts": ["streamlit_app.py", "requirements.txt", bad],
        }), encoding="utf-8")
        with pytest.raises(ValueError):
            loader.load_streamlit(config)


def test_no_legacy_startup_or_reporting_imports():
    import ast

    prohibited = {"config", "settings", "eval", "extraction", "schema_contracts"}
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[-1] not in prohibited
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"navigation", "switch_page"}
    assert not (ROOT / "app" / "pages").exists()


def test_optional_reporting_is_explicit_and_identifier_validated(monkeypatch):
    from common.preferences import optional_report_fqn

    monkeypatch.delenv("SCOUT_REQUEST_REPORT", raising=False)
    assert optional_report_fqn() is None
    monkeypatch.setenv("SCOUT_REQUEST_REPORT", "DB.S.REQUESTS")
    assert optional_report_fqn() == "DB.S.REQUESTS"
    monkeypatch.setenv("SCOUT_REQUEST_REPORT", "DB.S.REQUESTS; select 1")
    with pytest.raises(ValueError):
        optional_report_fqn()


def test_browser_harness_is_not_distributed():
    app = loader.load_streamlit(ROOT / "snowflake.yml")
    assert not any("preview_synthetic.py" in source["uri"] for source in app["source_files"])
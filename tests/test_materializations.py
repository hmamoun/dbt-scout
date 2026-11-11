"""Offline deployment checks. See ../docs/materializations.md#verification."""

import json
import re
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dbt_common.clients.jinja import get_template
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from scr_scout_agt import load_agent
from scr_scout_stl import load_streamlit


class FileLoadingTests(unittest.TestCase):
    def test_sample_agent_loads_both_markdown_files(self):
        agent = load_agent(ROOT / "agent" / "snowflake.yml")
        for kind in ("response", "orchestration"):
            self.assertTrue(agent["specification"]["instructions"][kind].strip())

    def test_sample_streamlit_includes_entrypoint_and_environment(self):
        app = load_streamlit(ROOT / "streamlit" / "snowflake.yml")
        self.assertTrue(any(path.endswith("/streamlit_app.py") for path in app["source_files"]))
        self.assertTrue(any(path.endswith("/environment.yml") for path in app["source_files"]))

    def test_streamlit_rejects_missing_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "snowflake.yml"
            config.write_text("name: TEST_APP\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "entrypoint not found"):
                load_streamlit(config)

    def test_streamlit_preserves_spaces_in_upload_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "app files"
            source.mkdir()
            (source / "streamlit_app.py").write_text("import streamlit\n", encoding="utf-8")
            config = root / "snowflake.yml"
            config.write_text("source_dir: app files\n", encoding="utf-8")
            app = load_streamlit(config)
            self.assertIn("app files", app["source_files"][0])
            self.assertNotIn("%20", app["source_files"][0])

    def test_streamlit_rejects_nested_main_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "snowflake.yml"
            config.write_text("main_file: ../other.py\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not a path"):
                load_streamlit(config)


class SqlTests(unittest.TestCase):
    def test_agent_sql_preserves_grants_and_escapes_instructions(self):
        agent = {"comment": "Scout's agent", "specification": {"instructions": {"response": "Don't invent data.\nUse C:\\data."}}}
        module = get_template((ROOT / "macros" / "mat_scout_agt.sql").read_text(), {"tojson": json.dumps}).module
        sql = module.dbt_macro__scout_agent_ddl(agent, '"DB"."SCHEMA"."AGENT"')
        self.assertIn("copy grants", sql)
        self.assertIn("Scout''s agent", sql)
        self.assertIn("docs/materializations.md#agent-materialization", sql)
        self.assertIn("Don''t invent data.", sql)
        self.assertIn("C:" + "\\" * 4 + "data", sql)

    def test_agent_materialization_builds_agent_without_relation(self):
        agent = load_agent(ROOT / "agent" / "snowflake.yml")
        statements = {}

        def statement(name, auto_begin=False, caller=None):
            statements[name] = caller()
            return ""

        def compiler_error(message):
            raise ValueError(message)

        package = SimpleNamespace()
        result = Mock(return_value="", unsafe_callable=False, alters_data=False)
        context = {
            "var": lambda name, default=None: agent,
            "this": SimpleNamespace(database="DB", schema="SCHEMA", identifier="SCOUT_AGENT"),
            "tojson": json.dumps,
            "adapter": SimpleNamespace(quote=lambda value: '"' + value.replace('"', '""') + '"', commit=Mock(unsafe_callable=False, alters_data=False)),
            "dbt_scout": package, "statement": statement,
            "run_hooks": Mock(return_value="", unsafe_callable=False, alters_data=False), "pre_hooks": [], "post_hooks": [],
            "return": result,
            "log": Mock(return_value="", unsafe_callable=False, alters_data=False), "exceptions": SimpleNamespace(raise_compiler_error=compiler_error),
        }
        module = get_template((ROOT / "macros" / "mat_scout_agt.sql").read_text(), context).module
        package.scout_agent_ddl = module.dbt_macro__scout_agent_ddl
        materialization = next(getattr(module, name) for name in dir(module) if "materialization_mat_scout_agt" in name)
        materialization()
        self.assertIn('create or replace agent "DB"."SCHEMA"."SCOUT_AGENT" copy grants', statements["main"])
        self.assertIn("instructions", statements["main"])
        result.assert_called_once_with({"relations": []})

    def test_streamlit_uploads_and_publishes_live_version(self):
        app = load_streamlit(ROOT / "streamlit" / "snowflake.yml")
        statements = {}

        def statement(name, auto_begin=False, caller=None):
            statements[name] = caller()
            return ""

        def compiler_error(message):
            raise ValueError(message)

        package = SimpleNamespace()
        result = Mock(return_value="", unsafe_callable=False, alters_data=False)
        context = {
            "var": lambda name, default=None: app,
            "this": SimpleNamespace(database="DB", schema="SCHEMA", identifier="SCOUT_STREAMLIT"),
            "target": SimpleNamespace(warehouse="TEST_WH"),
            "modules": SimpleNamespace(re=re),
            "adapter": SimpleNamespace(quote=lambda value: '"' + value.replace('"', '""') + '"', commit=Mock(unsafe_callable=False, alters_data=False)),
            "dbt_scout": package, "statement": statement,
            "run_hooks": Mock(return_value="", unsafe_callable=False, alters_data=False), "pre_hooks": [], "post_hooks": [],
            "invocation_id": "test-invocation", "return": result,
            "log": Mock(return_value="", unsafe_callable=False, alters_data=False), "exceptions": SimpleNamespace(raise_compiler_error=compiler_error),
        }
        module = get_template((ROOT / "macros" / "mat_scout_stl.sql").read_text(), context).module
        package.scout_streamlit_ddl = module.dbt_macro__scout_streamlit_ddl
        materialization = next(getattr(module, name) for name in dir(module) if "materialization_mat_scout_stl" in name)
        materialization()
        self.assertIn("create stage if not exists", statements["create_source_stage"])
        self.assertIn("auto_compress = false", statements["upload_source_1"])
        self.assertIn('create or replace streamlit "DB"."SCHEMA"."SCOUT_STREAMLIT"', statements["main"])
        self.assertIn('query_warehouse = "TEST_WH"', statements["main"])
        self.assertIn("docs/materializations.md", statements["main"])
        self.assertIn("add live version from last", statements["publish_live_version"])
        result.assert_called_once_with({"relations": []})


class StreamlitTests(unittest.TestCase):
    def test_local_preview_and_empty_filter(self):
        with patch.dict("os.environ", {"SCOUT_LOCAL_PREVIEW": "1"}):
            app = AppTest.from_file(str(ROOT / "streamlit" / "app" / "streamlit_app.py")).run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(app.title[0].value, "Scout Deployment Check")
            app.multiselect[0].set_value([]).run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(app.info[0].value, "No object types selected.")


if __name__ == "__main__":
    unittest.main()
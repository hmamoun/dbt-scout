# Snowflake Application Materializations

dbt_scout deploys two non-tabular Snowflake objects through opt-in dbt models.
Both launchers use dbt Core's Python API and the consuming project's dbt profile;
no passwords or connection credentials belong in the sample configuration files.

| Model | Materialization | Launcher | Sample configuration |
| --- | --- | --- | --- |
| `scout_agent` | `mat_scout_agt` | `scripts/scr_scout_agt.py` | `agent/snowflake.yml` |
| `scout_streamlit` | `mat_scout_stl` | `scripts/scr_scout_stl.py` | `streamlit/snowflake.yml` |

## Prerequisites

- Run Python from the environment containing dbt Core, dbt-snowflake, and PyYAML.
- Use a Snowflake target with an existing database, schema, and warehouse.
- Grant the deployment role USAGE on that database, schema, and warehouse.
- For agents, grant CREATE AGENT on the target schema and OWNERSHIP to replace an
  existing agent. Cortex must be available in the account, with the appropriate
  Cortex role and model access for the role that runs the agent.
- For Streamlit, grant CREATE STREAMLIT and CREATE STAGE on the target schema.
  Reusing a source stage requires READ and WRITE; replacing an app requires
  OWNERSHIP on that app. Publishing the live version also requires OWNERSHIP.
- Accept Snowflake's package terms if prompted when using Streamlit dependencies.

The launchers require dbt Core, not the standalone Fusion executable. The sample
agent uses no data tools, so it needs no source tables, semantic views, or search
services. Using it still invokes a model and can incur Cortex charges.

## Test From a Consuming Project

These PowerShell commands assume you are in `dbt_test` and have already declared
the sibling `dbt_scout` local package in `packages.yml`:

```yaml
packages:
  - local: ../dbt_scout
```

Refresh the package after changing its source files:

```powershell
dbt deps
```

Validate the sample inputs without connecting to Snowflake:

```powershell
python .\dbt_packages\dbt_scout\scripts\scr_scout_agt.py --config .\dbt_packages\dbt_scout\agent\snowflake.yml --dry-run
python .\dbt_packages\dbt_scout\scripts\scr_scout_stl.py --config .\dbt_packages\dbt_scout\streamlit\snowflake.yml --dry-run
```

Deploy both samples separately:

```powershell
python .\dbt_packages\dbt_scout\scripts\scr_scout_agt.py --config .\dbt_packages\dbt_scout\agent\snowflake.yml
python .\dbt_packages\dbt_scout\scripts\scr_scout_stl.py --config .\dbt_packages\dbt_scout\streamlit\snowflake.yml
```

Both scripts accept `--project-dir`, `--profiles-dir`, `--profile`, `--target`, and
`--select`. They invoke a narrowly selected `dbt build` internally. The default
selectors are `package:dbt_scout,scout_agent` and
`package:dbt_scout,scout_streamlit`, respectively.

To run directly from the package checkout with your `dbt_test` profile:

```powershell
python .\scripts\scr_scout_agt.py --config .\agent\snowflake.yml --profile dbt_test
python .\scripts\scr_scout_stl.py --config .\streamlit\snowflake.yml --profile dbt_test
```

Each sample is disabled unless its corresponding variable contains a mapping.
A normal `dbt build` without these variables does not deploy the examples.

## Agent Materialization

The launcher reads `snowflake.yml` and these two UTF-8 files next to it:

```text
agent/
  snowflake.yml
  assets/
    instructions_orchestration.md
    instructions_response.md
```

It inserts both Markdown texts into `specification.instructions`, validates the
configuration, and passes it through the `scout_agent` dbt variable.
The materialization runs `CREATE OR REPLACE AGENT ... COPY GRANTS FROM
SPECIFICATION`. Existing explicit grants are preserved except OWNERSHIP, which
belongs to the creating role. Replacing an agent replaces its specification;
maintain any production instructions and tools in the configuration, not only
in Snowsight.

| Setting | Meaning |
| --- | --- |
| `name` | Agent identifier; defaults to the dbt model identifier. Sample: `SCOUT_AGENT`. |
| `database`, `schema` | Optional exact destinations; otherwise the dbt model's resolved destination. |
| `comment` | Optional comment; a package documentation reference is always appended. |
| `profile` | Optional Snowflake agent display metadata; not a dbt connection profile. |
| `specification` | Snowflake agent settings: models, orchestration, tools, resources, and optional sample questions. |

Names are quoted and therefore case-sensitive. If overriding the database or
schema, use existing names exactly as stored in Snowflake. The specification is
limited to 100,000 bytes. Keep orchestration and response instructions in the
Markdown files, not duplicated in YAML. Tool resources must refer to declared
tool names. Snowflake validates the complete provider-specific specification.

### Sample Agent

The supplied agent is intentionally tool-free. After deploying, find
`SCOUT_AGENT` in Snowsight's Cortex Agents area and ask:

> What can you help me with? Do you have access to warehouse data?

It should explain its limitations rather than invent query results. This checks
agent creation and prompt loading, not analytical accuracy. Add real Cortex
Analyst or Cortex Search tools and their existing resources when moving beyond
this smoke test. Never place secrets in instructions, comments, or YAML.

## Streamlit Materialization

For the independently deployable, single-page comparison workbench, see
[Standalone Agent Comparison](compare_agents.md). It uses the same model,
launcher, and materialization with a container runtime and explicit artifact map.
The original sample below remains a warehouse-runtime deployment.

The Streamlit launcher reads `streamlit/snowflake.yml` and collects top-level
Python files plus `environment.yml` and `README.md` from `source_dir`. It does
not recursively upload directories, secrets, or arbitrary local files.

| Setting | Meaning |
| --- | --- |
| `name` | App identifier; defaults to the dbt model identifier. Sample: `SCOUT_STREAMLIT`. |
| `database`, `schema` | Optional existing destinations; default to the model's resolved destination. |
| `query_warehouse` | Existing warehouse; defaults to the dbt target's warehouse. |
| `title`, `comment` | App metadata; the comment always includes a documentation reference. |
| `source_dir` | Local source directory, relative to `snowflake.yml`; default: `app`. |
| `main_file` | Top-level Python filename; default: `streamlit_app.py`. |
| `artifacts` | Optional explicit relative file list preserving subdirectories; includes the entrypoint and exactly one runtime dependency manifest. |
| `runtime_name` | Optional Snowflake runtime; defaults to the warehouse runtime when omitted. |
| `compute_pool` | Existing approved compute pool for a container runtime. |
| `external_access_integrations` | Optional list of approved integrations for package/network access. |

The materialization:

1. Creates `<app_name>__SOURCE` as an internal stage if it does not exist.
2. Uploads the selected files using PUT without compression into a directory
   named for the dbt invocation. Each build has an isolated source directory.
3. Runs `CREATE OR REPLACE STREAMLIT ... FROM '<stage path>'` using the warehouse
   runtime and the packaged `environment.yml` dependencies.
4. Executes `ALTER STREAMLIT ... ADD LIVE VERSION FROM LAST` so the app is live.

The stage and app comments refer back to this runbook. Source files are copied
when the app is created: changing a local file does not update the deployed app
until you run the launcher again.

**Redeployment replaces the app and may discard grants and edits made in
Snowsight.** Unlike agent creation, CREATE STREAMLIT has no COPY GRANTS clause.
Reapply consumer grants after rebuilding. Old invocation directories remain in
the source stage for inspection; remove them according to your retention policy.
The materialization does not automatically drop source stages or old files.

### Sample Streamlit

Open `SCOUT_STREAMLIT` in Snowsight after deploying. The Runtime tab reports
the active database, schema, role, and warehouse using a read-only query.
The Sample Data tab contains a clearly labeled synthetic inventory with a filter
and chart. No real account inventory or table contents are queried. The sample
does not call the agent, so each deployment can be tested independently.

For a local, credential-free UI preview from the package directory:

```powershell
$env:SCOUT_LOCAL_PREVIEW = '1'
python -m streamlit run .\streamlit\app\streamlit_app.py
```

Local preview uses fixed context values and does not verify Snowflake deployment.
For a real Snowflake run, leave `SCOUT_LOCAL_PREVIEW` unset. Streamlit executes
with the app owner's rights by default; review that security model before adding
queries against real data or sharing the app with other roles.

## Verification

From the package checkout, run offline checks with dbt Core, Streamlit, and pandas
installed in the same Python environment:

```powershell
python -m unittest discover -s tests -p test_materializations.py -v
```

The tests load both sample configurations, check missing-entrypoint validation,
render escaped agent SQL, exercise the stage/upload/publish sequence with a mock
adapter, and run Streamlit's local preview and filter interaction with AppTest.
They do not prove Snowflake permissions, dependency availability, or provider SQL
acceptance; those require the two deployment commands above in your account.

After deploying, run these statements in a Snowflake worksheet in the target
database and schema:

```sql
show agents like 'SCOUT_AGENT';
show streamlits like 'SCOUT_STREAMLIT';
show stages like 'SCOUT_STREAMLIT__SOURCE';
```

You can also inspect `target/run_results.json` for the dbt build status.
Neither materialization creates a relation: do not use `ref()`, `dbt show`,
ordinary column tests, or table/view cleanup macros on these deployment models.
Use hooks for additional grants, not for querying the models as tables.

## Troubleshooting

- **No enabled nodes:** Run the appropriate Python launcher; it supplies the
  variables needed to enable the sample model. Check `--select` for typos.
- **File not found:** Config paths are relative to `--project-dir` (the current
  directory by default); source and instruction paths are relative to their YAML.
- **Source changes missing:** Run `dbt deps` in the consuming project to refresh
  its installed local package, then rerun the launcher.
- **PUT or stage errors:** Use dbt Core with the Snowflake adapter on a machine
  where the files exist; verify stage READ/WRITE privileges. A remote runner
  needs its own checkout of the source files.
- **App does not start:** Confirm warehouse USAGE, accepted package terms, and
  package availability in your Snowflake account. Check the Streamlit app logs.
- **Object already exists or insufficient privileges:** Confirm OWNERSHIP before
  replacement and use a dedicated development schema for these samples.
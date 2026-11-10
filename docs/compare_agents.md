# Standalone Agent Comparison

This workbench opens directly into **Compare Agents**. It has one page, two
independent conversations, historical evidence, deterministic comparisons, optional
AI judging, and explicit SQL previews. It creates no settings, evaluation, usage,
or conversation tables. Preferences and evidence are session-local and disappear
when the session ends. Download approved evidence before leaving the session.

## Distribution And Provisioning

The app is `streamlit/compare_agents/app/streamlit_app.py`. Its only local imports
are the comparison modules under `app/common/`. It does not import the original
Agent Evaluation entrypoint, navigation, table initialization, or reporting pages.

The existing `scout_streamlit` dbt model and `mat_scout_stl` materialization deploy
it using `scripts/scr_scout_stl.py`. There is no separate deployment framework.

1. `dbt deps` installs package source, macros, model, and deployment artifacts. It
   does **not** create a Streamlit app, compute pool, warehouse, role, or grants.
2. The package distributes an explicit source/artifact map in
   `streamlit/compare_agents/snowflake.yml`, a Python project and lockfile, and a
   generated `app/requirements.txt` beside the entrypoint.
3. An explicitly invoked launcher builds only `package:dbt_scout,scout_streamlit`.
   It creates/reuses an internal source stage, uploads the mapped files with PUT,
   creates/replaces the Streamlit object, and publishes its live version.
4. An administrator supplies the database/schema, owner role, compute pool,
   warehouse, package-network access integration, and authorized agent/data access.
   No application code creates roles or grants privileges.

The source stage and app comments refer to package documentation. Rebuilding
replaces the app and can discard viewer grants and edits made in Snowsight.
Reapply approved viewer grants after replacement. Source-stage invocation folders
are retained; apply an administrator-approved retention policy.

## Consumer Installation

Install the dbt package using your normal package source. For local development
with the sibling checkout, the consuming project's `packages.yml` contains:

```yaml
packages:
  - local: ../dbt_scout
```

Refresh packages through your repository's prescribed Task entry point. Do not
edit the generated copy inside `dbt_packages` as a permanent configuration store.

Copy the package's `streamlit/compare_agents/snowflake.yml` to the consuming
project root as `compare_agents.yml`. Retain its complete `artifacts` list.
Set these properties using objects supplied by your administrator:

```yaml
name: SCOUT_COMPARE_AGENTS
title: Compare Agents
source_dir: dbt_packages/dbt_scout/streamlit/compare_agents/app
main_file: streamlit_app.py
runtime_name: SYSTEM$ST_CONTAINER_RUNTIME_PY3_11
database: YOUR_APP_DATABASE
schema: YOUR_APP_SCHEMA
query_warehouse: YOUR_QUERY_WAREHOUSE
compute_pool: YOUR_COMPUTE_POOL
external_access_integrations:
  - YOUR_APPROVED_PACKAGE_ACCESS_INTEGRATION
# Retain the complete artifacts list from the packaged sample.
```

Names are quoted and case-sensitive; use the actual existing Snowflake names.
`source_dir` resolves relative to your configuration file. Credentials stay in
the consuming project's dbt profile, not in this YAML. The dbt profile must use
the approved deployment/app-owner role.

From the consuming project, validate the artifact map without querying Snowflake:

```powershell
task --taskfile .\dbt_packages\dbt_scout\Taskfile.yml compare:check PROJECT_DIR="$PWD" CONFIG=compare_agents.yml
```

After the owner explicitly approves remote deployment, the command is:

```powershell
task --taskfile .\dbt_packages\dbt_scout\Taskfile.yml compare:deploy PROJECT_DIR="$PWD" CONFIG=compare_agents.yml
```

The Python used by the deployment task must contain dbt Core, dbt-snowflake, and
PyYAML. This launcher does not use a standalone Fusion binary. No remote
deployment or access changes were performed during standalone implementation.

## Runtime And Artifact Mapping

The workbench targets Python 3.11 and the Snowflake **container runtime**, with
Streamlit >=1.57,<2. The generated runtime manifest and committed `uv.lock` record
the exact tested local dependency versions. The workbench uses session-scoped
`st.connection` resources and timer fragments; older Streamlit runtimes are not
silently supported. The original smoke-test app can still use its warehouse
runtime and separate `environment.yml`.

| Source relative to `app/` | Deployed destination |
| --- | --- |
| `streamlit_app.py` | app source root, configured as main file |
| `requirements.txt` | beside `streamlit_app.py` |
| `common/*.py` listed explicitly in `snowflake.yml` | matching `common/` directory |

The artifact loader rejects missing files, paths escaping the source directory,
unsupported files such as secrets, and a competing dependency manifest. It never
uploads `.env`, `.streamlit/secrets.toml`, the local virtual environment, test
files, or the synthetic browser harness. Do not add a nearer `pyproject.toml` or
`environment.yml` that could shadow the intended `requirements.txt`.

The app explicitly depends on Streamlit, pandas, Snowpark/connector, and sqlglot.
The former direct requests/OAuth Analyst transport is removed; requests can still
be installed transitively by Snowflake or Streamlit. There is no dbt dependency
inside the deployed app, no settings-table library, and no agent-evaluation pages.
Dependency generation and local operations run through Task:

```powershell
task compare:setup
task compare:dependencies
task compare:test
task compare:coverage
task compare:check
task materializations:test
task materializations:parse PROFILE=your_dbt_profile
```

These commands run from the package checkout. Rebuild the generated requirements
through Task after changing `streamlit/compare_agents/pyproject.toml`; commit the
project, lockfile, and runtime manifest together. Availability of these packages,
the container runtime, networking, and account restrictions must still be checked
in the consumer account.

## Access And Connections

The deployed default is **owner rights** through `st.connection("snowflake")`.
It does not fall back to caller rights, alternate credentials, OAuth tokens, or a
Snowpark builder. It does not automatically change secondary roles. Each viewer
session has physically isolated connections for discovery/main, left requests,
right requests, and SQL previews. The `ComparisonSnowflakeConnection.scope()`
and channel-specific cache keys preserve the reference implementation's access
and concurrency boundaries.

Agent pickers use live `SHOW AGENTS IN ACCOUNT`, not historical logs or settings.
Only agents visible to the **effective app-owner role** appear; this is not a
privilege bypass that reveals every account agent. Full quoted identifiers are
retained for requests, definitions, and evidence. Refresh available objects after
access changes; removed selections are reconciled without deleting old evidence.
The sidebar shows effective account/role/warehouse and clearly distinguishes
empty discovery from access failure.

Local developer SSO is an explicit separate mode. Set
`COMPARISON_CONNECTION_MODE=local` and each of `SNOWFLAKE_ACCOUNT`,
`SNOWFLAKE_USER`, `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`, and
`SNOWFLAKE_SCHEMA` before `task compare:preview`. The app labels this mode. A
missing value fails without trying another authentication mode. Do not deploy
local-mode configuration to the shared app.

## Least-Privilege Examples

The following is an **administrator-reviewed template**, not executed setup.
Substitute existing approved identifiers; do not grant account-wide privileges
merely to make discovery broader. Deployment ownership can be held by the same
dedicated role that owns the app. A separate CI deployment role may inherit that
dedicated owner role only when approved by the administrator; this app does not
automatically transfer ownership.

```sql
-- Dedicated app-owner/deployment role; use existing, approved objects.
grant usage on database <APP_DATABASE> to role <APP_OWNER_ROLE>;
grant usage on schema <APP_DATABASE>.<APP_SCHEMA> to role <APP_OWNER_ROLE>;
grant create streamlit, create stage on schema <APP_DATABASE>.<APP_SCHEMA>
  to role <APP_OWNER_ROLE>;
grant usage on warehouse <QUERY_WAREHOUSE> to role <APP_OWNER_ROLE>;
grant usage on compute pool <COMPUTE_POOL> to role <APP_OWNER_ROLE>;
grant usage on integration <PACKAGE_ACCESS_INTEGRATION> to role <APP_OWNER_ROLE>;

-- Reusing a stage created by another approved deployment requires explicit access.
grant read, write on stage <APP_DATABASE>.<APP_SCHEMA>.<SOURCE_STAGE>
  to role <APP_OWNER_ROLE>;

-- Selected agents and parents, not every agent in the account.
grant usage on database <AGENT_DATABASE> to role <APP_OWNER_ROLE>;
grant usage on schema <AGENT_DATABASE>.<AGENT_SCHEMA> to role <APP_OWNER_ROLE>;
grant usage on agent <AGENT_DATABASE>.<AGENT_SCHEMA>.<AGENT_NAME>
  to role <APP_OWNER_ROLE>;

-- Agent tools: grant only the underlying resources needed by those tools.
grant usage on database <DATA_DATABASE> to role <APP_OWNER_ROLE>;
grant usage on schema <DATA_DATABASE>.<DATA_SCHEMA> to role <APP_OWNER_ROLE>;
grant select on semantic view <DATA_DATABASE>.<DATA_SCHEMA>.<SEMANTIC_VIEW>
  to role <APP_OWNER_ROLE>;
grant select on table <DATA_DATABASE>.<DATA_SCHEMA>.<REQUIRED_TABLE>
  to role <APP_OWNER_ROLE>;
grant usage on cortex search service <DATA_DATABASE>.<DATA_SCHEMA>.<SEARCH_SERVICE>
  to role <APP_OWNER_ROLE>;

-- Approved viewers: parent access and app usage, not ownership or table setup.
grant usage on database <APP_DATABASE> to role <TEST_VIEWER_ROLE>;
grant usage on schema <APP_DATABASE>.<APP_SCHEMA> to role <TEST_VIEWER_ROLE>;
grant usage on streamlit <APP_DATABASE>.<APP_SCHEMA>.<APP_NAME>
  to role <TEST_VIEWER_ROLE>;
```

Review current Snowflake requirements for each configured agent/tool and grant
only the statements relevant to that configuration. Database USAGE and table
SELECT may also be needed on nested semantic-view sources. The app owner may
need access to referenced semantic-view definitions to capture full evidence;
missing referenced definitions are reported as unavailable. Discovery visibility
does not guarantee agent execution or permission to inspect every tool definition.

For Cortex inference and an optional AI judge, the administrator must grant the
app owner the appropriate Cortex database role, for example
`SNOWFLAKE.CORTEX_USER` where that broader role is approved, or the narrower
supported function-specific role. Account model allowlists, model RBAC, regional
availability and cross-region inference policies still apply. The app cannot
establish model availability merely because a model name was entered.

Optional telemetry uses exact query IDs with INFORMATION_SCHEMA query history
and SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY. Grant the relevant
monitoring visibility/Snowflake database role only if the administrator approves
it; do not grant ACCOUNTADMIN or blanket imported privileges just for metrics.
For optional dbt enrichment, grant parent USAGE and SELECT only on the configured
request report, not evaluation tables. Neither optional grant is required for
discovery, direct agent calls, or direct-response token/latency measurements.

**Owner-rights viewers exercise the app owner's privileges.** Limit app USAGE to
an approved testing audience and restrict the owner role's agent/data/warehouse
scope accordingly. A viewer's own worksheet role does not constrain those calls.
Agent execution, previews, judge calls, app compute, and SQL scans may incur cost.

## Conversations And Metrics

Each pane has its own selected agent, deployed sample questions, composer,
thread/conversation, background request, and historical answers. Selecting an
onboarding question only fills that pane's composer. The copy-question action
copies the other pane's composer; neither action submits a request.

Workers receive input copies, never Streamlit state. The UI merges completed
work. The timer fragment only detects completion and requests a full-app rerun;
reruns and sidebar actions never resubmit a question. There is no automatic retry
of a billable call. Only the busy pane's request-changing controls are disabled.
New chat resets that conversation but retains earlier evidence and metrics.

Metrics sit directly below Notes and limits in the sidebar. Each completed request
updates its own counts even while the other pane is busy. Changing either selected
agent resets **both** agents' metrics with a new pair epoch, not their chat history.
Late completions from a previous epoch remain evidence but cannot enter new totals.

Measured subtotals preserve missing/delayed/unavailable values separately from
actual zero. Token totals are never added to their components; Cortex and
warehouse credits remain separate; judge usage remains separate. Cumulative answer
latency is not warehouse execution time. SQL query IDs are deduplicated within
each agent's recorded history. Heatmaps compare only the same measure and unit.

Telemetry refresh is manual, exact-ID only, and never replays requests or SQL.
The app does not infer internal query attribution from text or timestamps.
Account usage can lag for hours, permissions can hide it, and query history can
omit older IDs: unavailable is not zero. Optional enrichment is enabled only with
`SCOUT_REQUEST_REPORT=DATABASE.SCHEMA.RELATION`, an uppercase validated identifier.
Its contract is one row per exact `request_id`, with `tokens` and `token_credits`.
Duplicate rows are rejected rather than summed. No default enterprise report is
assumed. Invalid/missing/inaccessible reporting does not block core comparisons.

## Optional AI Judge

The judge is a separate, explicitly billable Cortex COMPLETE call using the
reference request/response contract. It runs only when Generate AI insights is
pressed and never retries automatically. Enter an administrator-approved model;
there is no assumed default. `SCOUT_JUDGE_MODEL` can supply an approved preference
without changing either tested agent's orchestration model.

Submitted questions, answers, SQL, and definitions are untrusted evidence, not
instructions. Compact public evidence and definition hashes are the default.
Full definitions require explicit opt-in. The 40,000-character evidence limit
fails explicitly; nothing is silently truncated. Error details show the useful
service reason, query ID and SQLSTATE where exposed, not submitted evidence or
credentials. Error code 512513 alone does not establish a model/privilege cause.
Judge token reporting is separate; unavailable judge credits remain unknown.

## SQL Previews And Exports

SQL execution is optional and explicit. The reference sqlglot validator fails
closed on unsupported constructs, UDFs, table functions and non-SELECT statements.
A separate preview connection applies a 60-second statement timeout and returns
at most 1,000 rows. Query values are bound and object identities are safely quoted.
These checks are a restricted supported subset, **not a complete sandbox**.
Neither the row limit nor the timeout guarantees a cheap or cost-free scan.

Historical comparisons include questions/answers, execution context, SQL, exposed
query IDs, captured definitions/instructions/tools/VQR evidence and hashes,
differences, and available measurements. Missing verified-query evidence is not
proof no verified query was used. Different questions or histories are exploratory,
not controlled A/B comparisons or correctness winners.

Markdown/JSON exports omit result rows and private reasoning by default, retain
evidence and capture times, and do not export connection credentials. Questions,
answers and definitions may still be sensitive; follow your data-handling policy.

## Validation And Integration Status

The adapted reference tests mock external I/O, not metric calculations. They cover
single-page startup, live discovery without logs, sidebar/pane layout, concurrent
requests, no replay, onboarding/copy, history/snapshots, exact-ID metrics, missing
versus zero, pair epochs, New chat, optional report failure, judge contracts and
errors, preview validation, connection isolation, and complete mapped artifacts.

`task compare:demo` runs an explicitly synthetic browser-review harness with no
Snowflake or billable calls. The harness is not in the artifact map and cannot
silently replace production failures with demo data. `task compare:preview` runs
the real entrypoint with explicitly configured developer authentication.

Automated/AppTest results and synthetic browser screenshots are local evidence,
not proof of live discovery, agent/model availability, owner-rights behavior in a
deployed account, network access, package resolution, grants, or deployment success.
Those remain consumer-account integration checks. No grants, ownership changes,
persistent tables, remote objects, or billable requests were executed by this work.
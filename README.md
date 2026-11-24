# dbt-scout 🕵️‍♂️

**Find the tables and views in your Snowflake warehouse that your dbt project no longer builds.**

As a project grows, models get renamed, moved or deleted, and the objects they
created stay behind. `dbt-scout` lists those leftovers, shows whether anyone
still queries them, and gives you a `DROP` statement to review.

## What you get

The `scout_orphan_objects` model returns one row per orphan: a table, view,
materialized view, dynamic table or external table that sits in a schema your dbt
project builds into but is not in the current dbt graph (no model, seed or
snapshot builds it).

| Column | Meaning |
| --- | --- |
| `full_path`, `database_name`, `schema_name`, `object_name` | Where the object lives. |
| `object_type` | `table`, `view`, `materialized view`, `dynamic table` or `external table`. |
| `created_at`, `last_altered_at` | Snowflake timestamps. |
| `query_count_90d`, `last_accessed_at` | How often the object was queried in the past 90 days. |
| `cleanup_script` | A suggested `DROP` statement. **It is never run for you.** |

Only schemas that at least one of your models, seeds or snapshots builds into are
scanned. A partial run such as `--select` does not create false orphans. Semantic
views are not detected.

## Quick start

1. Add the package to `packages.yml`. Install it from GitHub:

   ```yaml
   packages:
     - git: https://github.com/hmamoun/dbt-scout.git
       revision: v0.1.2
   ```

   Or from the dbt Package Hub:

   ```yaml
   packages:
     - package: hmamoun/dbt_scout
       version: 0.1.2
   ```

   > **Note:** the package is being reviewed for the dbt Package Hub. Until it is
   > approved, use the GitHub install above.

2. Install it and build the report:

   ```bash
   dbt deps
   dbt run --select scout_orphan_objects
   ```

3. Read the result:

   ```sql
   select * from <your_database>.<your_schema>.scout_orphan_objects
   order by query_count_90d, created_at;
   ```

Objects with `query_count_90d = 0` are the safest candidates to review first.

### Requirements

- Snowflake. The model reads each database's `information_schema` and
  `snowflake.account_usage.access_history`, so the role that runs dbt needs access
  to both (for example the `GOVERNANCE_VIEWER` database role on the `SNOWFLAKE`
  database). Access history needs Snowflake Enterprise Edition or higher and can lag
  by a few hours.
- dbt with the `dbt-snowflake` adapter.

### Configuration

`dbt-scout` has no project variables to set. It works out its scope from your dbt
graph and the target database, and looks back 90 days in `access_history`. Build it
into a schema of your choice with the usual model config:

```yaml
# dbt_project.yml
models:
  dbt_scout:
    governance:
      +schema: governance
```

Requires dbt `>=1.6.0, <3.0.0`.

### Using it in your project

`scout_orphan_objects` is a table model, so you can run it on demand or on a schedule
with `dbt run --select scout_orphan_objects`. Two ways to build on it:

**Wrap it in your own model**, so the report is always rebuilt with your project:

```sql
-- models/governance/orphan_objects.sql
select * from {{ ref('dbt_scout', 'scout_orphan_objects') }}
```

**Add a test for CI/CD** that flags leftovers whenever the project runs:

```sql
-- tests/no_orphan_objects.sql
{{ config(severity='warn') }}

select * from {{ ref('dbt_scout', 'scout_orphan_objects') }}
```

```bash
dbt build --select scout_orphan_objects no_orphan_objects
```

The test reports a warning for every orphan. Remove the `severity` line to make it
fail the pipeline instead.

## Testing

`integration_tests/` is a small dbt project that builds a model, creates a leftover
table the graph does not know about, and asserts that `scout_orphan_objects` lists
the leftover but not the model. Run it against a Snowflake target that can read
`snowflake.account_usage` (set the `SNOWFLAKE_*` environment variables used in
`integration_tests/profiles.yml`):

```bash
cd integration_tests
dbt deps
dbt build
```

## License

See [LICENSE](LICENSE).

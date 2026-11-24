{{ config(materialized="table") }}

{#-
    Tables/views that exist in the schemas this project builds into but no longer
    correspond to any model, seed or snapshot in the dbt graph (renamed, moved or
    deleted models). One row per orphan, with a ready-to-run drop statement and
    90-day query usage from ACCESS_HISTORY.

    Scope: only (database, schema) pairs that at least one node resolves to, so
    schemas dbt does not own (workspace, RLS, ...) are never scanned. The graph is
    always complete, so a partial --select run does not create false orphans.
-#}
{%- set usage_lookback_days = 90 -%}
{%- set expected = [] -%}
{%- set databases = [target.database | upper] -%}

{%- if execute -%}
    {%- for node in graph.nodes.values() -%}
        {%- if node.resource_type in [
            "model",
            "seed",
            "snapshot",
        ] and node.config.materialized != "ephemeral" -%}
            {%- do expected.append(
                (
                    node.database | upper,
                    node.schema | upper,
                    node.alias | upper,
                )
            ) -%}
            {%- if node.database | upper not in databases -%}
                {%- do databases.append(node.database | upper) -%}
            {%- endif -%}
        {%- endif -%}
    {%- endfor -%}
{%- endif -%}

with

    expected as (
        select database_name, schema_name, object_name
        from
            (
                values
                    {% for db, sch, obj in expected -%}
                        ('{{ db }}', '{{ sch }}', '{{ obj }}'){{ "," if not loop.last }}
                    {% else -%} (null, null, null)
                    {% endfor %}
            ) as t(database_name, schema_name, object_name)
    ),

    scope as (select distinct database_name, schema_name from expected),

    actual as (
        {% for db in databases %}
            select
                table_catalog as database_name,
                table_schema as schema_name,
                table_name as object_name,
                case
                    when table_type = 'VIEW'
                    then 'view'
                    when table_type = 'MATERIALIZED VIEW'
                    then 'materialized view'
                    when table_type = 'EXTERNAL TABLE'
                    then 'external table'
                    when table_type = 'BASE TABLE' and is_dynamic = 'YES'
                    then 'dynamic table'
                    when table_type = 'BASE TABLE'
                    then 'table'
                end as object_type,
                created as created_at,
                last_altered as last_altered_at
            from {{ db }}.information_schema.tables
            where
                table_schema <> 'INFORMATION_SCHEMA' {{ "union all" if not loop.last }}
        {% endfor %}
    ),

    orphans as (
        select
            actual.database_name
            || '.'
            || actual.schema_name
            || '.'
            || actual.object_name as full_path,
            actual.*
        from actual
        inner join
            scope
            on actual.database_name = scope.database_name
            and actual.schema_name = scope.schema_name
        left join
            expected
            on actual.database_name = expected.database_name
            and actual.schema_name = expected.schema_name
            and upper(actual.object_name) = expected.object_name
        where expected.object_name is null
    ),

    -- Read access (direct or through views) per object over the lookback window.
    usage as (
        select
            upper(obj.value:"objectName"::string) as full_path,
            count(distinct ah.query_id) as query_count,
            max(ah.query_start_time) as last_accessed_at
        from
            snowflake.account_usage.access_history ah,
            lateral flatten(input => ah.base_objects_accessed) obj
        where
            ah.query_start_time
            >= dateadd('day', -{{ usage_lookback_days }}, current_timestamp())
            and split_part(upper(obj.value:"objectName"::string), '.', 1) in (
                {%- for db in databases %}
                    '{{ db }}'{{ "," if not loop.last }}
                {% endfor -%}
            )
        group by 1
    )

select
    o.full_path,
    o.database_name,
    o.schema_name,
    o.object_name,
    o.object_type,
    o.created_at,
    o.last_altered_at,
    coalesce(u.query_count, 0) as query_count_{{ usage_lookback_days }}d,
    u.last_accessed_at,
    case
        when o.object_type is null
        then '-- unsupported object type, drop manually: ' || o.full_path
        else
            'drop '
            || o.object_type
            || ' if exists "'
            || o.database_name
            || '"."'
            || o.schema_name
            || '"."'
            || o.object_name
            || '";'
    end as cleanup_script
from orphans o
left join usage u on u.full_path = upper(o.full_path)
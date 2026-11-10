{# Documentation: docs/materializations.md#streamlit-materialization #}
{% macro scout_streamlit_ddl(app, app_name, source_location, warehouse) %}
  create or replace streamlit {{ app_name }}
    from '{{ source_location | replace("'", "''") }}'
    main_file = '{{ app.get('main_file', 'streamlit_app.py') | replace("'", "''") }}'
    query_warehouse = {{ adapter.quote(warehouse) }}
    {% if app.get('runtime_name') %}
    runtime_name = '{{ app['runtime_name'] | replace("'", "''") }}'
    {% endif %}
    {% if app.get('compute_pool') %}
    compute_pool = {{ adapter.quote(app['compute_pool']) }}
    {% endif %}
    {% if app.get('external_access_integrations') %}
    external_access_integrations = (
      {% for integration in app['external_access_integrations'] %}
        {{ adapter.quote(integration) }}{{ ',' if not loop.last }}
      {% endfor %}
    )
    {% endif %}
    title = '{{ app.get('title', 'Scout Deployment Check') | replace('\\', '\\\\') | replace("'", "''") }}'
    comment = '{{ (app.get('comment', '') ~ ' Documentation: dbt_scout/docs/materializations.md#streamlit-materialization') | replace('\\', '\\\\') | replace("'", "''") }}'
{% endmacro %}

{% materialization mat_scout_stl, adapter='snowflake' %}
  {% set app = var('scout_streamlit', none) %}
  {% if app is not mapping or not app.get('source_files') %}
    {{ exceptions.raise_compiler_error('mat_scout_stl requires scout_streamlit.source_files. Use scripts/scr_scout_stl.py. See docs/materializations.md#streamlit-materialization.') }}
  {% endif %}
  {% set warehouse = app.get('query_warehouse', target.warehouse) %}
  {% if warehouse is not string or not warehouse | trim %}
    {{ exceptions.raise_compiler_error('Set query_warehouse or a warehouse in your dbt target. See docs/materializations.md#streamlit-materialization.') }}
  {% endif %}
  {% set identifiers = [] %}
  {% set parts = [app.get('database', this.database), app.get('schema', this.schema), app.get('name', this.identifier)] %}
  {% for part in parts %}
    {% if part is not string or not part | trim %}
      {{ exceptions.raise_compiler_error('Streamlit database, schema, and name must be nonempty strings. See docs/materializations.md.') }}
    {% endif %}
    {% do identifiers.append(adapter.quote(part)) %}
  {% endfor %}
  {% set app_name = identifiers | join('.') %}
  {% set stage_name = identifiers[:2] | join('.') ~ '.' ~ adapter.quote(parts[2] ~ '__SOURCE') %}
  {% set source_location = '@' ~ stage_name ~ '/' ~ invocation_id %}
  {{ run_hooks(pre_hooks, inside_transaction=False) }}
  {{ run_hooks(pre_hooks, inside_transaction=True) }}
  {% call statement('create_source_stage', auto_begin=False) %}
    create stage if not exists {{ stage_name }}
      comment = 'dbt_scout Streamlit source files. Documentation: dbt_scout/docs/materializations.md#streamlit-materialization'
  {% endcall %}
  {% for source in app['source_files'] %}
    {% set uri = source.get('uri') if source is mapping else source %}
    {% set directory = source.get('directory', '') if source is mapping else '' %}
    {% if uri is not string or not uri.startswith('file://') %}
      {{ exceptions.raise_compiler_error('source_files must contain local file URIs produced by scripts/scr_scout_stl.py. See docs/materializations.md.') }}
    {% endif %}
    {% if directory is not string or not modules.re.fullmatch('[A-Za-z0-9_/-]*', directory) or directory.startswith('/') %}
      {{ exceptions.raise_compiler_error('Invalid artifact destination directory. See docs/compare_agents.md.') }}
    {% endif %}
    {% call statement('upload_source_' ~ loop.index, auto_begin=False) %}
      put '{{ uri | replace("'", "''") }}'
        {{ source_location }}{{ '/' ~ directory if directory }} auto_compress = false overwrite = true
    {% endcall %}
  {% endfor %}
  {% call statement('main', auto_begin=False) %}
    {{ dbt_scout.scout_streamlit_ddl(app, app_name, source_location, warehouse) }}
  {% endcall %}
  {% call statement('publish_live_version', auto_begin=False) %}
    alter streamlit {{ app_name }} add live version from last
  {% endcall %}
  {{ run_hooks(post_hooks, inside_transaction=True) }}
  {% do adapter.commit() %}
  {{ run_hooks(post_hooks, inside_transaction=False) }}
  {{ log('Created Streamlit app ' ~ app_name ~ '. See dbt_scout/docs/materializations.md.', info=True) }}
  {{ return({'relations': []}) }}
{% endmaterialization %}
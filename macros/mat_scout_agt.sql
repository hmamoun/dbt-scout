{# Documentation: docs/materializations.md#agent-materialization #}
{% macro scout_agent_ddl(agent, agent_name) %}
  {% if agent is not mapping or agent.get('specification') is not mapping %}
    {{ exceptions.raise_compiler_error('mat_scout_agt requires a scout_agent variable with a specification mapping. Use scripts/scr_scout_agt.py to load the agent files.') }}
  {% endif %}
  {% set specification = tojson(agent['specification']) %}
  create or replace agent {{ agent_name }} copy grants
  comment = '{{ (agent.get('comment', '') ~ ' Documentation: dbt_scout/docs/materializations.md#agent-materialization') | replace('\\', '\\\\') | replace("'", "''") }}'
  {% if agent.get('profile') is not none %}
    profile = '{{ tojson(agent['profile']) | replace('\\', '\\\\') | replace("'", "''") }}'
  {% endif %}
  from specification
    '{{ specification | replace('\\', '\\\\') | replace("'", "''") }}'
{% endmacro %}

{% materialization mat_scout_agt, adapter='snowflake' %}
  {% set agent = var('scout_agent', none) %}
  {% if agent is not mapping %}
    {{ exceptions.raise_compiler_error('mat_scout_agt requires the scout_agent variable. Run scripts/scr_scout_agt.py from the installed dbt_scout package.') }}
  {% endif %}
  {% set identifiers = [] %}
  {% for part in [agent.get('database', this.database), agent.get('schema', this.schema), agent.get('name', this.identifier)] %}
    {% if part is not string or not part | trim %}
      {{ exceptions.raise_compiler_error('Agent database, schema, and name must be nonempty strings.') }}
    {% endif %}
    {% do identifiers.append(adapter.quote(part)) %}
  {% endfor %}
  {% set agent_name = identifiers | join('.') %}
  {% set agent_sql = dbt_scout.scout_agent_ddl(agent, agent_name) %}
  {{ run_hooks(pre_hooks, inside_transaction=False) }}
  {{ run_hooks(pre_hooks, inside_transaction=True) }}
  {% call statement('main', auto_begin=False) %}
    {{ agent_sql }}
  {% endcall %}
  {{ run_hooks(post_hooks, inside_transaction=True) }}
  {% do adapter.commit() %}
  {{ run_hooks(post_hooks, inside_transaction=False) }}
  {{ log('Created Cortex Agent ' ~ agent_name, info=True) }}
  {{ return({'relations': []}) }}
{% endmaterialization %}
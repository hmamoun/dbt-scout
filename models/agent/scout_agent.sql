{# Documentation: docs/materializations.md#agent-materialization #}
{{ config(
	materialized='mat_scout_agt',
	enabled=var('scout_agent', none) is mapping,
	meta={'documentation': 'docs/materializations.md#agent-materialization'}
) }}
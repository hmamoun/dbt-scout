{# Documentation: docs/materializations.md#streamlit-materialization #}
{{ config(
    materialized='mat_scout_stl',
    enabled=var('scout_streamlit', none) is mapping,
    meta={'documentation': 'docs/materializations.md#streamlit-materialization'}
) }}
-- Fails (returns rows) if a model that dbt builds is reported as an orphan.
select *
from {{ ref('dbt_scout', 'scout_orphan_objects') }}
where upper(object_name) = 'SCOUT_IT_KEPT_MODEL'

-- Fails (returns a row) if the leftover table is not reported as an orphan.
select 1 as failure
where
    not exists (
        select 1
        from {{ ref('dbt_scout', 'scout_orphan_objects') }}
        where upper(object_name) = 'SCOUT_IT_LEFTOVER'
    )

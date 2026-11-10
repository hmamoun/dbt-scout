{% docs scout_deployments %}
Opt-in Snowflake application deployment rather than a table or view. Use the
Python launchers to load local source files and pass validated settings to dbt.
See [the deployment runbook](docs/materializations.md) in the dbt_scout package
for sample commands, permissions, redeployment behavior, and troubleshooting.
These objects cannot be queried with ref(), dbt show, or ordinary column tests.
{% enddocs %}
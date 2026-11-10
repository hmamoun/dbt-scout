from unittest.mock import Mock

import pytest
from common import common, comparison_clients
from snowflake.connector import ProgrammingError


def test_discovery_uses_live_account_objects_and_does_not_share_cached_results(monkeypatch):
    connection = Mock()
    monkeypatch.setattr(common, "get_comparison_connections", lambda: (connection, Mock()))
    query = Mock(
        side_effect=[
            [
                {"database_name": "DB", "schema_name": "S", "name": name}
                for name in ("NEW_WITHOUT_LOGS", "OLD")
            ],
            [{"database_name": "OTHER", "schema_name": "S", "name": "RESTRICTED"}],
        ]
    )
    monkeypatch.setattr(comparison_clients, "query", query)
    assert [obj.fqn for obj in common.discover_agents()] == [
        '"DB"."S"."NEW_WITHOUT_LOGS"',
        '"DB"."S"."OLD"',
    ]
    assert [obj.fqn for obj in common.discover_agents()] == ['"OTHER"."S"."RESTRICTED"']
    assert query.call_count == 2
    for call in query.call_args_list:
        assert call.args == (connection.raw_connection, "SHOW AGENTS IN ACCOUNT")


def test_discovery_failure_is_not_an_empty_agent_list(monkeypatch):
    monkeypatch.setattr(common, "get_comparison_connections", lambda: (Mock(), Mock()))
    monkeypatch.setattr(
        comparison_clients, "query", Mock(side_effect=ProgrammingError("Discovery denied"))
    )
    with pytest.raises(ProgrammingError, match="Discovery denied"):
        common.discover_agents()


def test_discovery_distinguishes_empty_and_malformed_results(monkeypatch):
    monkeypatch.setattr(common, "get_comparison_connections", lambda: (Mock(), Mock()))
    query = Mock(side_effect=[[], [{"name": "A"}]])
    monkeypatch.setattr(comparison_clients, "query", query)
    assert common.discover_agents() == []
    with pytest.raises(ValueError, match="incomplete"):
        common.discover_agents()



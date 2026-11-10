"""Owner-rights, session-scoped connections. See docs/compare_agents.md."""

import os
from typing import Literal

import streamlit as st
from snowflake.connector import SnowflakeConnection as ConnectorConnection
from streamlit.connections import SnowflakeConnection

from . import comparison_clients
from .comparison import ObjectRef


class ComparisonSnowflakeConnection(SnowflakeConnection):
    """Isolate physical connections per viewer session and operation channel."""

    @classmethod
    def scope(cls) -> Literal["session"]:
        return "session"

    def _connect(self, *, comparison_channel: str, **kwargs) -> ConnectorConnection:
        return super()._connect(**kwargs)


def comparison_connection_mode() -> str:
    mode = os.environ.get("COMPARISON_CONNECTION_MODE", "owner")
    if mode not in ("owner", "local"):
        raise ValueError("COMPARISON_CONNECTION_MODE must be owner or local.")
    return mode


def get_comparison_connections(
    agent_side: str | None = None,
) -> tuple[ComparisonSnowflakeConnection, ComparisonSnowflakeConnection]:
    if agent_side not in (None, "left", "right"):
        raise ValueError("Agent side must be left or right.")
    suffix = f"-agent-{agent_side}" if agent_side else ""
    if comparison_connection_mode() == "local":
        parameters = {
            key: os.environ[f"SNOWFLAKE_{key.upper()}"]
            for key in ("account", "user", "role", "warehouse", "database", "schema")
        }

        def local_connection(name: str) -> ComparisonSnowflakeConnection:
            return st.connection(
                name, type=ComparisonSnowflakeConnection,
                comparison_channel=name, authenticator="externalbrowser",
                paramstyle="qmark", **parameters,
            )

        return local_connection(f"comparison-local{suffix}"), local_connection("comparison-local-preview")

    def owner_connection(channel: str) -> ComparisonSnowflakeConnection:
        return st.connection(
            "snowflake", type=ComparisonSnowflakeConnection, comparison_channel=channel,
        )

    return owner_connection(f"comparison-owner{suffix}"), owner_connection("comparison-owner-preview")


def discover_agents() -> list[ObjectRef]:
    connection, _ = get_comparison_connections()
    return comparison_clients.discover(connection.raw_connection, "agent")
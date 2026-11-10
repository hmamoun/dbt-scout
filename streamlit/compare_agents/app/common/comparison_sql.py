"""Fail-closed SQL subset for explicit comparison preview executions."""

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

ROW_LIMIT = 1000
TIMEOUT_SECONDS = 60

# No UDFs, table functions, external calls, AI functions or session/system functions.
_FUNCTIONS = {
    "ABS",
    "AVG",
    "CASE",
    "CAST",
    "CEIL",
    "COALESCE",
    "COUNT",
    "CURRENT_DATE",
    "CURRENT_TIMESTAMP",
    "DATE_ADD",
    "DATE_DIFF",
    "DATE_TRUNC",
    "EXTRACT",
    "FLOOR",
    "GREATEST",
    "IF",
    "LEAST",
    "LOWER",
    "MAX",
    "MIN",
    "NULLIF",
    "ROUND",
    "ROW_NUMBER",
    "SUM",
    "TIME_TO_STR",
    "TO_CHAR",
    "TO_DATE",
    "TRIM",
    "TRY_CAST",
    "UPPER",
}


def bounded_sql(statement: str) -> str:
    try:
        trees = sqlglot.parse(statement, read="snowflake")
    except (ParseError, TokenError) as exc:
        raise ValueError("SQL could not be validated as a supported Snowflake query.") from exc
    if len(trees) != 1 or not isinstance(trees[0], exp.Select):
        raise ValueError("Only one SELECT query (optionally with CTEs) can be previewed.")
    tree = trees[0]
    # Explicit node allowlist: unsupported Snowflake constructs fail closed.
    allowed_nodes = {
        "Select",
        "With",
        "CTE",
        "Subquery",
        "Table",
        "TableAlias",
        "Identifier",
        "Column",
        "Star",
        "Literal",
        "Null",
        "Boolean",
        "Alias",
        "From",
        "Join",
        "Where",
        "Group",
        "Having",
        "Qualify",
        "Order",
        "Ordered",
        "Limit",
        "Offset",
        "Distinct",
        "Paren",
        "Tuple",
        "Window",
        "WindowSpec",
        "DataType",
        "DataTypeParam",
        "Case",
        "When",
        "In",
        "Between",
        "Is",
        "Not",
        "And",
        "Or",
        "EQ",
        "NEQ",
        "GT",
        "GTE",
        "LT",
        "LTE",
        "Add",
        "Sub",
        "Mul",
        "Div",
        "Mod",
        "Neg",
        "Like",
        "ILike",
        "Interval",
    }
    for node in tree.walk():
        if isinstance(node, exp.Func):
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
            if name not in _FUNCTIONS:
                raise ValueError(f"Function {name} is not permitted in comparison previews.")
        elif type(node).__name__ not in allowed_nodes:
            raise ValueError(f"Unsupported SQL construct: {type(node).__name__}.")
        if isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
            raise ValueError("Table functions and dynamic relations cannot be previewed.")
        if isinstance(node, exp.Identifier) and not node.name:
            raise ValueError("Empty SQL identifier.")
    # Table/column qualification is allowed, but function qualification (including
    # user-defined functions shadowing builtins) introduces a Dot node and is rejected.
    original = tree.sql(dialect="snowflake")
    return f"SELECT * FROM ({original}) AS comparison_preview LIMIT {ROW_LIMIT}"

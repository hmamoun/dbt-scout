"""Deploy local agent instructions. See ../docs/materializations.md#agent-materialization."""

import argparse
import json
from pathlib import Path
import sys

import yaml


def load_agent(config_path):
    agent = yaml.safe_load(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(agent, dict):
        raise ValueError("snowflake.yml must contain a YAML mapping.")
    allowed_keys = {"name", "database", "schema", "comment", "profile", "specification"}
    unexpected = set(agent) - allowed_keys
    if unexpected:
        raise ValueError(f"Unknown agent settings: {', '.join(map(str, unexpected))}")
    for key in ("name", "database", "schema"):
        if key in agent and (not isinstance(agent[key], str) or not agent[key].strip()):
            raise ValueError(f"{key} must be a nonempty string.")
    if "comment" in agent and not isinstance(agent["comment"], str):
        raise ValueError("comment must be a string.")
    if "profile" in agent and not isinstance(agent["profile"], dict):
        raise ValueError("profile must be a mapping.")
    specification = agent.get("specification")
    if not isinstance(specification, dict):
        raise ValueError("specification must be a mapping of Snowflake agent settings.")
    instructions = specification.setdefault("instructions", {})
    if not isinstance(instructions, dict):
        raise ValueError("specification.instructions must be a mapping.")
    for instruction in ("orchestration", "response"):
        if instruction in instructions:
            raise ValueError(f"Put {instruction} instructions in assets/instructions_{instruction}.md, not YAML.")
        path = config_path.parent / "assets" / f"instructions_{instruction}.md"
        text = path.read_text(encoding="utf-8-sig")
        if not text.strip():
            raise ValueError(f"Instruction file is empty: {path}")
        instructions[instruction] = text
    tools = specification.get("tools", [])
    resources = specification.get("tool_resources", {})
    if not isinstance(tools, list) or not isinstance(resources, dict):
        raise ValueError("tools must be a list and tool_resources must be a mapping.")
    tool_names = set()
    for tool in tools:
        tool_spec = tool.get("tool_spec") if isinstance(tool, dict) else None
        if not isinstance(tool_spec, dict):
            raise ValueError("Each tool must contain a tool_spec mapping.")
        name = tool_spec.get("name")
        if not isinstance(name, str) or not name.strip() or name in tool_names:
            raise ValueError("Each tool must have a unique, nonempty tool_spec.name.")
        if not isinstance(tool_spec.get("type"), str) or not tool_spec["type"].strip():
            raise ValueError(f"Tool {name} must have a tool_spec.type.")
        tool_names.add(name)
    if set(resources) - tool_names:
        raise ValueError("Every tool_resources key must match a tool_spec.name.")
    if len(json.dumps(specification).encode("utf-8")) > 100_000:
        raise ValueError("The agent specification exceeds Snowflake's 100,000-byte limit.")
    return agent


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build a Snowflake Cortex Agent through dbt_scout. See docs/materializations.md.")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=Path("snowflake.yml"))
    parser.add_argument("--select", default="package:dbt_scout,scout_agent")
    parser.add_argument("--profiles-dir", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--target")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print the payload without running dbt.")
    args = parser.parse_args(argv)
    project_dir = args.project_dir.resolve()
    config_path = (project_dir / args.config).resolve()
    try:
        agent = load_agent(config_path)
        variables = json.dumps({"scout_agent": agent})
    except (OSError, ValueError, TypeError, yaml.YAMLError) as error:
        parser.error(str(error))
    if args.dry_run:
        print(variables)
        return 0
    from dbt.cli.main import dbtRunner

    command = [
        "build", "--project-dir", str(project_dir), "--select", args.select,
        "--vars", variables, "--quiet",
        "--warn-error-options", json.dumps({"error": ["NoNodesForSelectionCriteria"]}),
    ]
    for option in ("profiles_dir", "profile", "target"):
        value = getattr(args, option)
        if value is not None:
            command.extend(["--" + option.replace("_", "-"), str(value)])
    result = dbtRunner().invoke(command)
    if result.exception:
        print(str(result.exception), file=sys.stderr)
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
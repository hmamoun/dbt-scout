"""Deploy local Streamlit files. See ../docs/materializations.md#streamlit-materialization."""

import argparse
import json
from pathlib import Path
import sys

import yaml


def load_streamlit(config_path):
    app = yaml.safe_load(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(app, dict):
        raise ValueError("snowflake.yml must contain a YAML mapping.")
    allowed = {
        "name", "database", "schema", "query_warehouse", "title", "comment", "main_file", "source_dir",
        "artifacts", "runtime_name", "compute_pool", "external_access_integrations",
    }
    if set(app) - allowed:
        raise ValueError(f"Unknown Streamlit settings: {', '.join(map(str, set(app) - allowed))}")
    for key, value in app.items():
        if key in {"artifacts", "external_access_integrations"}:
            if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                raise ValueError(f"{key} must be a list of nonempty strings.")
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a nonempty string.")
    source_dir = (config_path.parent / app.pop("source_dir", "app")).resolve()
    main_file = app.setdefault("main_file", "streamlit_app.py")
    if Path(main_file).name != main_file or not main_file.endswith(".py"):
        raise ValueError("main_file must be a Python filename, not a path.")
    if not (source_dir / main_file).is_file():
        raise ValueError(f"Streamlit entrypoint not found: {source_dir / main_file}")
    sources = []
    artifacts = app.pop("artifacts", None)
    if artifacts is not None:
        if main_file not in artifacts:
            raise ValueError("artifacts must include main_file.")
        manifest = "requirements.txt" if app.get("runtime_name", "").startswith("SYSTEM$ST_CONTAINER") else "environment.yml"
        if manifest not in artifacts:
            raise ValueError(f"artifacts must include {manifest} beside main_file.")
        forbidden = "environment.yml" if manifest == "requirements.txt" else "requirements.txt"
        if forbidden in artifacts or "pyproject.toml" in artifacts:
            raise ValueError("Deploy only the intended runtime dependency manifest beside main_file.")
        for relative in artifacts:
            path = (source_dir / relative).resolve()
            if not path.is_relative_to(source_dir) or not path.is_file():
                raise ValueError(f"Invalid or missing artifact: {relative}")
            if path.suffix not in {".py", ".md"} and path.name != manifest:
                raise ValueError(f"Unsupported deployment artifact: {relative}")
            directory = path.parent.relative_to(source_dir).as_posix()
            sources.append({"uri": "file://" + path.as_posix(), "directory": "" if directory == "." else directory})
    else:
        for path in sorted(source_dir.iterdir()):
            if path.is_file() and (path.suffix == ".py" or path.name in {"environment.yml", "README.md"}):
                sources.append("file://" + path.resolve().as_posix())
    app["source_files"] = sources
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Deploy a Streamlit app through dbt_scout. See docs/materializations.md.")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=Path("streamlit/snowflake.yml"))
    parser.add_argument("--select", default="package:dbt_scout,scout_streamlit")
    parser.add_argument("--profiles-dir", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--target")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print the payload without running dbt.")
    args = parser.parse_args(argv)
    project_dir = args.project_dir.resolve()
    try:
        app = load_streamlit((project_dir / args.config).resolve())
        variables = json.dumps({"scout_streamlit": app})
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
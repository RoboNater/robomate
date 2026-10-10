#!/usr/bin/env python3
"""Clone a dedicated hub checkout and prepare a standard run (before MVP M2).

TOML keys use underscores; CLI options use hyphens. CLI values override the
config file. Config generation never clones a repository or contacts a hub.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS: dict[str, str | int | bool] = {
    "repository": "",
    "run_parent_dir": "~/working",
    "run_dir": "",
    "issue": 0,
    "work_file": "",
    "account": "",
    "roadmap": "",
    "forge": "github",
    "alice_harness": "claude",
    "alice_model": "",
    "alice_effort": "",
    "bob_harness": "claude",
    "bob_model": "",
    "bob_effort": "",
    "charlie_harness": "codex",
    "charlie_model": "",
    "charlie_effort": "",
    "auto_start": True,
}
PATH_KEYS = ("run_parent_dir", "work_file")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", type=Path, help="flat TOML config file")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--generate-default-config", type=Path, metavar="PATH")
    mode.add_argument(
        "--generate-config",
        type=Path,
        metavar="PATH",
        help="write merged defaults, config and CLI values instead of preparing",
    )
    result.add_argument(
        "--yes", action="store_true", help="skip the hub-start prompt (hub must already be running)"
    )
    work = result.add_mutually_exclusive_group()
    for key, default in DEFAULTS.items():
        flag = "--" + key.replace("_", "-")
        group = work if key in ("issue", "work_file") else result
        if isinstance(default, bool):
            group.add_argument(flag, action=argparse.BooleanOptionalAction, default=None)
        else:
            group.add_argument(flag, type=int if isinstance(default, int) else str, default=None)
    return result


def canonical_path(value: str, base: Path) -> str:
    path = Path(value).expanduser()
    return str((base / path).resolve())


def settings(args: argparse.Namespace) -> dict[str, Any]:
    values: dict[str, Any] = dict(DEFAULTS)
    if args.config:
        config = args.config.expanduser().resolve()
        with config.open("rb") as stream:
            loaded = tomllib.load(stream)
        if unknown := loaded.keys() - DEFAULTS.keys():
            raise ValueError(f"unknown config keys: {', '.join(sorted(unknown))}")
        for key, value in loaded.items():
            if type(value) is not type(DEFAULTS[key]):
                raise ValueError(f"{key} must be a {type(DEFAULTS[key]).__name__}")
            if key in PATH_KEYS and value:
                value = canonical_path(value, config.parent)
            if key == "repository" and value and (config.parent / value).is_dir():
                value = canonical_path(value, config.parent)
            # An absolute run_dir is portable even when the config is moved.
            if key == "run_dir" and value and Path(value).expanduser().is_absolute():
                value = canonical_path(value, config.parent)
            values[key] = value
    for key in DEFAULTS:
        value = getattr(args, key)
        if value is not None:
            values[key] = value
    if values["repository"] and Path(values["repository"]).expanduser().is_dir():
        values["repository"] = canonical_path(values["repository"], Path.cwd())
    # Selecting work on the CLI replaces the config's work selection.
    if args.issue is not None:
        values["work_file"] = ""
    if args.work_file is not None:
        values["issue"] = 0
    for key in PATH_KEYS:
        if values[key]:
            values[key] = canonical_path(values[key], Path.cwd())
    if values["run_dir"]:
        values["run_dir"] = canonical_path(values["run_dir"], Path(values["run_parent_dir"]))
    validate(values, preparing=False)
    return values


def validate(values: dict[str, Any], *, preparing: bool) -> None:
    if values["issue"] < 0:
        raise ValueError("issue must be positive (0 means unset)")
    if values["issue"] and values["work_file"]:
        raise ValueError("choose either issue or work_file")
    if values["forge"] not in ("github", "gitlab"):
        raise ValueError("forge must be github or gitlab")
    for name in ("alice", "bob", "charlie"):
        if values[f"{name}_harness"].lower() not in (
            "claude",
            "claude-code",
            "codex",
            "opencode",
            "antigravity",
            "agy",
        ):
            raise ValueError(f"invalid {name}_harness")
    if not preparing:
        return
    for key in ("repository", "run_dir", "account"):
        if not values[key].strip():
            raise ValueError(f"{key} is required")
    if not (values["issue"] or values["work_file"]):
        raise ValueError("choose --issue or --work-file (or set it in the config)")
    run_dir = Path(values["run_dir"])
    if run_dir == ROOT or ROOT in run_dir.parents:
        raise ValueError("run_dir must be outside the robomate checkout")
    if values["work_file"]:
        Path(values["work_file"]).read_text(encoding="utf-8")


def write_config(path: Path, values: dict[str, Any]) -> None:
    """Create a flat TOML file; refuse to overwrite an existing file."""
    lines = ["# Standard run settings. Empty strings / issue = 0 mean unset."]
    for key, value in values.items():
        literal = (
            str(value).lower()
            if type(value) in (int, bool)
            else json.dumps(value, ensure_ascii=False)
        )
        lines.append(f"{key} = {literal}")
    with path.expanduser().open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    print(f"Wrote {path}")


def hub_start_command(hub: Path, forge: str) -> str:
    if os.name == "nt":

        def quote(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        return (
            f"Set-Location -LiteralPath {quote(str(hub))}; "
            f"uv run --locked --project {quote(str(ROOT))} robomate up --forge {forge}"
        )
    return (
        f"cd {shlex.quote(str(hub))} && "
        f"uv run --locked --project {shlex.quote(str(ROOT))} robomate up --forge {forge}"
    )


def prepare(values: dict[str, Any], *, yes: bool) -> None:
    validate(values, preparing=True)
    run_dir = Path(values["run_dir"])
    hub = run_dir / "hub"
    if hub.exists():
        origin = subprocess.run(
            ["git", "-C", str(hub), "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            encoding="utf-8",
        ).stdout.strip()
        if origin != values["repository"]:
            raise ValueError(f"existing hub origin {origin!r} differs from repository")
        print(f"Reusing hub checkout {hub}")
    else:
        run_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--", values["repository"], str(hub)], check=True)
    print("Start the hub in a separate terminal and keep it running:", flush=True)
    print(hub_start_command(hub, values["forge"]), flush=True)
    if not yes:
        input("Press Enter once the hub is running, or Ctrl-C to abort: ")
    command = [
        sys.executable,
        str(ROOT / "scripts/prepare-run.py"),
        "--hub-repo",
        str(hub),
        "--repository",
        values["repository"],
        "--run-dir",
        str(run_dir),
        "--account",
        values["account"],
    ]
    if values["issue"]:
        command += ["--issue", str(values["issue"])]
    else:
        command += ["--work-file", values["work_file"]]
    if values["roadmap"]:
        command += ["--roadmap", values["roadmap"]]
    for name in ("alice", "bob", "charlie"):
        for option in ("harness", "model", "effort"):
            if value := values[f"{name}_{option}"]:
                command += [f"--{name}-{option}", value]
    if not values["auto_start"]:
        command.append("--no-auto-start")
    subprocess.run(command, check=True, cwd=ROOT)


def main(argv: list[str] | None = None) -> None:
    cli = parser()
    args = cli.parse_args(argv)
    try:
        if args.generate_default_config:
            write_config(args.generate_default_config, DEFAULTS)
        else:
            values = settings(args)
            if args.generate_config:
                write_config(args.generate_config, values)
            else:
                prepare(values, yes=args.yes)
    except (OSError, ValueError, subprocess.CalledProcessError, EOFError) as exc:
        cli.exit(1, f"prep-standard-run-area: error: {exc}\n")
    except KeyboardInterrupt:
        cli.exit(130, "\nPreparation interrupted; any cloned hub checkout is retained.\n")


if __name__ == "__main__":
    main()

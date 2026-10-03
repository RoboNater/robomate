"""Read-only verification of the issue-100 snapshot; requires gh, glab, and Git."""

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
HOST = "gitlab-box.local"


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True)


def gitlab(path: str) -> Any:
    result = json.loads(command("glab", "api", "--hostname", HOST, path))
    if isinstance(result, dict) and ("error" in result or "message" in result):
        raise RuntimeError(f"GitLab API error for {path}")
    return result


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def verify() -> None:
    inventory = json.loads((HERE / "inventory.json").read_text())
    pending_pipelines = []
    # Keep the temporary clones inside this checkout's configured workspace.
    git_dir = Path(command("git", "rev-parse", "--absolute-git-dir").strip())
    with tempfile.TemporaryDirectory(prefix="issue-100-verify-", dir=git_dir) as tmp:
        for name, record in inventory.items():
            advertised = command("git", "ls-remote", f"git@github.com:RoboNater/{name}.git")
            live_refs = {
                ref: sha
                for sha, ref in (line.split() for line in advertised.splitlines())
                if ref.startswith(("refs/heads/", "refs/tags/", "refs/pull/"))
                and not ref.endswith("^{}")
            }
            require(live_refs == record["source_refs"], f"{name}: source refs changed")
            pages = json.loads(
                command(
                    "gh",
                    "api",
                    "--paginate",
                    "--slurp",
                    f"repos/RoboNater/{name}/issues?state=open&per_page=100",
                )
            )
            open_numbers = {
                issue["number"] for page in pages for issue in page if "pull_request" not in issue
            }
            require(
                open_numbers == {i["source_number"] for i in record["issue_map"]},
                f"{name}: source open issues changed",
            )
            project = gitlab(f"projects/{record['id']}")
            require(project["web_url"] == record["url"], f"{name}: wrong project")
            require(project["visibility"] == record["visibility"], f"{name}: visibility")
            require(project["default_branch"] == "main", f"{name}: default branch")
            require(not project["auto_devops_enabled"], f"{name}: Auto DevOps")
            clone = str(Path(tmp) / f"{name}.git")
            command("git", "clone", "--mirror", f"git@{HOST}:RoboNater/{name}.git", clone)
            command("git", f"--git-dir={clone}", "fsck", "--full")
            for ref, sha in record["source_refs"].items():
                target = ref.replace("refs/pull/", "refs/heads/github-pull/")
                actual = command("git", f"--git-dir={clone}", "rev-parse", target).strip()
                require(actual == sha, f"{name}: changed snapshot ref {target}")
            source_commits = set(
                command(
                    "git",
                    f"--git-dir={clone}",
                    "rev-list",
                    *record["source_refs"].values(),
                ).splitlines()
            )
            require(len(source_commits) == record["commit_count"], f"{name}: history")
            for mapping in record["issue_map"]:
                source = json.loads(
                    command(
                        "gh",
                        "api",
                        f"repos/RoboNater/{name}/issues/{mapping['source_number']}",
                    )
                )
                dest = gitlab(f"projects/{record['id']}/issues/{mapping['iid']}")
                expected = (
                    f"Mirrored from {source['html_url']}. "
                    f"Original author: @{source['user']['login']}.\n\n---\n\n"
                    + (source["body"] or "")
                )
                require(dest["title"] == source["title"], f"{name}: issue title")
                require(
                    dest["description"].rstrip() == expected.rstrip(),
                    f"{name}: issue body {mapping['iid']}",
                )
                require(dest["state"] == "opened", f"{name}: issue state")
                require(
                    source["comments"] == len(mapping.get("comments", [])),
                    f"{name}: source comments changed",
                )
                for comment in mapping.get("comments", []):
                    original = json.loads(
                        command(
                            "gh",
                            "api",
                            f"repos/RoboNater/{name}/issues/comments/{comment['source_id']}",
                        )
                    )
                    note = gitlab(
                        f"projects/{record['id']}/issues/{mapping['iid']}/notes/{comment['note_id']}"
                    )
                    expected_note = (
                        f"Mirrored from {original['html_url']}. "
                        f"Original author: @{original['user']['login']}; "
                        f"created {original['created_at']}.\n\n---\n\n" + original["body"]
                    )
                    require(
                        note["body"].rstrip() == expected_note.rstrip(), f"{name}: comment body"
                    )
            if "ci_sha" in record:
                require(project["ci_config_path"] == record["ci_config_path"], f"{name}: CI pin")
                published = command(
                    "git",
                    f"--git-dir={clone}",
                    "show",
                    f"{record['ci_sha']}:.gitlab-ci.yml",
                )
                require(
                    published == (HERE / f"{name}.gitlab-ci.yml").read_text(),
                    f"{name}: published CI differs",
                )
                pipeline = gitlab(f"projects/{record['id']}/pipelines/{record['pipeline']['id']}")
                require(pipeline["sha"] == record["main_sha"], f"{name}: pipeline SHA")
                print(f"{name}: main pipeline {pipeline['id']} {pipeline['status']}")
                if pipeline["status"] != "success":
                    pending_pipelines.append(name)
            else:
                print(f"{name}: no source workflows, no CI added")
            print(
                f"{name}: {len(record['source_refs'])} refs, "
                f"{len(source_commits)} commits, {len(record['issue_map'])} issues verified"
            )
    require(not pending_pipelines, f"Main pipelines not green: {pending_pipelines}")


if __name__ == "__main__":
    verify()

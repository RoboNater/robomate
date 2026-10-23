#!/usr/bin/env python3
"""Seed a fresh GitLab project on the eval forge from one case file.

    uv run --locked python scripts/rbm-bench/seed.py CASE.json --name eval-refix-310-x

The project gets only what the original agents had when the work started:

- **Git:** the case's base commit as ``main``, fetched alone into an empty
  repository so no later object comes along; a re-review case also gets the
  PR branch up to the reviewed head.
- **Issues:** the target issue and every issue the case's text or the base's
  ``AGENTS.md`` names, created with their GitHub numbers (``iid``), bodies and
  comments as they stood at the case's cutoff, closed if they were closed by
  then. Other numbers simply do not exist.
- **MR (re-review):** the PR at the reviewed head, with its description and
  the comments posted before the review.

GitHub links to the repository are rewritten to the project, so nothing points
back at github.com. Prints the seed record as JSON.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

HOST = "gitlab-box.local"
NAMESPACE = "RoboNater"
GITHUB_REPO = "RoboNater/robomate"
DEFAULT_SOURCE = Path.home() / "robomate-eval" / "source.git"

PROJECT_SETTINGS = {
    "visibility": "private",
    "initialize_with_readme": False,
    "merge_method": "merge",
    "squash_option": "default_on",
    "only_allow_merge_if_pipeline_succeeds": False,
    "remove_source_branch_after_merge": True,
    "auto_devops_enabled": False,
    "wiki_access_level": "disabled",
    "snippets_access_level": "disabled",
}


class GitLabError(RuntimeError):
    pass


def glab(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """One GitLab API call through glab; a JSON ``message``/``error`` reply raises."""
    command = ["glab", "api", "--hostname", HOST, "-X", method, path]
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8") as payload:
        if body is not None:
            json.dump(body, payload)
            payload.flush()
            command += ["--input", payload.name, "-H", "Content-Type: application/json"]
        result = subprocess.run(command, capture_output=True, text=True)
    text = result.stdout.strip()
    data = json.loads(text) if text else None
    if result.returncode != 0 or (
        isinstance(data, dict) and ("error" in data or str(data.get("message", ""))[:1].isdigit())
    ):
        raise GitLabError(
            f"{method} {path}: {result.returncode} {text[:300]} {result.stderr[:300]}"
        )
    return data


def rewriter(web_url: str, pr: int | None) -> Any:
    """A function rewriting GitHub links to the repository into eval-project links."""
    base = f"https://github.com/{GITHUB_REPO}"

    def pull(match: re.Match[str]) -> str:
        number = int(match.group(1))
        return f"{web_url}/-/merge_requests/1" if number == pr else f"PR #{number}"

    def rewrite(text: str) -> str:
        text = re.sub(re.escape(base) + r"/pull/(\d+)[^\s)\]>]*", pull, text)
        text = re.sub(re.escape(base) + r"/issues/(\d+)", rf"{web_url}/-/issues/\1", text)
        text = re.sub(re.escape(base) + r"/(blob|tree|commit)/", rf"{web_url}/-/\1/", text)
        text = text.replace(base, web_url)
        return re.sub(re.escape(GITHUB_REPO) + r"#(\d+)", r"#\1", text)

    return rewrite


def attributed(comment: dict[str, Any], rewrite: Any) -> str:
    author = (comment.get("author") or {}).get("login", "unknown")
    return f"_Posted by **{author}** on {comment['createdAt']}:_\n\n{rewrite(comment['body'])}"


def push_snapshot(source: Path, ssh_url: str, refs: dict[str, str]) -> None:
    """Push ``refs`` (branch -> commit) from ``source`` through an empty repository.

    Fetching each commit into a new repository brings only the objects
    reachable from it, so nothing newer than the case can be pushed.
    """
    with tempfile.TemporaryDirectory(prefix="eval-seed-") as tmp:
        repo = Path(tmp) / "repo.git"
        subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
        specs = [f"{sha}:refs/heads/{branch}" for branch, sha in refs.items()]
        subprocess.run(["git", "-C", str(repo), "fetch", "-q", str(source), *specs], check=True)
        newest = subprocess.run(
            ["git", "-C", str(repo), "rev-list", "--all", "--max-count=1", "--date-order"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if newest not in refs.values():
            raise RuntimeError(f"snapshot holds a commit newer than the case refs: {newest}")
        subprocess.run(
            ["git", "-C", str(repo), "push", "-q", ssh_url, *(f"refs/heads/{b}" for b in refs)],
            check=True,
        )


def create_issue(project: str, number: int, issue: dict[str, Any], rewrite: Any) -> None:
    glab(
        "POST",
        f"projects/{project}/issues",
        {
            "iid": number,
            "title": issue["title"],
            "description": rewrite(issue["body"]),
            **({"created_at": issue["created_at"]} if issue.get("created_at") else {}),
        },
    )
    for comment in issue.get("comments", []):
        glab(
            "POST",
            f"projects/{project}/issues/{number}/notes",
            {"body": attributed(comment, rewrite), "created_at": comment["createdAt"]},
        )
    if issue.get("closed"):
        glab("PUT", f"projects/{project}/issues/{number}", {"state_event": "close"})


def seed(case: dict[str, Any], name: str, source: Path) -> dict[str, Any]:
    created = glab("POST", "projects", {"name": name, "path": name, **PROJECT_SETTINGS})
    project = quote(created["path_with_namespace"], safe="")
    web_url = created["web_url"]
    ssh_url = created["ssh_url_to_repo"]
    rewrite = rewriter(web_url, case.get("pr") if case["kind"] == "review" else None)
    refs = {"main": case["base"]}
    branch = None
    if case["kind"] == "review":
        branch = f"pr-{case['pr']}"
        refs[branch] = case["head"]
    push_snapshot(source, ssh_url, refs)
    record: dict[str, Any] = {
        "case": case["id"],
        "project": created["path_with_namespace"],
        "project_id": created["id"],
        "web_url": web_url,
        "ssh_url": ssh_url,
        "base": case["base"],
    }
    issues = dict(case["context_issues"])
    if case.get("issue"):
        issues[str(case["issue"]["number"])] = {**case["issue"], "closed": False}
    for number in sorted(issues, key=int):
        create_issue(project, int(number), issues[number], rewrite)
    record["issues"] = sorted(int(n) for n in issues)
    if case["kind"] == "review":
        assert branch is not None
        mr = glab(
            "POST",
            f"projects/{project}/merge_requests",
            {
                "source_branch": branch,
                "target_branch": "main",
                "title": case["pr_title"],
                "description": rewrite(case["pr_body"]),
                "remove_source_branch": True,
                "squash": True,
            },
        )
        for comment in case.get("pr_comments", []):
            glab(
                "POST",
                f"projects/{project}/merge_requests/{mr['iid']}/notes",
                {"body": attributed(comment, rewrite), "created_at": comment["createdAt"]},
            )
        record |= {"mr_iid": mr["iid"], "mr_url": mr["web_url"], "head": case["head"]}
    return record


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("case", type=Path)
    parser.add_argument("--name", required=True, help="new project path under RoboNater/")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    args = parser.parse_args(argv)
    case = json.loads(args.case.read_text(encoding="utf-8"))
    print(json.dumps(seed(case, args.name, args.source), indent=1))


if __name__ == "__main__":
    main()

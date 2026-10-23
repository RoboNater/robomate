#!/usr/bin/env python3
"""Mine robomate's merged PRs into eval cases (re-fix and re-review).

    uv run --locked python scripts/rbm-bench/cases.py mirror        # bare mirror, refs/pull/*
    uv run --locked python scripts/rbm-bench/cases.py fetch         # cache PR data
    uv run --locked python scripts/rbm-bench/cases.py survey        # one line per PR
    uv run --locked python scripts/rbm-bench/cases.py refix 318     # a re-fix case
    uv run --locked python scripts/rbm-bench/cases.py review 318 1  # a re-review case

Cases and the cache live under ``--eval-root`` (default ``~/robomate-eval``),
outside every checkout an eval agent can see. A case file holds the ground
truth (the merged diff, the original findings), so it is never copied into a
run directory.

A **re-fix** case starts at the main commit the PR's first commit branched
from, with the closing issue's body as it stood at that time (GitHub keeps
issue edit history). A **re-review** case is one ``Reviewed head: <sha>``
comment: the base, the PR commits up to that head, the reviewer's findings
(``rN-M``) and, from the implementer's replies, which of them were fixed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

REPO = "RoboNater/robomate"
DEFAULT_EVAL_ROOT = Path.home() / "robomate-eval"

PR_QUERY = """
query($owner: String!, $name: String!, $n: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $n) {
      number title body createdAt mergedAt additions deletions changedFiles
      userContentEdits(first: 100) { nodes { editedAt diff } }
      baseRefName headRefName mergeCommit { oid }
      closingIssuesReferences(first: 5) { nodes { number title } }
      commits(first: 100) {
        nodes { commit { oid messageHeadline committedDate parents(first: 2) { nodes { oid } } } }
      }
      comments(first: 100) { nodes { author { login } body createdAt } }
    }
  }
}
"""

ISSUE_QUERY = """
query($owner: String!, $name: String!, $n: Int!) {
  repository(owner: $owner, name: $name) {
    issueOrPullRequest(number: $n) {
      __typename
      ... on Issue {
        number title body createdAt state closedAt
        userContentEdits(first: 100) { nodes { editedAt diff } }
        comments(first: 100) { nodes { author { login } body createdAt } }
      }
      ... on PullRequest { number title createdAt }
    }
  }
}
"""

REVIEWED_HEAD = re.compile(r"(?:Re-reviewed|Reviewed) head:\s*`([0-9a-f]{40})`")
VERDICT = re.compile(r"Verdict:\s*\*\*([^*]+)\*\*")
FINDING_ID = re.compile(r"\br(\d+)-(\d+)\b")
SECTION = re.compile(r"^#{2,4}\s+(.*)$", re.MULTILINE)
RESOLVED = re.compile(r"\b(resolved|fixed|addressed)\b", re.IGNORECASE)


def gh_graphql(query: str, number: int) -> dict[str, Any]:
    owner, name = REPO.split("/")
    out = subprocess.run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"n={number}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return cast(dict[str, Any], json.loads(out)["data"]["repository"])


def source_repo(eval_root: Path) -> Path:
    """The eval root's bare mirror of the repository, with GitHub's refs/pull/*."""
    return eval_root / "source.git"


def git(eval_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(source_repo(eval_root)), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def cache_dir(eval_root: Path) -> Path:
    path = eval_root / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_pr(eval_root: Path, number: int, refresh: bool = False) -> dict[str, Any]:
    path = cache_dir(eval_root) / f"pr-{number}.json"
    if path.exists() and not refresh:
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    data = cast(dict[str, Any], gh_graphql(PR_QUERY, number)["pullRequest"])
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


def load_issue(eval_root: Path, number: int, refresh: bool = False) -> dict[str, Any]:
    path = cache_dir(eval_root) / f"issue-{number}.json"
    if path.exists() and not refresh:
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    data = cast(dict[str, Any], gh_graphql(ISSUE_QUERY, number)["issueOrPullRequest"])
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


def merged_numbers() -> list[int]:
    out = subprocess.run(
        ["gh", "pr", "list", "--repo", REPO, "--state", "merged", "--limit", "500"]
        + ["--json", "number"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(item["number"] for item in json.loads(out))


# -- parsing ------------------------------------------------------------------------


def paragraph(body: str, at: int) -> str:
    """The bullet or paragraph of ``body`` that contains offset ``at``."""
    start = max(body.rfind("\n\n", 0, at), body.rfind("\n- ", 0, at))
    start = 0 if start < 0 else start + 1
    ends = [i for i in (body.find("\n\n", at), body.find("\n- ", at)) if i >= 0]
    return body[start : min(ends) if ends else len(body)].strip()


def findings(body: str, seen: set[str]) -> list[dict[str, Any]]:
    """Findings first raised in this review comment, with their severity.

    Review comments are free-form Markdown; a finding is an ``rN-M`` ID not seen
    in an earlier comment. Its severity comes from its own paragraph, else the
    section heading above it ("nonblocking" wins over "blocking").
    """
    sections = [(m.start(), m.group(1).lower()) for m in SECTION.finditer(body)]
    result = []
    for match in FINDING_ID.finditer(body):
        ident = match.group(0)
        if ident in seen:
            continue
        seen.add(ident)
        text = paragraph(body, match.start())
        heading = next((name for start, name in reversed(sections) if start < match.start()), "")
        probe = text[:200].lower()
        if "nonblocking" in probe or "non-blocking" in probe or re.search(r"\bp[34]\b", probe):
            severity = "nonblocking"
        elif "blocking" in probe or re.search(r"\bp[012]\b", probe):
            severity = "blocking"
        elif "nonblocking" in heading or "non-blocking" in heading:
            severity = "nonblocking"
        elif "blocking" in heading:
            severity = "blocking"
        else:
            severity = "unknown"
        result.append({"id": ident, "severity": severity, "text": text})
    return result


def reviews(pr: dict[str, Any]) -> list[dict[str, Any]]:
    """Each review round on the PR, with the findings it raised and whether they were resolved.

    A finding counts as resolved when a later comment (the implementer's reply
    or a re-review) mentions its ID next to "resolved", "fixed" or "addressed".
    """
    comments = pr["comments"]["nodes"]
    rounds = []
    seen: set[str] = set()
    for index, comment in enumerate(comments):
        body = comment["body"]
        head = REVIEWED_HEAD.search(body)
        if not head or not body.lstrip().startswith("Reviewer agent"):
            for match in FINDING_ID.finditer(body):
                seen.add(match.group(0))
            continue
        verdict = VERDICT.search(body)
        rounds.append(
            {
                "comment_index": index,
                "created_at": comment["createdAt"],
                "head": head.group(1),
                "verdict": verdict.group(1).strip().lower() if verdict else "",
                "findings": findings(body, seen),
            }
        )
    for review in rounds:
        for finding in review["findings"]:
            later = comments[review["comment_index"] + 1 :]
            finding["resolved"] = any(
                RESOLVED.search(paragraph(c["body"], m.start()))
                for c in later
                for m in re.finditer(rf"\b{finding['id']}\b", c["body"])
            )
    return rounds


def pr_commits(pr: dict[str, Any]) -> list[dict[str, Any]]:
    return [node["commit"] for node in pr["commits"]["nodes"]]


def branch_base(eval_root: Path, pr: dict[str, Any]) -> str:
    """The main commit the PR's first commit was made on."""
    first = pr_commits(pr)[0]
    parent = str(first["parents"]["nodes"][0]["oid"])
    git(eval_root, "merge-base", "--is-ancestor", parent, "main")
    return parent


def commit_time(eval_root: Path, sha: str) -> str:
    return git(eval_root, "show", "-s", "--format=%cI", sha).strip()


def work_start(eval_root: Path, pr: dict[str, Any]) -> str:
    """When work on the PR began: its first commit's author date, in UTC.

    Issue bodies, comments and context issues are taken as they stood then.
    PR commits come from the mirror's ``refs/pull/<n>/head``.
    """
    first = pr_commits(pr)[0]["oid"]
    stamp = git(eval_root, "show", "-s", "--format=%aI", first).strip()
    moment = datetime.fromisoformat(stamp).astimezone(UTC)
    return moment.isoformat().replace("+00:00", "Z")


def body_at(issue: dict[str, Any], when: str) -> tuple[str, str]:
    """The issue body as it stood at ``when``, and how that was established.

    ``userContentEdits`` lists each edit with the body *after* it (``diff``,
    newest first). The body at ``when`` is the newest edit made at or before
    ``when``, or the oldest recorded body if every edit came later.
    """
    edits = issue.get("userContentEdits", {}).get("nodes", [])
    cutoff = datetime.fromisoformat(when.replace("Z", "+00:00"))
    if not edits:
        return issue["body"], "current (never edited)"
    dated = sorted(
        (datetime.fromisoformat(e["editedAt"].replace("Z", "+00:00")), e["diff"] or "")
        for e in edits
    )
    before = [body for at, body in dated if at <= cutoff]
    if not before:
        return dated[0][1], "oldest recorded revision (all edits after cutoff)"
    source = f"edit history ({len(before)}/{len(dated)} revisions before cutoff)"
    if len(dated) >= 100 and len(before) == 1:
        # GitHub keeps the original and the newest edits only; the revision at
        # the cutoff may have been dropped, so this is the original body.
        source += "; GAP: history truncated, original body used"
    return before[-1], source


# -- commands -----------------------------------------------------------------------


def cmd_mirror(args: argparse.Namespace) -> None:
    source = source_repo(args.eval_root)
    if source.exists():
        subprocess.run(["git", "-C", str(source), "remote", "update", "--prune"], check=True)
    else:
        url = f"https://github.com/{REPO}.git"
        subprocess.run(["git", "clone", "--mirror", url, str(source)], check=True)


def cmd_fetch(args: argparse.Namespace) -> None:
    for number in merged_numbers():
        load_pr(args.eval_root, number, refresh=args.refresh)
        print(number, end=" ", flush=True)
    print()


def cmd_survey(args: argparse.Namespace) -> None:
    paths = sorted(
        cache_dir(args.eval_root).glob("pr-*.json"), key=lambda p: int(p.stem.split("-")[1])
    )
    print("pr   +add/-del files commits issues  rounds  blocking(resolved)  title")
    for path in paths:
        pr = json.loads(path.read_text(encoding="utf-8"))
        rounds = reviews(pr)
        blocking = [f for r in rounds for f in r["findings"] if f["severity"] != "nonblocking"]
        fixed = [f for f in blocking if f["resolved"]]
        issues = ",".join(str(i["number"]) for i in pr["closingIssuesReferences"]["nodes"])
        print(
            f"{pr['number']:<4} +{pr['additions']}/-{pr['deletions']} {pr['changedFiles']:>3} "
            f"{len(pr_commits(pr)):>4}  {issues or '-':<7} {len(rounds):>3}  "
            f"{len(blocking):>4}({len(fixed)})  {pr['title'][:70]}"
        )


def save_case(eval_root: Path, case: dict[str, Any]) -> Path:
    directory = eval_root / "cases"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{case['id']}.json"
    path.write_text(json.dumps(case, indent=1), encoding="utf-8")
    print(path)
    return path


def related_issues(eval_root: Path, text: str, cutoff: str, skip: set[int]) -> dict[str, Any]:
    """Issues a case's text names (#N), with bodies as of ``cutoff``; PRs are noted only."""
    result: dict[str, Any] = {}
    for number in sorted({int(n) for n in re.findall(r"(?<![\w/])#(\d+)\b", text)} - skip):
        item = load_issue(eval_root, number)
        if item is None or item.get("__typename") != "Issue":
            continue
        if item["createdAt"] > cutoff:
            continue
        body, source = body_at(item, cutoff)
        result[str(number)] = {
            "title": item["title"],
            "created_at": item["createdAt"],
            "closed": bool(item.get("closedAt")) and item["closedAt"] <= cutoff,
            "body": body,
            "body_source": source,
            "comments": [c for c in item["comments"]["nodes"] if c["createdAt"] <= cutoff],
        }
    return result


def cmd_refix(args: argparse.Namespace) -> None:
    pr = load_pr(args.eval_root, args.pr)
    issues = pr["closingIssuesReferences"]["nodes"]
    if len(issues) != 1:
        raise SystemExit(f"PR #{args.pr} closes {len(issues)} issues; a re-fix case needs one")
    number = issues[0]["number"]
    base = branch_base(args.eval_root, pr)
    base_time = commit_time(args.eval_root, base)
    cutoff = work_start(args.eval_root, pr)
    issue = load_issue(args.eval_root, number)
    body, source = body_at(issue, cutoff)
    merged = pr["mergeCommit"]["oid"]
    case = {
        "id": f"refix-{number}",
        "kind": "refix",
        "repository": REPO,
        "pr": pr["number"],
        "issue": {
            "number": number,
            "title": issue["title"],
            "created_at": issue["createdAt"],
            "body": body,
            "body_source": source,
            "comments": [c for c in issue["comments"]["nodes"] if c["createdAt"] <= cutoff],
        },
        "base": base,
        "base_time": base_time,
        "work_start": cutoff,
        "context_issues": related_issues(
            args.eval_root,
            body + "\n" + (git(args.eval_root, "show", f"{base}:AGENTS.md")),
            cutoff,
            {number},
        ),
        "truth": {
            "merge_commit": merged,
            "diff_stat": git(args.eval_root, "diff", "--stat", base, merged)
            .splitlines()[-1]
            .strip(),
            "files": git(args.eval_root, "diff", "--name-only", base, merged).split(),
            "review_rounds": len(reviews(pr)),
        },
    }
    save_case(args.eval_root, case)


def curated(found: list[dict[str, Any]], overrides: list[str]) -> list[dict[str, Any]]:
    """Findings with ``--severity ID=blocking|nonblocking`` applied.

    Only resolved findings are reference. The parsed severity is a heuristic
    over free-form review text, so a resolved finding whose severity it could
    not tell must be classified by hand before the case is written.
    """
    severity: dict[str, str] = {}
    for item in overrides:
        ident, _, value = item.partition("=")
        if value not in ("blocking", "nonblocking"):
            raise SystemExit(f"--severity {item!r}: expected ID=blocking or ID=nonblocking")
        severity[ident] = value
    ids = {f["id"] for f in found}
    if unknown := sorted(set(severity) - ids):
        raise SystemExit(f"--severity names findings this round did not raise: {unknown}")
    result = [f | {"severity": severity.get(f["id"], f["severity"])} for f in found]
    unclear = [f["id"] for f in result if f["resolved"] and f["severity"] == "unknown"]
    if unclear:
        raise SystemExit(
            f"severity unclear for resolved finding(s) {', '.join(unclear)}; read the review "
            "and pass --severity ID=blocking|nonblocking for each"
        )
    return result


def cmd_review(args: argparse.Namespace) -> None:
    pr = load_pr(args.eval_root, args.pr)
    rounds = reviews(pr)
    if not 1 <= args.round <= len(rounds):
        raise SystemExit(f"PR #{args.pr} has {len(rounds)} review rounds")
    review = rounds[args.round - 1]
    commits = pr_commits(pr)
    shas = [c["oid"] for c in commits]
    if review["head"] not in shas:
        raise SystemExit(f"reviewed head {review['head']} is not a PR commit")
    upto = commits[: shas.index(review["head"]) + 1]
    base = branch_base(args.eval_root, pr)
    issues = pr["closingIssuesReferences"]["nodes"]
    number = issues[0]["number"] if issues else None
    base_time = commit_time(args.eval_root, base)
    cutoff = work_start(args.eval_root, pr)
    issue = load_issue(args.eval_root, number) if number else None
    body, source = body_at(issue, cutoff) if issue else ("", "")
    case = {
        "id": f"review-{args.pr}-r{args.round}",
        "kind": "review",
        "repository": REPO,
        "pr": pr["number"],
        "pr_title": pr["title"],
        "pr_body": body_at(pr, review["created_at"])[0],
        "pr_comments": pr["comments"]["nodes"][: review["comment_index"]],
        "issue": None
        if issue is None
        else {
            "number": number,
            "title": issue["title"],
            "created_at": issue["createdAt"],
            "body": body,
            "body_source": source,
            "comments": [c for c in issue["comments"]["nodes"] if c["createdAt"] <= cutoff],
        },
        "base": base,
        "base_time": base_time,
        "work_start": cutoff,
        "head": review["head"],
        "commits": [{"oid": c["oid"], "headline": c["messageHeadline"]} for c in upto],
        "context_issues": related_issues(
            args.eval_root,
            body
            + "\n"
            + body_at(pr, review["created_at"])[0]
            + "\n"
            + git(args.eval_root, "show", f"{base}:AGENTS.md"),
            cutoff,
            {number} if number else set(),
        ),
        "truth": {
            "verdict": review["verdict"],
            "findings": curated(review["findings"], args.severity),
            "later_rounds": len(rounds) - args.round,
        },
    }
    save_case(args.eval_root, case)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--eval-root", type=Path, default=DEFAULT_EVAL_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mirror")
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--refresh", action="store_true")
    sub.add_parser("survey")
    refix = sub.add_parser("refix")
    refix.add_argument("pr", type=int)
    review = sub.add_parser("review")
    review.add_argument("pr", type=int)
    review.add_argument("round", type=int)
    review.add_argument(
        "--severity",
        action="append",
        default=[],
        metavar="ID=blocking|nonblocking",
        help="classify a finding whose severity the parser could not tell (repeatable)",
    )
    args = parser.parse_args(argv)
    {
        "mirror": cmd_mirror,
        "fetch": cmd_fetch,
        "survey": cmd_survey,
        "refix": cmd_refix,
        "review": cmd_review,
    }[args.command](args)


if __name__ == "__main__":
    main()

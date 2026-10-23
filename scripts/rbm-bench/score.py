#!/usr/bin/env python3
"""Score a finished eval run from its results directory.

    uv run --locked python scripts/rbm-bench/score.py ~/robomate-eval/results/<run id>

**Re-review:** the candidate's review is charlie's MR comment
(``mr-notes.json``). A judge model (``--judge``, run with no tools) matches its
findings against the case's original findings and classifies each extra
finding as a valid defect, a defensible nit or a false positive. The judge's
JSON is saved as ``score.json`` with recall over the original blocking findings.

**Re-fix:** the merged ``main`` is checked out into a scoring directory and
validated inside the sandbox: ruff, mypy, the full pytest suite, and the
original PR's test files copied over the candidate's (hidden tests, a soft
signal, since they can name the original implementation's internals).

Judging and validation are separate from the run, so a run can be re-scored.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jail import seed_opencode_db  # noqa: E402

TOOLING = Path(__file__).resolve().parents[2]
#: ``harness:model`` for the judge; Claude Code or OpenCode, run with no tools.
DEFAULT_JUDGE = "opencode:opencode-go/muse-spark-1.3-contributor"

JUDGE_PROMPT = """\
You are grading a code reviewer in a benchmark. The same merge request, at the
same head commit, was reviewed earlier by a strong reviewer; the findings it
raised were confirmed by later fixes (marked resolved) and are the reference.

Compare the CANDIDATE REVIEW to the REFERENCE FINDINGS.

1. For each reference finding, decide whether the candidate review raises the
   same underlying defect (same root cause; wording, line numbers and proposed
   fix may differ). Partial credit: "partial" when it notices the area or
   symptom but misses the actual defect.
2. For each candidate finding that matches no reference finding, classify it:
   "valid" (a real defect the reference missed), "nit" (defensible but minor or
   stylistic) or "false_positive" (wrong, or not a defect).
3. Record the candidate's verdict (approved / changes requested / other).

Reply with only a JSON object, no prose:
{"reference": [{"id": "r1-1", "match": "yes|partial|no", "candidate_ids": ["..."],
  "note": "..."}],
 "extra": [{"candidate_id": "...", "class": "valid|nit|false_positive", "note": "..."}],
 "candidate_verdict": "...", "summary": "one or two sentences"}

REFERENCE FINDINGS (verdict: {verdict}):
{reference}

CANDIDATE REVIEW:
{candidate}
"""


def candidate_review(results: Path) -> str:
    notes = json.loads((results / "mr-notes.json").read_text(encoding="utf-8"))
    bodies = [
        note["body"]
        for iid in sorted(notes, key=int)
        for note in notes[iid]
        if note["body"].lstrip().startswith("Reviewer agent")
    ]
    return "\n\n---\n\n".join(bodies)


def judge(prompt: str, spec: str) -> dict[str, Any]:
    harness, _, model = spec.partition(":")
    if harness == "claude":
        command = ["claude", "-p", "--model", model, "--tools", "", "--output-format", "text"]
        result = subprocess.run(command, input=prompt, capture_output=True, text=True, check=True)
    elif harness == "opencode":
        # Each call gets its own OpenCode data directory (login only, as in the
        # sandbox) and an empty working directory: a shared session database or
        # a large project directory can stall a new session indefinitely.
        command = ["opencode", "run", "--model", model, prompt]
        for attempt in (1, 2, 3):
            with tempfile.TemporaryDirectory(prefix="eval-judge-") as tmp:
                (Path(tmp) / "data" / "opencode").mkdir(parents=True)
                (Path(tmp) / "work").mkdir()
                seed_opencode_db(Path(tmp) / "data" / "opencode")
                env = {
                    **os.environ,
                    "XDG_DATA_HOME": str(Path(tmp) / "data"),
                    "OPENCODE_CONFIG_CONTENT": json.dumps({"permission": {"*": "deny"}}),
                }
                try:
                    result = subprocess.run(
                        command,
                        cwd=Path(tmp) / "work",
                        capture_output=True,
                        text=True,
                        check=True,
                        env=env,
                        timeout=300,
                    )
                    break
                except subprocess.TimeoutExpired:
                    if attempt == 3:
                        raise
    else:
        raise ValueError(f"unknown judge harness {harness!r}")
    match = re.search(r"\{.*\}", result.stdout, re.DOTALL)
    if not match:
        raise RuntimeError(f"judge returned no JSON: {result.stdout[:500]}")
    data: dict[str, Any] = json.loads(match.group(0))
    data["judge"] = spec
    return data


def score_review(case: dict[str, Any], results: Path, judge_spec: str) -> dict[str, Any]:
    review = candidate_review(results)
    if not review:
        return {"status": "no_review", "recall_blocking": 0.0}
    truth = case["truth"]
    reference = "\n\n".join(
        f"[{f['id']}] severity={f['severity']} resolved={f['resolved']}\n{f['text']}"
        for f in truth["findings"]
    )
    prompt = (
        JUDGE_PROMPT.replace("{verdict}", truth["verdict"])
        .replace("{reference}", reference or "(none: the reference approved this head)")
        .replace("{candidate}", review)
    )
    verdict = judge(prompt, judge_spec)
    blocking = {f["id"] for f in truth["findings"] if f["severity"] != "nonblocking"}
    weight = {"yes": 1.0, "partial": 0.5, "no": 0.0}
    hits = [
        weight.get(r.get("match", "no"), 0.0) for r in verdict["reference"] if r["id"] in blocking
    ]
    extras = [e.get("class") for e in verdict.get("extra", [])]
    return {
        "status": "scored",
        "recall_blocking": round(sum(hits) / len(hits), 2) if hits else None,
        "extra_valid": extras.count("valid"),
        "extra_nit": extras.count("nit"),
        "false_positives": extras.count("false_positive"),
        "judge": verdict,
    }


def run_in_jail(run_dir: Path, workdir: Path, command: str, log: Path, timeout_s: int) -> int:
    jail = TOOLING / "scripts" / "rbm-bench" / "jail.py"
    with log.open("a", encoding="utf-8") as out:
        out.write(f"\n$ {command}\n")
        out.flush()
        try:
            return subprocess.run(
                [sys.executable, str(jail), "--run-dir", str(run_dir), "--agent", "scorer"]
                + ["--", "bash", "-c", command],
                cwd=workdir,
                stdout=out,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
            ).returncode
        except subprocess.TimeoutExpired:
            out.write("TIMEOUT\n")
            return -1


def validation_steps(work: Path, workers: int) -> dict[str, str]:
    """The checks as the snapshot defined them: older bases predate xdist and format checks."""
    lock = (work / "uv.lock").read_text(encoding="utf-8") if (work / "uv.lock").exists() else ""
    workflows = "".join(
        path.read_text(encoding="utf-8") for path in (work / ".github" / "workflows").glob("*.y*ml")
    )
    env = f"PYTEST_XDIST_AUTO_NUM_WORKERS={workers} UV_CACHE_DIR=$HOME/.cache/uv"
    xdist = " -n auto" if 'name = "pytest-xdist"' in lock else ""
    ruff = "uv run --locked ruff check ."
    if "ruff format" in workflows:
        ruff += " && uv run --locked ruff format --check ."
    return {
        "sync": "uv sync --locked --all-packages --dev",
        "ruff": ruff,
        "mypy": "uv run --locked mypy",
        "pytest": f"{env} uv run --locked pytest{xdist} -q -p no:cacheprovider",
    }


def score_refix(case: dict[str, Any], results: Path, run_dir: Path, workers: int) -> dict[str, Any]:
    summary = json.loads((results / "summary.json").read_text(encoding="utf-8"))
    seed = json.loads((results / "seed.json").read_text(encoding="utf-8"))
    target = summary.get("main")
    scored = "merged main"
    if not target or target == case["base"]:
        # Nothing merged (an escalation, the time budget): score the open MR's
        # head, so the implementation is still validated.
        opened = [mr for mr in summary.get("merge_requests", []) if mr["state"] == "opened"]
        if len(opened) != 1:
            return {"status": "no_merge", "workflow_status": summary.get("workflow_status")}
        target = opened[0]["sha"]
        scored = f"unmerged MR !{opened[0]['iid']} head"
    work = run_dir / "score"
    if work.exists():
        shutil.rmtree(work)
    subprocess.run(["git", "clone", "-q", seed["ssh_url"], str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "fetch", "-q", "origin", target], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", target], check=True)
    for stale in (run_dir / "jail" / "sock").glob("*.sock"):
        stale.unlink()
    services = subprocess.Popen(
        [sys.executable, str(TOOLING / "scripts" / "rbm-bench" / "services.py")]
        + ["--run-dir", str(run_dir), "--hub-url", "http://127.0.0.1:9"]
        + ["--token-file", str(run_dir / "score" / "no-token")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log = results / "score.log"
    log.write_text("", encoding="utf-8")
    checks: dict[str, Any] = {}
    try:
        while not (run_dir / "jail" / "sock" / "egress.sock").exists():
            time.sleep(0.2)
        steps = validation_steps(work, workers)
        pytest = steps["pytest"]
        for name, command in steps.items():
            checks[name] = run_in_jail(run_dir, work, command, log, 3600) == 0
            if name == "sync" and not checks[name]:
                break
        # A failing check is charged to the candidate only if it passed on the base.
        failed = [name for name, ok in checks.items() if not ok and name != "sync"]
        if failed:
            subprocess.run(["git", "-C", str(work), "checkout", "-q", case["base"]], check=True)
            baseline = {}
            for name in failed:
                baseline[name] = run_in_jail(run_dir, work, steps[name], log, 3600) == 0
            checks["baseline_of_failures"] = baseline
            subprocess.run(["git", "-C", str(work), "checkout", "-q", target], check=True)
        # Hidden tests: the original PR's test files over the candidate's.
        merged = case["truth"]["merge_commit"]
        tests = [f for f in case["truth"]["files"] if f.startswith("tests/") and f.endswith(".py")]
        source = Path.home() / "robomate-eval" / "source.git"
        for path in tests:
            content = subprocess.run(
                ["git", "-C", str(source), "show", f"{merged}:{path}"],
                capture_output=True,
                check=True,
            ).stdout
            (work / path).parent.mkdir(parents=True, exist_ok=True)
            (work / path).write_bytes(content)
        if tests:
            command = f"{pytest} " + " ".join(tests)
            checks["hidden_tests"] = run_in_jail(run_dir, work, command, log, 3600) == 0
    finally:
        services.terminate()
        services.wait()
    hub_report = json.loads((results / "hub-report.json").read_text(encoding="utf-8") or "{}")
    return {
        "status": "scored",
        "scored": scored,
        "commit": target,
        "workflow_status": summary.get("workflow_status"),
        "diff_stat": subprocess.run(
            ["git", "-C", str(work), "diff", "--shortstat", case["base"], target],
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "checks": checks,
        "review_rounds_reference": case["truth"]["review_rounds"],
        "hub_report_keys": sorted(hub_report)[:20],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("results", type=Path)
    parser.add_argument("--pytest-workers", type=int, default=3)
    parser.add_argument(
        "--judge", default=DEFAULT_JUDGE, help="harness:model, e.g. claude:claude-opus-5-5"
    )
    args = parser.parse_args(argv)
    results = args.results.resolve()
    config = json.loads((results / "config.json").read_text(encoding="utf-8"))
    case = json.loads(Path(config["case"]).read_text(encoding="utf-8"))
    run_dir = results.parent.parent / "runs" / results.name
    if case["kind"] == "review":
        score = score_review(case, results, args.judge)
    else:
        score = score_refix(case, results, run_dir, args.pytest_workers)
    (results / "score.json").write_text(json.dumps(score, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in score.items() if k != "judge"}, indent=1))


if __name__ == "__main__":
    main()

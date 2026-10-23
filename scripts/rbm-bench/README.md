# rbm-bench: model evaluation by replaying robomate's history

rbm-bench (pilot) picks models per role by replaying robomate's own merged work,
rather than from public benchmarks. Two case kinds:

- **Re-fix:** an issue a merged PR closed, started again from the main commit
  the PR branched from. A full Alice/bob/charlie workflow runs on it; the
  candidate is usually bob.
- **Re-review:** one `Reviewed head: <sha>` review round. A scripted Alice
  assigns charlie (the candidate) one review of the MR at that head; the
  original findings that were later resolved are the reference.

Everything runs against throwaway projects on the eval GitLab
(`gitlab-box.local`, see #69), never GitHub.

## Isolation

The repository is public, so an agent that can reach GitHub or the web can find
the real fix. Each harness runs inside `jail.py` (bubblewrap):

- no network namespace of its own; `services.py` outside runs an allowlisting
  CONNECT proxy (model APIs, PyPI, the eval GitLab) and logs every request to
  `<run>/jail/egress.jsonl`;
- the home directory is an empty tmpfs with only harness binaries,
  credentials and caches bound back; Claude, Codex and OpenCode session
  histories are per-run, per-agent directories;
- `~/lw` (every checkout) and other runs are not visible; the robomate tooling
  copy that runs the eval is not mounted, and `robomate mcp` is served by the
  MCP broker in `services.py` through a `uv` shim;
- web tools are off in each harness (some run on the provider's side, past any
  proxy).

Seeded projects hold only history up to the case's base commit, and issue
bodies and comments as they stood when the original work started.

## Layout (`~/robomate-eval`)

```
source.git/   bare mirror of GitHub, with refs/pull/*  (cases.py mirror)
cache/        PR and issue JSON from GitHub            (cases.py fetch)
cases/        case files with ground truth             (cases.py refix|review)
tooling/      copy of this checkout that runs evals    (rsync, uv sync)
runs/<id>/    run directories (agents see their own)
results/<id>/ logs, forge state, summary.json, score.json (agents never see)
```

## Commands

```sh
# once, and to refresh
uv run --locked python scripts/rbm-bench/cases.py mirror
uv run --locked python scripts/rbm-bench/cases.py fetch
uv run --locked python scripts/rbm-bench/cases.py survey
uv run --locked python scripts/rbm-bench/cases.py refix 312        # PR number
uv run --locked python scripts/rbm-bench/cases.py review 307 1     # PR, review round
rsync -a --delete --exclude .git --exclude .venv ./ ~/robomate-eval/tooling/
(cd ~/robomate-eval/tooling && uv sync --locked --all-packages --dev)

# a run, from the tooling copy
cd ~/robomate-eval/tooling
uv run --locked python scripts/rbm-bench/run_case.py ~/robomate-eval/cases/refix-310.json \
    --label glm53 --alice-model claude-opus-5-5 \
    --bob-harness opencode --bob-model opencode-go/glm-5.3
uv run --locked python scripts/rbm-bench/score.py ~/robomate-eval/results/<run id>

# a re-review case: only the reviewer runs; a scripted Alice assigns the review
uv run --locked python scripts/rbm-bench/run_case.py ~/robomate-eval/cases/review-307-r1.json \
    --label haiku55 --charlie-harness opencode --charlie-model opencode-go/claude-haiku-5-5
```

`score.py --judge harness:model` picks the re-review judge (Claude Code or
OpenCode, run with no tools); the default is an OpenCode Go model.

## Running it with an agent

[`review-run-prompt.md`](review-run-prompt.md) is a prompt for an operator-side
agent (for example Codex) that runs a set of re-review cases for one candidate,
scores them and reports. It covers detaching runs with `setsid` (agent shell
tools kill a command's process group when the call returns), the OpenCode
startup stall and how to rerun after it, and the rules that keep the agent off
other hubs and accounts.

## Known issues (pilot)

- OpenCode sometimes stalls at startup before creating a session. Runs need a
  check-in watch and a rerun; the judge retries on its own.
- OpenCode accepts an unknown `--variant` silently, so an effort typo runs at
  the default. Check the variant in the stored session before trusting it.
- The re-review judge is only as good as its model; read the reviews when the
  judge is not a frontier model.

Run one case at a time on a small machine: a re-fix run has three harnesses
and their test suites going at once.

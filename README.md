# robomate

robomate lets a small team of AI coding agents take a GitHub or GitLab issue, or a written
statement of work, all the way to a reviewed and merged pull request. One agent orchestrates,
one implements, and one reviews. Each can run in whichever agent harness and model you choose,
such as Claude Code, Codex, OpenCode, or AntiGravity. robomate is the local hub they coordinate
through. It hands out the work, keeps each agent in its own workspace, and refuses to merge
anything the reviewer didn't approve.

It replaces the manual relay of copying prompts and results between agent sessions: you start
the hub, tell each agent to join, submit the work, and read the report at the end.

## How it works

The MVP experience, as the [spec](docs/mvp-spec.md) (§2) defines it:

```sh
robomate up                       # in a checkout of your repo: starts that checkout's hub
# In each agent's harness, one line:
#   "Join robomate as alice, orchestrator."
#   "Join robomate as bob, implementer."
#   "Join robomate as charlie, reviewer."
robomate submit sow.md            # or: robomate submit --issue 42
robomate status                   # who's connected, what phase, which PR, what's blocked
robomate report                   # merged SHA, review findings and where each one went
```

**Works today (M1, M5):** `robomate up`, `down`, `status`, and `ls`, the `robomate mcp`
bridge that connects every agent to the hub, and both GitHub and GitLab workflows. A hub belongs
to the checkout where `up` runs, so each linked worktree of a repository can run its own named
hub. Runs are still
prepared with `scripts/prepare-run.py`, which writes each agent's configuration and launch prompt
(see the [user guide](docs/user-guide.md)), and each worker uses its own full clone.
**Planned:** the one-line joins, worktrees, and `robomate submit` (M2); harness installs and
certification (M3); close-out and `robomate report` (M4).

- **Pull model.** Agents connect to the hub over MCP and ask for work. The hub holds each
  request open until there is something to do, so no agent polls or spins.
- **Orchestrator (Alice)** plans from the statement of work, assigns tasks, and decides
  when to re-review, rebase, merge, or ask you.
- **Implementer (Bob)** works on a branch in his own git worktree and opens the PR/MR.
- **Reviewer (Charlie)** checks out the exact commit under review in a separate worktree,
  tests it, and posts findings.
- **The hub** stores durable state in SQLite, serves each role's instructions, and tracks
  liveness. It checks merge conditions against the forge. Agents use `gh` / `glab`
  themselves.

## Guarantees

- **Merge is bound to the approved commit.** Alice merges only when the reviewer approved
  the PR's current head, CI is green on that head, and the base is up to date. A push
  after approval, or a stale base, sends the work back through review or a rebase.
- **The reviewer works independently.** Charlie reads the code from the forge in his own
  workspace, never from Bob's files or summaries.
- **Restarts are safe.** The hub, or any single agent, can restart mid-run without losing
  or repeating work.
- **Nothing is dropped at close-out** (M4). Every non-blocking review finding is either
  filed as an issue, noted, or dropped with a recorded reason before a run can finish.
- **It works across harnesses and models** (certification: M3). Any certified harness can
  take any role, with any model and effort setting.

## Status

robomate is in early development, and is now built with itself. M1 and M5 (GitLab) are done:
the standalone hub with `robomate up/down/status/ls`, `robomate mcp`, and dual-forge support
(GitHub and GitLab). M2 is next. Milestone status is on the
[roadmap](https://github.com/RoboNater/robomate/issues/2).

robomate grew out of a proof of concept, robo-agents, which ran the full
issue → PR → review → merge loop on GitHub:
- Claude Code and Codex agents took part.
- Windows and WSL hosts were both used.
- Its runs included changes-requested rounds, post-approval pushes, and stale-base rebases.

The PoC's design is in [`docs/poc-spec.md`](docs/poc-spec.md), its run evidence in
[`docs/evidence/`](docs/evidence/), and its plans, runbooks, and worklog in
[`docs/historical/poc/`](docs/historical/poc/).

## Documentation

- [MVP spec](docs/mvp-spec.md): goals, architecture, interfaces, and plan
- [Milestone plans](docs/development/): [M1](docs/development/implementation-plan-mvp-m1.md)
  (done) and [M5, GitLab](docs/development/implementation-plan-mvp-m5-gitlab.md) (done; M2 next)
- GitLab: the [feasibility study](docs/feasibility-and-impact-of-supporting-gitlab-centric-workflows.md)
  and the [investigation and prototype report](docs/gitlab-forge-support-investigation.md)
- [Harness investigation](docs/harness-investigation-opencode-antigravity.md): OpenCode and
  AntiGravity, for M3
- [User guide](docs/user-guide.md): running a workflow today
- [PoC spec](docs/poc-spec.md): the original design, frozen
- [PoC README](docs/poc-readme.md): the PoC's README, as it stood at the move to robomate
- [PoC lessons](docs/poc-lessons.md): what building and running the PoC taught us
- [robo-agents issues](docs/robo-agents-issues.md): which PoC issues were carried into robomate
- [Historical](docs/historical/): frozen PoC plans, runbooks, and notes

## License

MIT

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
robomate up --agents alice,bob,charlie  # in the target checkout, in its own terminal
# Start each harness in its printed worktree and tell it to join:
#   "Join robomate as alice, orchestrator."
#   "Join robomate as bob, implementer."
#   "Join robomate as charlie, reviewer."
robomate submit sow.md            # or: robomate submit --issue 42
robomate status                   # run headline: state, step, PR, gate; then agents and detail
robomate report                   # merged SHA, review findings and where each one went
```

Alice's join response supplies the next steps for starting a queued run.

`robomate status` opens with a headline block naming the run's state and open
questions on their own lines, the current step (or last terminal one), the PR or
MR with its short head and start age, the latest stored merge-gate reading, and
agent liveness counts. `--brief` prints only that block for `watch`;
`--activity` adds one line per agent, `--notes` shows each latest progress note
in full, `--inbox` shows the questions `robomate inbox` would, and `--live`
adds one read-only forge reading labelled `live`, finding the open PR by the
run branch when none is recorded (status otherwise makes no forge call). The
full hub, workflow and agent detail follows the headline, and
`--json` carries the same facts additively for tools.

Set up the static MCP entry for each harness before the first run. The user
guide also covers starting the whole team from a TOML file and joining workers
from another host. [Run robomate on a repository](docs/user-guide.md)

- **Pull model.** Agents connect to the hub over MCP and ask for work. The hub holds each
  request open until there is something to do, so no agent polls or spins.
- **Role principle.** The orchestrator runs the process; the implementer and reviewer
  decide and agree on the work. The implementer and reviewer usually run on frontier or
  near-frontier models to work out what the task needs and reach agreement, while the
  orchestrator can run on a smaller model because orchestration is mostly a rules-based
  routing process.
- **Orchestrator (Alice)** runs the process from the statement of work: assigns tasks,
  verifies each step happened, records decisions, and routes re-reviews, rebases, merges,
  or operator escalations without judging the work's content.
- **Implementer (Bob)** works on a branch in their own git worktree, opens the PR/MR, and
  proposes substantive decisions (including shared-counter reservations and responses to
  review findings).
- **Reviewer (Charlie)** checks out the exact commit under review in a separate worktree,
  tests it, accepts or challenges the implementer's proposed decisions and counter claims,
  and posts findings.
- **The hub** stores durable state in SQLite, serves each role's instructions, and tracks
  liveness. It checks merge conditions against the forge. Agents use `gh` / `glab`
  themselves.

## Guarantees

- **Merge is bound to the approved commit.** Alice merges only when the reviewer approved
  the PR's current head, CI is green on that head, and the base is up to date. A push
  after approval, or a stale base, sends the work back through review or a rebase.
- **The reviewer works independently.** Charlie reads the code from the forge in their own
  workspace, never from Bob's files or summaries.
- **Restarts are safe.** The hub, or any single agent, can restart mid-run without losing
  or repeating work.
- **Nothing is dropped at close-out** (M4). Every non-blocking review finding is either
  filed as an issue, noted, or dropped with a recorded reason before a run can finish.
- **It works across harnesses and models** (certification: M3). Any certified harness can
  take any role, with any model and effort setting.

## Status

robomate is built with itself. The joined workflow supports GitHub and GitLab.
The [roadmap](https://github.com/RoboNater/robomate/issues/2) tracks milestones
and remaining harness certification work.

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
  (done) and [M5, GitLab](docs/development/implementation-plan-mvp-m5-gitlab.md) (done)
- GitLab: the [feasibility study](docs/feasibility-and-impact-of-supporting-gitlab-centric-workflows.md)
  and the [investigation and prototype report](docs/gitlab-forge-support-investigation.md)
- [Harness investigation](docs/harness-investigation-opencode-antigravity.md): OpenCode and
  AntiGravity, for M3; [Devin CLI](docs/harness-investigation-devin-cli.md), a prototype
  pending a live run
- [User guide](docs/user-guide.md): running a workflow today
- [PoC spec](docs/poc-spec.md): the original design, frozen
- [PoC README](docs/poc-readme.md): the PoC's README, as it stood at the move to robomate
- [PoC lessons](docs/poc-lessons.md): what building and running the PoC taught us
- [robo-agents issues](docs/robo-agents-issues.md): which PoC issues were carried into robomate
- [Historical](docs/historical/): frozen PoC plans, runbooks, and notes

## License

MIT

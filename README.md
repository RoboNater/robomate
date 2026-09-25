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

```sh
robomate up                       # in your repo: starts this repo's hub
# In each agent's harness, one line:
#   "Join robomate as alice, orchestrator."
#   "Join robomate as bob, implementer."
#   "Join robomate as charlie, reviewer."
robomate submit sow.md            # or: robomate submit --issue 42
robomate status                   # who's connected, what phase, which PR, what's blocked
robomate report                   # merged SHA, review findings and where each one went
```

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
- **Nothing is dropped at close-out.** Every non-blocking review finding is either filed as
  an issue, noted, or dropped with a recorded reason before a run can finish.
- **It works across harnesses and models.** Any certified harness can take any role, with
  any model and effort setting.

## Status

robomate is in early development. The [MVP spec](docs/mvp-spec.md) describes the interface
shown above, and it is being built now.

robomate grew out of a proof of concept, robo-agents, which ran the full
issue → PR → review → merge loop on GitHub:
- Claude Code and Codex agents took part.
- Windows and WSL hosts were both used.
- Its runs included changes-requested rounds, post-approval pushes, and stale-base rebases.

The PoC's evidence and design are kept in [`docs/evidence/`](docs/evidence/) and
[`docs/poc-spec.md`](docs/poc-spec.md).

## Documentation

- [MVP spec](docs/mvp-spec.md): goals, architecture, interfaces, and plan
- [PoC spec](docs/poc-spec.md): the original design, frozen
- [PoC lessons](docs/poc-lessons.md): what building and running the PoC taught us

## License

MIT

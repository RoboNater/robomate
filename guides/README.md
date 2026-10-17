# Role guides

Runtime-agnostic instructions for the joined team, served by the hub at
`GET /guides/{role}.md` (spec §4.2) and fetched through the worker's
`get_role_guide(role)` tool. Every runtime gets identical text, which is why
worker behaviour cannot live in a Claude Code skill.

The plan Step 5 content is in `worker.md`, `implementer.md` and `reviewer.md`
(spec §5). `rebase.md` came earlier with the REBASE step (GitHub issue #41),
and `assign_task(role="rebase")` points workers at it.

The orchestrator receives `orchestrator.md` and its hash at `join`; the skill
is a pointer to that tool. Prepared-run prompts stay supported until M2 Step 11.

File names are standing or task role names: lowercase
slugs matching `[a-z][a-z0-9-]*`, with a `.md` suffix. Anything else directly here —
this README included — is not a role and is not served directly.

## Forge neutrality and composition

The role guides above are forge-neutral: they say what to do ("open a change
request with your forge CLI", "read the head back from the forge", "the forge
is the work-product store") and point at the forge appendix for how to do it.
Every forge CLI command lives in exactly one appendix under `forge/`
(`forge/github.md`, `forge/gitlab.md` for workers, and
`forge/orchestrator-github.md`, `forge/orchestrator-gitlab.md` for the orchestrator); no role guide names `gh` or `glab`,
and there are no per-forge copies of role guides.

The guide route composes at serve time: it appends the appendix for the hub's
forge (spec §10) to the role guide before serving it. `gitlab` gives
`gitlab.md`; anything else gives `github.md`, matching gate selection. An
explicit `?forge=` query parameter overrides the hub's forge. If the appendix
for the effective forge is missing, the route returns 404 rather than a guide
without its forge commands.

Worker appendices hold no merge commands. The orchestrator guide composes
with `forge/orchestrator-<forge>.md`, which includes merge and merged-state
read-back commands. GitHub retains `--delete-branch` until M2 Step 8.

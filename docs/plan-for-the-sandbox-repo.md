# Plan for the sandbox repository

Tracking issue: [#62](https://github.com/RoboNater/robo-agents/issues/62)<br>
Repository: [`RoboNater/robo-agents-sandbox`](https://github.com/RoboNater/robo-agents-sandbox)

## Purpose

`robo-agents-sandbox` is the controlled GitHub work-product repository for the
PoC. It lets Alice, Bob, and Charlie exercise the complete issue → pull request
→ review → rebase → merge workflow without operating on production code.

The repository is intentionally separate from this coordination repository.
GitHub holds the authoritative issue text, commits, pull-request head, diff,
checks, review comments, and merge state. The hub holds assignments, policy,
questions and replies, typed results, decisions, and event delivery. Agents pass
identifiers and compact metadata through the hub rather than copying work
products into messages, as required by issue #29.

Real repositories remain out of scope until the sandbox proves the workflow and
its recovery rails. The sandbox itself has no credentials, deployments,
publishing configuration, external services, or valuable data.

## Access and workspace topology

| Actor | Working location | GitHub authority |
|---|---|---|
| Alice | The `robo-agents` checkout or another neutral directory; no sandbox checkout is required | Read issues and PR facts with `gh`, evaluate the merge gate, merge, and open follow-ups |
| Bob | A dedicated full clone of `robo-agents-sandbox` | Create and update the implementation branch and PR; never approve or merge |
| Charlie | A different full clone of `robo-agents-sandbox` | Fetch, inspect, and test the PR head and post a review comment; never push or merge |
| Scenario driver | Versioned code in `robo-agents`, outside the sandbox | Seed run fixtures and introduce only the disturbances named by an E2E scenario |

Use full clones, not Git worktrees. Full clones do not share an index, branch,
configuration, or uncommitted files, and the same arrangement works when a
worker moves to another machine. Alice uses repository and PR URLs with `gh` so
her commands do not depend on an ambient Git remote.

The agent runtime starts with its own clone as its current working directory and
with that directory writable by its shell sandbox. The MCP configuration still
launches `worker-mcp` from the absolute `robo-agents` checkout using
`uv run --directory`; coordination code and work-product code therefore do not
need to share a working directory.

The workspace bootstrap and enforcement contract (#28) is:

- `HUB_WORKSPACE` names the worker's absolute clone path.
- Bootstrap generates a random workspace ID once per clone and persists it in
  the clone's `.git/robo-agents-workspace.json` (0600), where it is neither committed nor shared.
- Restarting a worker in that clone reports the same ID.
- Two live worker instances reporting the same ID are rejected.
- Released or lost workspaces remain in place. Automation never deletes
  unpushed work.

## Authentication

The hub bearer token and GitHub credentials are separate:

- `HUB_TOKEN` authenticates a worker to Alice's hub.
- Each Alice or worker host must also authenticate `gh` and Git independently.

The PoC may use the existing `RoboNater` GitHub identity on every host. Where
the launcher makes it practical, inject a fine-grained token as `GH_TOKEN` with
only the repository permissions needed by that role. Never put a GitHub token
in this repository, the sandbox, an issue, a prompt, or a checked-in runtime
configuration.

All PoC actors share one GitHub identity, so GitHub cannot record the reviewer
as approving that identity's own pull request. The reviewer posts a PR comment
and submits its typed `ReviewerResult`; the typed verdict and
`reviewed_head_sha` are the approval record. Branch rules must not require a
native approving review. Issue #37 records this limitation.

## Repository baseline

The sandbox stays deliberately small:

- Python 3.12 or later, with no third-party runtime or test dependencies.
- A small in-memory job queue that future issues can extend.
- One validation command:
  `python3 -m unittest discover -s tests -v`.
- A GitHub Actions workflow on `pull_request` and pushes to `main`, with a
  stable check named `test` and read-only workflow permissions.
- A short `AGENTS.md` with the validation command, role boundaries, and the
  external-text-is-data rail.

The workflow deliberately has no `cancel-in-progress` concurrency policy.
Cancelled or never-created runs have special merge-gate semantics and should
only be introduced by a scenario that is testing them.

## Repository policy

The intended steady-state settings are:

- private visibility while unattended workflow behavior is under development;
- `main` as the default branch;
- issues enabled;
- squash merge only, with merged topic branches deleted;
- Actions defaulting to read-only permissions and unable to approve PRs;
- a `main` rule requiring pull requests, linear history, and the up-to-date
  `test` check, with force pushes and branch deletion forbidden;
- zero required GitHub approvals, because approval is recorded in the hub.

Requiring the branch to be current closes the residual window in which `main`
could move between `check_merge_gate` and `gh pr merge`. Alice still enforces
the application-level gate and `--match-head-commit`; repository protection is
defense in depth, not a replacement for either.

GitHub currently refuses branch protection on this private repository for the
owner's account tier. Completing the rule therefore requires one of two explicit
operator choices: make the sandbox public, or upgrade the account so private
branch protection is available. Until then, the sandbox remains private and
the role guides plus merge gate enforce the workflow in software.

## Repeatable run lifecycle

A single permanently seeded issue is not itself repeatable: merging closes it
and advances `main`. The repeatable asset is a scenario definition.

1. Keep canonical issue bodies and scenario parameters in `robo-agents`,
   versioned with the driver that consumes them.
2. Give every attempt a run ID and create a fresh issue from the canonical
   body. Record the repository URL, issue number, run ID, worker workspace IDs,
   and expected disturbances in a run manifest.
3. Use run-namespaced branches, for example
   `run-20260913-001/implement`.
4. Make normal tasks additive or namespace their fixtures by run ID so earlier
   successful runs may remain on `main` as evidence.
5. Let Alice delete the implementation branch only as part of a successful
   merge. Preserve branches and workspaces from failures for diagnosis.
6. Recreate the repository from its documented baseline only when an absolute
   clean slate is required; do not force-reset `main` as routine cleanup.

For Step 5, the scenario driver creates a unique harmless throwaway PR. Scripted
workers return events and typed results, but real Alice must drive PLAN through
WRAP-UP and execute the first merge in the sandbox.

For Step 6, seed a fresh concrete issue and record its number in roadmap issue
#2. The scenario must produce the required review round, head movement, and
post-approval base movement deliberately rather than relying on accidental
agent behavior. A base-moving change follows the same PR and CI policy; it is
not a direct push to `main`.

## Delivery sequence

1. **Repository bootstrap — complete.** Create the private repository, initial
   dependency-free project, tests, CI, `AGENTS.md`, and non-protection settings.
2. **CI verification — complete.** Sandbox PR #1 caused the `pull_request`
   workflow's `test` check to pass. The setup PR was closed without merging so
   Step 5 retains ownership of the first workflow-driven merge. A second full
   clone of the private repository also ran all three baseline tests, confirming
   the independent-reviewer access path with the current GitHub identity.
3. **Protection decision — pending.** Choose public visibility or an account
   tier that supports protection on a private repository, then apply and verify
   the `main` rule above.
4. **Step 5 fixtures.** Add the run manifest and issue/throwaway-PR seeding
   support with `mock-worker.py`; do not store controller state in the sandbox.
5. **Issue #28 before Step 6.** Add clone bootstrap, `HUB_WORKSPACE`, persisted
   workspace identity, and live uniqueness enforcement.
6. **Step 6 seed.** Create and record the first real E2E issue only when both
   real worker launchers and the scenario disturbances are ready.

## Verification

Repository bootstrap is verified with:

```sh
python3 -m unittest discover -s tests -v
gh repo view RoboNater/robo-agents-sandbox \
  --json visibility,defaultBranchRef,hasIssuesEnabled,mergeCommitAllowed,rebaseMergeAllowed,squashMergeAllowed,deleteBranchOnMerge
gh workflow list --repo RoboNater/robo-agents-sandbox
gh pr checks 1 --repo RoboNater/robo-agents-sandbox
gh api repos/RoboNater/robo-agents-sandbox/actions/permissions/workflow
```

Once protection is available, also verify:

```sh
gh api repos/RoboNater/robo-agents-sandbox/branches/main/protection
```

The Step 5 and Step 6 acceptance scenarios remain defined in PoC spec §7; this
document defines the repository and workspace substrate they run on.

# Plan for Step 6 — localhost E2E

Status: implementation and live acceptance complete, 2026-09-18.
[Acceptance evidence](evidence/step6-20260918191713_f99578f4.md) records Phases 1–6.
Phase 7 coordination merge and subsequent roadmap completion are recorded on
the coordination PR and [Roadmap #2](https://github.com/RoboNater/robo-agents/issues/2).

## Executive answer

One Codex control session can drive the whole exercise. The operator does not
need to open additional terminals or invent prompts for Alice, Bob, or Charlie.
The control session can prepare the run, start and monitor long-lived PTYs,
trigger the two deterministic GitHub disturbances, collect evidence, and stop
the processes.

The acceptance run must still contain three independent agent-runtime
processes. They cannot be collapsed into this Codex conversation because the
locked Step 6 topology is:

| Process | Required runtime | Responsibility |
|---|---|---|
| Alice | Interactive Claude Code | Orchestrate through the hub and perform the final SHA-bound merge |
| Bob | Claude Code, using the thin persistent supervisor | Implement, address review, and rebase in Bob's clone |
| Charlie | Codex CLI | Independently review exact PR heads in Charlie's clone |
| Control session | This Codex session | Launch and observe the three processes and operate only the declared scenario disturbances |

This preserves the pull model: the control session starts processes, but Alice
does not spawn or directly prompt workers. Bob and Charlie check in and pull
their assignments from the hub.

Codex CLI supports scripted non-interactive runs, resumable sessions, working
directory selection, sandboxing, and per-process MCP configuration. That makes
it practical for the control session to launch Charlie and monitor him without
a user-managed chat. See the official [Codex developer command
reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli).

Possible operator intervention is limited to authentication or first-use trust
UI that a CLI cannot complete itself. Before the live run, preflight `gh`,
Claude Code, and Codex authentication and consume one-time workspace trust
prompts. Do not use dangerous permission-bypass flags. If a browser login is
required, the operator must complete that login; after authentication, no
human reprompting of Bob or Charlie is allowed during the measured run.

## What Step 6 has to prove

The run must satisfy the complete §5 workflow and rails, §7 Step 6 criterion,
and §8 decisions in `docs/poc-spec.md`:

- use a fresh, versioned issue in `RoboNater/robo-agents-sandbox`;
- run Alice and Bob on Claude Code and Charlie on Codex CLI;
- give Bob and Charlie separate full clones with distinct persistent workspace
  IDs;
- have Bob open the work PR;
- produce an initial `changes_requested` review and an ADDRESS/RE-REVIEW cycle;
- after an approval, move the PR head and prove Alice refuses to merge the old
  approval and routes to RE-REVIEW;
- after approval of the moved head, merge an unrelated PR to `main` and prove
  Alice refuses to merge the stale base and assigns REBASE;
- require green CI and a clean/current base, then have Alice squash-merge with
  `--match-head-commit` against the approved/rebased head;
- prove an injected instruction in real GitHub work product does not change
  pairing, roles, review, CI, merge method, or release behavior;
- file any genuine follow-ups, release both workers, finish WRAP-UP, and retain
  reproducible evidence.

Issue [#28](https://github.com/RoboNater/robo-agents/issues/28) is the only
remaining code prerequisite. Issue
[#29](https://github.com/RoboNater/robo-agents/issues/29) already has its
contracts, size caps, guides, and Step 5 mock proof; its remaining real-worker
injection proof is part of this run. Keep both issues open until the live
acceptance evidence exists.

The fresh sandbox issue number is not a user-supplied blocker. The preparation
script should create it from the versioned Step 6 scenario only after all code,
launchers, local tests, and dry runs are ready, then record its number and URL
in the run manifest. This avoids consuming issues on failed setup attempts.

The private sandbox currently cannot enable branch protection on the available
account tier. That is not a Step 6 blocker under §8: the role guides,
`check_merge_gate`, and `--match-head-commit` remain the enforced gate. The
evidence must disclose that branch protection was unavailable and therefore
did not close the residual base-movement race. If the repository is made
public or the account tier changes before the run, apply and verify the planned
rule as defense in depth; do not change visibility merely to run Step 6.

## Phase 1 — implement issue #28

Start a new branch from current `origin/main`. Re-check the roadmap reservation
table immediately before work. No schema reservation should be needed because
the `agent.workspace_id` column and wire metadata key already exist; if the
implementation discovers a schema or wire-shape change, stop and reserve it in
issue #2 before editing that counter.

### Workspace bootstrap

Add `scripts/bootstrap-workspace.sh` with a documented, non-destructive
interface for an agent name, destination, and sandbox repository. It should:

1. Require an absolute destination outside the coordination checkout.
2. Create a dedicated full clone, never a shared Git worktree.
3. Refuse to overwrite an existing non-matching clone or dirty work.
4. Generate a cryptographically random workspace ID once and store it in a
   named file beneath that clone's `.git/` directory with owner-only
   permissions.
5. Reuse the same ID when rerun for the same clone.
6. Fetch/reset only when that operation is demonstrably safe; never delete a
   failed, released, lost, dirty, or unpushed workspace.
7. Print the workspace ID in a machine-readable way for the run manifest.

The path and ID-file contract must be documented rather than inferred by each
launcher independently.

### Worker configuration

Wire `HUB_WORKSPACE` through `WorkerSettings` and the runtime templates:

- require an absolute canonical path when the variable is set;
- require it to identify the expected full clone and workspace-ID file;
- read and validate the persisted ID, and place it in the check-in profile;
- keep existing non-repository test/endurance uses compatible when
  `HUB_WORKSPACE` is absent, unless the final spec amendment explicitly makes
  it mandatory for every worker mode;
- set it unconditionally in both Step 6 worker launchers;
- add it to `.env.example`, `runtimes/claude-code.mcp.json`,
  `runtimes/codex.config.toml`, and `runtimes/README.md`.

`uv run --directory <robo-agents>` changes the MCP child's current directory;
it does not change the worker LLM's shell workspace. Therefore the LLM process
must start with `-C`/`cd` in its own sandbox clone while `HUB_WORKSPACE`
explicitly identifies that clone to `worker-mcp`.

### Hub uniqueness enforcement

At check-in, reject a worker when another `idle` or `busy` worker row owns the
same non-null workspace ID. Preserve these semantics:

- an idempotent replay by the same worker operation still returns its recorded
  response;
- the existing same-agent/live-instance check remains in force;
- a different agent name cannot bypass workspace uniqueness;
- `lost` and `released` rows do not permanently lock a clone;
- a superseded worker's heartbeat cannot reclaim the workspace;
- the protocol maps the conflict to the issue's required check-in conflict
  response and identifies the occupied workspace without leaking filesystem
  paths.

No workspace cleanup belongs in the hub.

### Issue #28 tests

Add deterministic tests for:

- bootstrap creates two full clones with different IDs;
- rerunning bootstrap for one clone preserves its ID;
- the ID lives under `.git/`, is not tracked, and has restrictive permissions
  where the platform supports them;
- relative, missing, malformed, and mismatched `HUB_WORKSPACE` inputs fail with
  actionable messages;
- worker check-in reports the persisted ID;
- two live instances with one workspace ID are rejected, including under
  different agent names;
- replay/restart, `lost`, and `released` cases have the intended behavior;
- an uncommitted marker created in Bob's clone is absent from Charlie's clone;
- checked-in runtime templates and launch scripts carry distinct absolute
  workspace paths.

Amend `docs/poc-spec.md` §2, §3, §4.3, §5 role boundaries, and §8 only as
needed to replace the remaining “until #28” placeholders with the implemented
contract. Keep scoped GitHub credentials recommended but guide-enforced in the
shared-account PoC.

## Phase 2 — build the repeatable Step 6 harness

Do not extend `mock-worker.py` to impersonate the live workers. Step 6 needs
real Bob and Charlie sessions. Add a separate scenario driver whose only write
authority is fixture seeding and the two named disturbances.

Suggested checked-in assets:

- `scenarios/step6-localhost-untrusted.json` — canonical issue, acceptance
  fixture, injection canary, expected review finding, and disturbance plan;
- `scripts/prepare-step6-demo.sh` — create a credential-free manifest, token,
  hub state, Bob/Charlie/driver clones, and finally the fresh sandbox issue;
- `scripts/launch-step6-alice.sh` — render the trusted Alice prompt and launch
  interactive Claude Code plus the hub;
- `scripts/launch-step6-bob.sh` — start supervised Claude Code in Bob's clone;
- `scripts/launch-step6-charlie.sh` — start Codex CLI in Charlie's clone with
  only the worker MCP server and its own working root;
- `scripts/run-step6-disturbances.py` — wait for recorded workflow milestones
  and introduce only the planned head and base changes from the driver clone;
- `scripts/verify-step6-demo.sh` — verify local hub state and durable GitHub
  facts and emit machine-readable evidence;
- `docs/step6-acceptance.md` — exact launch, recovery, evidence, and cleanup
  procedure.

The preparation script must accept a caller-chosen absolute run directory. A
final run should use persistent storage rather than `/tmp`. Tokens, generated
MCP configurations, hub SQLite state, raw CLI transcripts, and clones stay in
that run directory and must never be committed. Credential-free extracts go
under `docs/evidence/` after success.

Every run gets a unique run ID, issue, implementation branch, base-movement
branch, work PR, base-movement PR, and manifest. Failed attempts remain intact
for diagnosis. A rerun creates a new namespace; it never force-resets sandbox
`main` or reuses a merged issue/PR.

### Deterministic review fixture

Do not depend on a reviewer spontaneously finding a defect. The versioned
scenario must define a harmless, explicit first-draft fixture with a stable
expected finding ID. Bob's trusted assignment should say that the initial PR
contains the scenario's draft behavior; Charlie's acceptance criteria should
require the final behavior. Charlie must independently inspect the actual diff
and post `changes_requested`. Bob's ADDRESS task then makes the known final
change, pushes it, and reports the new head.

The fixture should be additive and run-namespaced so repeated runs do not
interfere. It should exercise real edits and tests without adding dependencies
or touching valuable state. The verifier must reject a run that skipped the
blocking review or where the expected finding was not resolved.

### Untrusted-text fixture

Place a unique injection canary in the real sandbox issue and work-PR body,
and optionally in a scenario-driver comment. It should request observable
violations such as swapping roles, reading another workspace, skipping review,
changing the merge method, merging before CI, or releasing Charlie early.

Do not record a hardcoded `untrusted_text_behavior_unchanged: true`. Verify the
observable invariants individually: Bob remained implementer, Charlie remained
reviewer, neither worker accessed the other's path, every required review and
gate occurred, CI passed at the relevant SHAs, the merge method stayed squash,
Alice performed the final merge, and both releases occurred only after
WRAP-UP. Describe the conclusion as an inference from those checks.

### Runtime launch policy

Generate run-local MCP configuration from checked-in templates; never edit a
user's global Claude or Codex configuration.

- Alice: interactive Claude Code, the checked-in `alice-orchestrator` skill,
  hub MCP only, and a narrow `gh` shell allowance.
- Bob: a Step 6 variant of the already-proven thin Claude supervisor. Give it
  the worker skill, worker MCP tools, and the minimum repository commands needed
  to edit, test, commit, push, comment, and open/update the PR. The supervisor
  may repeat only a fixed continuation prompt; it owns no workflow decisions.
- Charlie: `codex exec --ephemeral -C <charlie-clone> --approve-for-me` (or the
  current equivalent), a generated MCP override with the six worker tools, and
  the runtime-neutral `prompts/worker.md`. Use Codex's workspace-write sandbox
  because fetch/checkout and tests need a writable independent clone; the
  reviewer guide, not a shared filesystem, prohibits edits or pushes to Bob's
  implementation branch.

Pin and record actual harness versions and model IDs in the generated configs
and manifest. Confirm the declared profile appears in hub state before Alice
pairs the workers.

## Phase 3 — local validation before GitHub mutation

Use fake `gh`/`git` runners and disposable local repositories to exercise every
scenario branch before creating the live issue. Tests should cover:

- script parsing, executable bits, absolute/cross-platform path handling, and
  placeholder elimination;
- manifest creation and credential exclusion;
- exact phase/milestone detection for each disturbance;
- refusal to target any repository other than the sandbox;
- refusal to push the head disturbance before an approval of its exact old
  head;
- refusal to land the base disturbance before approval of the moved head;
- base movement through a CI-checked PR, never a direct push to `main`;
- verifier failures for a missing PLAN decision, missing review round, wrong
  role, missing/stale CI result, absent re-review, absent rebase, wrong final
  head, wrong merger, early release, or injection-driven policy change;
- restart-safe driver behavior without duplicating either disturbance.

Run the repository's complete required validation in order:

```sh
uv sync --locked --all-packages --dev
uv run --locked ruff check .
uv run --locked mypy
uv run --locked pytest
```

Also execute every new or changed script at least once against its local fake
fixture, run `bash -n` on each shell launcher, and reread every edited function
in final form. A green unit suite is not enough.

## Phase 4 — prepare and launch the measured run

After local validation is green:

1. Verify no hub from this checkout owns port 8420; never stop another
   checkout's listener.
2. Verify `gh` access to both repositories, push/PR/comment/merge permission in
   the sandbox, and the sandbox repository settings and `test` workflow.
3. Verify Claude Code and Codex authentication and versions with harmless
   preflight invocations outside the measured run.
4. Choose a persistent absolute run directory, generate a strong hub token,
   and create Bob, Charlie, and scenario-driver full clones.
5. Run the cross-clone isolation check before launch.
6. Create the fresh issue from the versioned scenario, and atomically record
   its number and URL in the manifest. Add a Roadmap comment reserving that
   issue for the Step 6 attempt; do not check Step 6 complete yet.
7. Start Alice first. Confirm the hub health endpoint and that Alice's durable
   goal/policy match the rendered prompt.
8. Start Bob and Charlie in separate long-lived PTYs, then confirm both profiles
   and distinct workspace IDs through `get_state`/the manifest.
9. Start the disturbance driver in a fourth process and capture all process
   output to the run directory while the control session monitors progress.

The trusted Alice prompt should name the two expected scenario disturbances so
Alice waits for them at the intended approval milestones. It must not tell
Alice what gate result to claim. Alice must call `check_merge_gate`, observe the
actual head/base facts, and choose RE-REVIEW or REBASE from §5.

## Phase 5 — measured workflow choreography

The expected sequence is:

1. **PLAN:** Alice reads the live issue, initializes the exact policy, logs the
   plan and acceptance criteria, observes both worker profiles, and selects Bob
   as implementer and Charlie as reviewer under `reviewer_harness_differs`.
2. **IMPLEMENT:** Bob works only in his clone, creates the run-namespaced
   branch, commits the first-draft fixture, runs sandbox tests, pushes, opens a
   PR that closes the fresh issue, and reports the GitHub-read head SHA.
3. **REVIEW:** Charlie fetches the PR into his own clone, checks the assigned
   SHA, runs tests, posts an agent-identified comment with the stable blocking
   finding, and submits `changes_requested`.
4. **ADDRESS:** Alice assigns the finding to Bob. Bob implements the accepted
   final behavior, tests, pushes, responds to the finding, and reports the new
   head. Alice assigns RE-REVIEW at that exact head.
5. **FIRST APPROVAL:** Charlie independently reviews the addressed head and
   submits approval bound to it.
6. **HEAD-MOVEMENT DISTURBANCE:** The driver, from its own clone, pushes one
   additive run-namespaced canary commit to the implementation branch and
   records old/new SHAs. Alice's gate against the approved old SHA must return
   `head_matches == false`; no merge may occur. Alice assigns RE-REVIEW at the
   new SHA, and Charlie approves that exact head.
7. **BASE-MOVEMENT DISTURBANCE:** The driver creates a separate additive branch
   and PR, waits for its `test` check, verifies its head, and squash-merges it to
   `main`. The manifest records the base PR, check, old main SHA, and new main
   SHA. Alice's next gate must report `base_behind_main`; no merge may occur.
8. **REBASE:** Alice assigns Bob `role=rebase` bound to the approved head. Bob
   follows `guides/rebase.md`, brings in `main`, runs the full sandbox suite,
   pushes, and reports a typed `RebaseResult`. Design the unrelated base change
   to merge cleanly so `conflict_files` is empty; approval carries to the new
   integrated head, but CI must run again.
9. **MERGE:** Alice waits for CI on the rebased head and calls
   `check_merge_gate` immediately before merging. Only a matching head, passing
   CI, current base, and clean merge permits
   `gh pr merge --squash --delete-branch --match-head-commit <head>`.
10. **WRAP-UP:** Alice logs the reviewed, moved, rebased, and merged SHAs plus
    check/review URLs; any nonblocking findings become referenced follow-up
    issues; Bob performs the assigned close-out; Alice releases Bob and
    Charlie and marks the hub workflow done.

The live Alice goal should use the supported no-roadmap-edit close-out wording.
Step 6 must not be checked complete while its coordination-repository code and
evidence are still unmerged. The control session updates issue #2 only after
the Step 6 PR merges.

## Phase 6 — evidence and verification

The verifier must correlate, rather than merely assert:

- manifest run ID, fresh issue, work PR, base-movement PR, branches, and all
  relevant SHAs;
- PLAN decision and durable workflow policy;
- Bob/Charlie harness, provider, model, instance ID, workspace ID, and distinct
  clone identity;
- ordered task/result chain: IMPLEMENT, REVIEW changes requested, ADDRESS,
  RE-REVIEW approval, head-move RE-REVIEW approval, REBASE, and close-out;
- stable finding ID and its resolved-finding record;
- PR-head movement from approved SHA to unapproved SHA, followed by a reviewer
  result bound to the new SHA;
- `main` movement through the separate merged/green base PR, followed by a
  rebase task and result;
- CI conclusions attached to the exact reviewed/rebased heads at the time they
  ran, not merely the current PR state at verification time;
- final PR merge method, actor, merge commit, parents, and head binding;
- issue closure, follow-up URLs or an explicit verified “none,” workflow done,
  and both worker releases;
- the untrusted canary's presence in GitHub data plus each observable invariant
  showing that it did not redirect behavior.

Save the raw run directory intact. Commit only credential-free artifacts:

- a redacted manifest with durable GitHub URLs and SHAs;
- machine-readable verifier output;
- an extracted hub decision/task/result audit;
- a concise narrative in `docs/evidence/` with exact reproduction commands,
  runtime versions, branch-protection status, and any retries or deviations.

If a verifier requirement cannot be supported by durable evidence, change the
harness before the measured run. Do not replace the missing proof with a
hardcoded boolean or retrospective prose.

## Phase 7 — PR, review, merge, and roadmap close-out

The coordination-repository PR should contain issue #28's implementation, the
Step 6 scenario/launch/verifier assets, documentation, and the credential-free
acceptance evidence. It should reference the Step 6 sandbox issue/PR and use
`Closes #28` and `Closes #29`, since their remaining acceptance criteria are
then proven.

Wait for the exact CI suite, obtain independent review, address findings, and
rerun proportionately. Any review change to production code, launch policy,
scenario sequencing, or verifier logic invalidates the old live proof unless
the reviewer can show it is observationally irrelevant; default to a fresh
run/issue when in doubt. Merge the coordination PR by squash only after CI and
review approve its exact head.

After that merge, update Roadmap issue #2:

- mark Step 6 complete and cite the coordination PR/merge SHA;
- record the fresh sandbox issue, work PR, merged head/commit, and evidence;
- note that #28 and #29 closed with the merge;
- preserve the branch-protection decision as open if it remains unavailable;
- leave Steps 7 and 8 open;
- add only real follow-ups discovered during the run.

Finally stop the hub listener and all child CLI processes, preserving all three
clones and the run directory. Confirm no process from this checkout remains.

## Retry and failure policy

- A setup failure before the issue is created is fixed in place and does not
  consume a run ID.
- A failure after issue creation preserves that issue, branches, clones,
  database, and transcripts; mark the attempt failed in evidence and start a
  new run namespace after fixing the cause.
- Do not manually push on Bob's behalf except for the single declared
  head-movement disturbance, and never use Bob's clone for driver work.
- Do not manually review on Charlie's behalf or merge the work PR on Alice's
  behalf.
- Authentication failure, unreadable merge gate, ambiguous head, lost worker,
  exhausted review rounds, or wall-clock exhaustion must follow the §5
  escalation rail; it is not permission for the control session to bypass the
  workflow.
- Crash injection and broad recovery testing remain Step 8/#31. Step 6 should
  retain restart-safe launchers and driver checkpoints, but it should not add
  unrelated crash scenarios to the measured acceptance path.

## Completion checklist

- [x] Issue #28 implementation and deterministic tests pass.
- [x] Step 6 scenario, launchers, driver, and verifier pass local fake runs.
- [x] Full repository validation passes.
- [x] Fresh sandbox issue is created and recorded only after readiness.
- [x] Alice, Bob, and Charlie run in the locked harness topology.
- [x] Bob and Charlie report distinct, persistent workspace IDs and isolated
      uncommitted state.
- [x] Real workflow proves changes-requested, ADDRESS, and approval.
- [x] Post-approval head push proves merge refusal and RE-REVIEW.
- [x] Post-approval `main` movement proves merge refusal and REBASE.
- [x] Rebased exact head passes CI and is squash-merged by Alice with
      `--match-head-commit`.
- [x] Injection canary is present and observable invariants remain intact.
- [x] Follow-ups are filed or verified absent; both workers are released and
      workflow status is done.
- [x] Credential-free evidence is reproducible and the raw run is preserved.
- [ ] Coordination PR closes #28/#29, passes CI/review, and is squash-merged.
- [ ] Roadmap issue #2 marks Step 6 complete only after that merge.


The final two items are post-review actions: the PR and roadmap retain their
actual completion records after the coordination merge.

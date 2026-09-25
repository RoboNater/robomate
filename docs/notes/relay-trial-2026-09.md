# Relay-mode orchestration trial, September 2026

In relay mode Alice is a chat agent with no tools and no hub. A human copies
each prompt she writes into the implementer (Bob) or the reviewer (Charlie),
then pastes the worker's reply back to her. Four issues ran this way on
2026-09-10, to try the §5 loop by hand before Step 5 automates it.

The fourth run (#26) used a written skill,
[`skills/alice-relay/SKILL.md`](../../skills/alice-relay/SKILL.md), whose
templates are the prompts the first three runs settled on. Step 5's hub-mode
`alice-orchestrator` skill is derived from it. Findings from the trial are
tracked as #40–#44.

| Issue | Alice | Bob (implementer) | Charlie (reviewer) | PR → merge |
|---|---|---|---|---|
| #33 | fable-5.1-high, Claude Windows app | gem-3.8-flash-high, Antigravity Windows app | gpt-5.6-sol-high, Linux TUI | #35 → `dfbdb0e` |
| #32 | same | same | same | #36 → `dd99ba1` |
| #23 | same | same | same | #38 → `81080e9` |
| #26 | nemotron-3-ultra, OpenCode Windows TUI, `alice-relay` skill | opus-5-high, Claude Code Linux TUI | gpt-5.6-sol-high, Codex Linux TUI | #39 → `5f11b97` |

Runs are in start order. #26 started while PR #38 (#23) was under review and
merged first, which is what made PR #38 conflict with `main`.

Each log is the human's own notes, numbered by turn. Lines starting with `#`
are the human's annotations. Worker replies pasted from rich-text apps had file
and symbol names split onto lines of their own; those are rejoined. Otherwise
the text is unedited, apart from trailing whitespace, a closing separator line
and an empty final entry.

## #33 — Reorder plan; add PoC success statement

**Outcome.** Merged as `dfbdb0e` (PR #35) after one review round, closing #33
and #21. The first kickoff prompt carried design detail Alice could not verify
— scope limits, the #21 overlap, the README edit, when to touch the roadmap —
and the human judged it too detailed. The human's feedback (turn 002) set the
conventions every later run used: short kickoff prompts, steering through
issues and the roadmap rather than prompts, agents identifying themselves in PR
comments ("Implementation agent Bob on behalf of RoboNater"), and the roadmap
update as the implementer's close-out job. The same feedback is where the
lead-role observation in #44 comes from.

~~~text
# 2026-09-10 07:30
# First run using Claude as an Alice orchestrator. Not hooked up, but reading PR and generating next Bob/Charlie prompt for me to copy paste.
# Address issue #33
# Orchestrator Alice: fable-5.1-high orch (Claude windows apps, not Claude Code)
# Implementer Bob:    gem-3.8-flash-high impl (Antigravity windows app)
# Reviewer Charlie:   gpt-5.6-sol-high rvw (Linux TUI)

# 001 - impl
Please address issue #33. Review docs/poc-spec.md and the roadmap in issue #2 first, since this issue reorders the plan (§7) and adds a success statement (§1) and §8 rows. Work on your own branch, commit as you go, and open a PR when you are done.
Notes:
- This is a spec-only change; no code or test changes are expected. If you find that a code change is needed, stop and say so rather than making it.
- Issue #21 (record Codex CLI as the second worker runtime in §2/§8) overlaps the §8 edits. If you can close it within this PR, do so and reference #21 in the PR description; otherwise leave it open and say why.
- The README status paragraph is also stale (it still says Steps 1–3). Update it to reflect steps 1–4 complete and the retrofit in progress if that is a trivial edit; otherwise open an issue for it and mention the issue in the PR.
- Do not update the roadmap in issue #2 yet; that happens at merge.
- When you report back, include the PR number and the final commit hash, and confirm the hash exists on origin.

# 002 - orch
The agent finished and opened PR #35.
# The rest of this is really feedback to Claude as the team lead / architect, but capturing here for useful notes
Regarding your questions, for disambiguation just note "for implementation agent Bob" or "for reviewer agent Charlie" prior to the prompt, that's enough disambiguation for me. But it would be helpful if the agent identified themself in their PR comments, e.g. "Implementation agent Bob on behalf of RoboNater"... maybe try something like that on the next prompt?
  The roadmap update to move a job to done should be the implementer's job, as  I try to keep the Alice work as light as possible.
In that regard, your initial prompt to kick off issue #33 was probably too detailed, ideally Alice would just say "please address issue #33. work on your own branch, commit as you go, and open a PR when you are done" or similar.  We definitely need a project lead / architect who knows the project and can steer the work from a higher level, which is what you are actually doing here.  I can see that becoming a 4th role... let's think through that and revisit once we get the PoC done.  For now let's simplify your Alice-generated prompts, and if you need to steer the agents you can do it via updates to the roadmap or new issues.

# 003 - rvw
Please review and comment on PR #35. Identify yourself in your PR comments as "Reviewer agent Charlie on behalf of RoboNater".

# 004 - impl
A reviewer posted comments on your PR. Please review, adjudicate, address, and/or respond as appropriate. Identify yourself in your PR comments as "Implementation agent Bob on behalf of RoboNater".

# 005 - orch
Bob finished and provided the following plus a more detailed summary of findings, validation, and PR comments:
PR & Commit Details
Pull Request: #35
Final Commit Hash: e022e5f68fdc15fc110578da60f22c101cbc2651
Origin Confirmation: Confirmed on origin/issue-33-reorder-plan-success-statement via git ls-remote:
e022e5f68fdc15fc110578da60f22c101cbc2651  refs/heads/issue-33-reorder-plan-success-statement
CI Status: Green on GitHub Actions.

# 006 - rvw
The developer responded and added commit e022e5f to the PR. Please review and comment.

# 007 - orch
Charlie's response:
Follow-up review posted.
  Commit e022e5f resolves the prior finding. No remaining issues; PR is mergeable and CI is green. Marked ready
  to merge.

# 008 - impl
The reviewer responded to your most recent commit e022e5f and stated the PR is ready to merge. Please close out any remaining open PR threads and merge, then update the roadmap in issue #2 with current status.

# 009 - orch
Bob's response:
Merge and Roadmap Update Complete
Pull Request Merged:
Closed out review threads on PR #35 following Reviewer Charlie's approval.
Squash-merged into main (commit dfbdb0e).
Automatically closed #33 and #21.
Deleted the issue-33-reorder-plan-success-statement branch.
Roadmap Updated:
Updated Issue #2 (Roadmap):
Marked #22 (3c138e4, #34), #33 (dfbdb0e, #35), and #21 (dfbdb0e, #35) as completed.
Sequenced Step 4A — Durability retrofit (#23–#27) as ← *next*, followed by Step 4B — Worker endurance (#30), Step 5 — Guides, Alice skill, prompts, Step 6, Step 7, Step 8, and Step 9 — CI / merge polish.
Updated the overall status paragraph and effort breakdown to match spec §7
~~~

## #32 — Document PoC threat model; per-agent credentials in §9

**Outcome.** Merged as `dd99ba1` (PR #36) with no review rounds: three worker
prompts, all in the short style from #33. The reviewer found nothing blocking,
but GitHub would not let the shared account approve its own PR, so the review
recorded that it would approve. That same-account limitation is tracked in #37.

~~~text
# 2026-09-10 14:25
# Second run using Claude as an Alice orchestrator. Not hooked up, but reading PR and generating next Bob/Charlie prompt for me to copy paste.
# Address issue #32
# Orchestrator Alice: fable-5.1-high orch (Claude windows apps, not Claude Code)
# Implementer Bob:    gem-3.8-flash-high impl (Antigravity windows app)
# Reviewer Charlie:   gpt-5.6-sol-high rvw (Linux TUI)

# 001 - impl
Please address issue #32. Work on your own branch, commit as you go, and open a PR when you are done.

# 002 - orch
Bob's response:
Issue #32 has been addressed on branch issue-32-poc-threat-model, and Pull Request #36 is open.

# 003 - rvw
Please review and comment on PR #36.

# 004 - orch
Charlie's response:
Reviewed PR #36 and posted a comment.
  No blocking findings. The changes match issue #32, are
  correctly scoped, and CI is green. GitHub prevents
  approving your own PR, so I recorded that I would approve
  it as written.

# 005 - impl
The reviewer stated the PR is ready to merge. Please close out any remaining open PR threads and merge, then update the roadmap in issue #2 with current status.

### DONE WITH ISSUE #32
~~~

## #23 — Typed, versioned task results; idempotent worker mutations

**Outcome.** Merged as `81080e9` (PR #38) after the longest loop of the trial,
eleven worker prompts. The first review found four blocking issues, fixed in
`b769be6`; the second found one more, fixed in `1afd588`. By then #26 had
merged and PR #38 conflicted with `main` in 16 files, which Bob resolved by
merging `main` (`5610dc9`). Re-reviewing that head, Charlie found that both PRs
had claimed DB schema v3, so a database created by current `main` skipped the
new migration and check-in failed with `no such table: operation`. Tests on
both branches had passed; the failure only showed when upgrading a real
current-`main` database. Bob bumped the schema to v4 (`6cc913f`), and the PR
was approved and merged. The #26 implementer had flagged the collision in its
PR report and expected the rebase to be mechanical (see the #26 log). This run
is the source of #40 (reserve shared counters before parallel work), #41 (the
merge gate must notice a stale base), and #42's rule that a rebase re-review
does not count as a review round.

~~~text
# 2026-09-10 17:25
# Third run using Claude as an Alice orchestrator. Not hooked up, but reading PR and generating next Bob/Charlie prompt for me to copy paste.
# Implement step 4A of the plan in poc_spec.md
# Orchestrator Alice: fable-5.1-high orch (Claude windows apps, not Claude Code)
# Implementer Bob:    gem-3.8-flash-high impl (Antigravity windows app, w533d)
# Reviewer Charlie:   gpt-5.6-sol-high rvw (Linux TUI, w533b)

# 001 - Alice orch
We are ready to start on 4A, please provide the initial prompt for the implementation agent Bob.

# 002 - Bob impl
Please address issue #23. Work on your own branch, commit as you go, and open a PR when you are done.

# 003 - Alice orch
I have addressed Issue #23 on branch issue-23-typed-results and opened Pull Request #38

# 004 - Charlie revw
Please review and comment on PR #38. Identify yourself in your PR comments as "Reviewer agent Charlie on behalf of RoboNater".

# 005 - Alice orch
Charlie's response:
Reviewed PR #38 and posted a changes requested comment as
  “Reviewer agent Charlie on behalf of RoboNater.”
  Found four blocking issues:
  - Non-atomic idempotency ledger writes
  - Operation IDs change after ambiguous read timeouts
  - Required versioned/typed protocol fields remain
    optional
  - 40-character SHA format is not enforced
  Validation passed: Ruff, mypy, and all 243 tests.

# 006 - Bob impl
A reviewer posted comments on your PR. Please review, adjudicate, address, and/or respond as appropriate. Identify yourself in your PR comments as "Implementation agent Bob on behalf of RoboNater".

# 007 - Alice orch
Bob's response:
```
All review comments posted by reviewer agent Charlie on PR #38 have been reviewed, adjudicated, addressed, and verified.

Actions Taken
[P1] Atomic Operation-Ledger Writes with Business Mutations:

Refactored check_in, record_progress, and submit_result in agent_hub/store.py to wrap ledger check, business mutation, and operation recording within a single SQLite transaction (BEGIN IMMEDIATE).
If a failure occurs before commit, all mutations roll back atomically. Signals are dispatched only post-commit.
Added test_atomic_rollback_on_failure in tests/test_idempotency.py.
[P1] Ambiguous Response Timeout Retries & ID Retention:

Updated _request_with_retry and _stream_rpc in worker_mcp/client.py to catch (httpx.TransportError, httpx.TimeoutException), retrying with the identical prepared payload and operation ID.
Added _pending_checkin and _pending_progress to WorkerHubClient to preserve pending operation IDs across caller-level retry attempts.
Added unit tests in tests/test_idempotency.py verifying retry behavior under write and read timeouts.
[P1] Strict Versioned Typed Wire Contract:

Added strict validation in agent_hub/protocol.py:
_require_schema_version enforces metadata["hub.schema_version"] == 1, returning HTTP 400 if missing or invalid.
_require_operation_id enforces non-empty metadata["hub.operation_id"], returning HTTP 400 if missing or blank.
_require_result requires a typed result object, returning HTTP 400 if missing or invalid.
Removed all legacy synthesis of placeholder PR URLs and dummy SHAs.
[P1] 40-Character Hex SHA Validation:

Constrained head_sha on ImplementerResult and reviewed_head_sha on ReviewerResult to match ^[0-9a-fA-F]{40}$.
Added parameterized negative tests in tests/test_models.py.
[Nonblocking] Operation Table created Column:

Added created TEXT NOT NULL to the operation table DDL in agent_hub/database.py and updated database migrations.
Verification
uv sync --locked --all-packages --dev passed.
uv run --locked ruff check . passed with 0 errors.
uv run --locked mypy passed with 0 errors across 48 source files.
uv run --locked pytest passed (244 passed, 10 skipped).
Changes committed and pushed to origin/issue-23-typed-results (commit b769be6).
GitHub Actions CI on PR #38 passed (test pass 49s).
Response posted to reviewer Charlie on PR #38 comment identifying as "Implementation agent Bob on behalf of RoboNater".
```

# 008 - Charlie revw
The developer responded and added commit b769be6 to the PR. Please review and comment.

# 009 - Alice orch
Charlie's response:
```
Reviewed commit b769be6 and posted a follow-up as
  “Reviewer agent Charlie on behalf of RoboNater.”

  The original findings were addressed, but one blocking
  issue remains: successful check-in and progress operation
  IDs stay cached, causing later identical calls to be
  incorrectly deduplicated.

  View the follow-up review comment

  All repository validation and CI passed.
```

# 010 - Bob impl
A reviewer posted comments on your PR including your commit b769be6. Please review, adjudicate, address, and/or respond as appropriate.

# 011 - Alice orch
Bob's response:
```
Changes committed (1afd588) and pushed to branch issue-23-typed-results.
GitHub Actions CI on PR #38 passed (test pass 40s).
Response posted to Charlie on PR #38 comment identifying as "Implementation agent Bob on behalf of RoboNater". Detailed walkthrough updated in walkthrough.md.
```

# 012 - Charlie revw
The developer responded and added commit 1afd588 to the PR. Please review and comment.

# 013 - Alice orch
Charlie's response:
```
Reviewed commit 1afd588 and posted a follow-up comment.

  The code-level blocker is resolved, but PR #38 now
  conflicts with current main across 16 files, including
  core protocol, store, client, models, and tests. I
  requested conflict resolution and another review
  afterward.
```

# 014 - Bob impl
The reviewer responded to your most recent commit 1afd588: the code findings are resolved, but the PR now conflicts with main (issue #26 merged). Please rebase or merge main, resolve the conflicts, confirm CI is green, push, and report the new commit hash so the reviewer can look at the rebased PR.

# 015 - Alice orch
Bob's response:
```
Summary of Rebase & Merge Resolution
main (commit 5f11b9765f367de84317b65b2f117b05f36a88af, which introduced the worker identity profile and policy-driven role selection from issue #26) has been merged into issue-23-typed-results. All conflicts across the overlapping features have been resolved, and CI is green.

Semantic Reconciliation
Data Models & Constants (agent_hub_common):
Preserved AgentProfile, ModelSource, UNKNOWN, and profile extraction helpers alongside ImplementerResult, ReviewerResult, TestResult, Finding, and wire constants (SCHEMA_VERSION, MetaKeys.OPERATION_ID, MetaKeys.SCHEMA_VERSION).
Database Migrations (agent_hub/database.py):
Chained both migrations in initialize_database so legacy schemas migrate profile columns as well as the operation idempotency ledger table (including created).
Hub Protocol & Store (agent_hub/protocol.py, agent_hub/store.py):
Reconciled _check_in to extract and validate AgentProfile while strictly enforcing hub.operation_id (and accepting hub.schema_version=1), hashing profile fields into payload_hash.
Preserved atomic SQLite transactions (BEGIN IMMEDIATE) across profile check-in, progress recording, and result submission.
Worker Client & Mock Worker (worker_mcp/client.py, scripts/mock-worker.py):
Preserved _pending_checkin and _pending_progress operation ID lifecycles while constructing check-in metadata from AgentProfile.
Updated mock-worker CLI and test configurations to use WorkerSettings(profile=AgentProfile(harness=...)).
Cleanups & Tests:
Updated tests across tests/test_idempotency.py, tests/test_mock_worker.py, and tests/test_protocol.py to pass AgentProfile and mutation envelopes.
Cleaned up the nonblocking trailing EOF blank line in tests/test_idempotency.py (git diff --check is clean).
Verification Results
uv sync --locked --all-packages --dev: Passed
uv run --locked ruff check .: Passed (0 errors)
uv run --locked mypy: Passed (0 errors across 48 source files)
uv run --locked pytest: Passed (266 passed, 10 skipped)
git diff --check origin/main..HEAD: Clean (0 warnings)
GitHub Actions CI: Passed (test pass 39s) — Workflow Run #34556709089
Commit Hash & PR Comment
New Commit Hash: 5610dc9d5657e40bef2209a39490df9a5ef6114e (short SHA: 5610dc9)
PR #38 Comment: Posted at PR #38 comment #5628792326 identifying as "Implementation agent Bob on behalf of RoboNater".
```

# 016 - Charlie revw
The developer merged main into the branch, resolved the conflicts, and added commit 5610dc9 to the PR. Please review the rebased PR and comment.

# 017 - Alice orch
Charlie's response:
```
 Reviewed merged head 5610dc9 and posted a changes-
  requested comment.

  One blocking migration issue remains: current main
  already creates schema-v3 databases without the new
  operation table. Because the PR also uses version 3,
  initialization skips the migration and worker check-in
  fails with no such table: operation.

  View the review comment

  All existing tests, Ruff, mypy, focused integration
  tests, and CI passed; the missing case is upgrading an
  actual current-main v3 database.
```

# 018 - Bob Impl
A reviewer posted comments on your PR including your commit 5610dc9. Please review, adjudicate, address, and/or respond as appropriate.

# 019 - Alice orch
Bob's response:
```
Summary of Changes and Response to Reviewer
Reviewer Charlie identified that databases created by current main already had user_version = 3 (from the worker identity profile migration) and that leaving the PR at database version 3 skipped creation of the operation table for existing mainline databases, causing HubStore.check_in to fail with no such table: operation.

We have addressed this issue, verified the upgrade path, pushed commit 6cc913fa845966028608bd4f483ddeb030ccaafd (6cc913f), and replied on PR #38.

Implementation Details
Bump Database Schema Version to 4 (packages/hub/src/agent_hub/database.py):

Bumped SCHEMA_VERSION = 4.
Updated initialize_database so pre-existing databases with user_version = 3 from main trigger migrations, running _migrate_operation_table to add the operation table and advancing user_version to 4.
Enhanced _migrate_operation_table to defensively check and add the created column if not present.
Regression Test Coverage (tests/test_database.py):

Updated _legacy_database to construct exact mainline v3 schemas with full profile columns and user_version = 3.
Parametrized test_migrated_agent_table_matches_a_fresh_one over [1, 2, 3].
Added test_migration_from_v3_mainline_preserves_profiles_and_enables_idempotent_check_in:
Creates a mainline v3 database populated with agent profile fields (harness, harness_version, provider, model, model_source, workspace_id).
Runs initialize_database(path) and confirms user_version advances from 3 to 4.
Verifies operation table exists with all required columns.
Asserts agent profile data survived the migration intact.
Boots the ASGI test client over the migrated database and performs an end-to-end wire check-in over /a2a, verifying that the operation row is recorded and duplicate submission returns the cached response.
Verification
git diff --check origin/main..HEAD: Clean (0 warnings)
uv sync --locked --all-packages --dev: Passed
uv run --locked ruff check .: Passed (0 errors)
uv run --locked mypy: Passed (0 issues across 48 source files)
uv run --locked pytest: Passed (268 passed, 10 skipped in 30.58s)
GitHub Actions CI: Passed (test pass 42s) — Workflow Run #34595336131

Posted response at PR #38 comment #5633916949 identifying as "Implementation agent Bob on behalf of RoboNater" reporting commit 6cc913fa845966028608bd4f483ddeb030ccaafd.

```

# 020 - Charlie revw
The developer responded and added commit 6cc913f to the PR. Please review and comment.

# 021 - Alice orch
Charlie's response:
```
Reviewed commit 6cc913f and posted an approval comment as
  “Reviewer agent Charlie on behalf of RoboNater.”

  No blocking findings. The main-v3 migration succeeds,
  preserves profile data, and restores idempotent check-in
  replay. Ruff, mypy, and all 278 tests passed.
```

# 022 - Bob impl
The reviewer responded to your most recent commit 6cc913f and stated the PR is ready to merge. Please close out any remaining open PR threads and merge, then update the roadmap in issue #2 with current status.

## ************************************** ##
# FINAL STATUS RESPONSE FROM BOB AT Merge
Actions Completed
Review Threads & Approval:

Verified that reviewer Charlie approved PR #38 with no remaining findings or open review threads.
Squash Merge:

Squash-merged PR #38 into main at commit 81080e9, closing Issue #23.
Deleted the remote and local feature branch issue-23-typed-results.
Updated local main to 81080e9 with a clean working tree.
Roadmap Update in Issue #2:

Updated Issue #2:
Marked #23 — Typed task results & idempotent worker mutations as completed (81080e9, #38) under Step 4A.
Documented the schema v4 migration, atomic SQLite operation ledger writes, typed models in agent_hub_common, and worker client retry lifecycle.
Updated the high-level Step 4A status note.
~~~

## #26 — Worker identity profile and policy-driven role selection

**Outcome.** Merged as `5f11b97` (PR #39) with no review rounds. It was the
first run with the written `alice-relay` skill, and the first with a different
model as Alice. Alice used the skill's templates verbatim and needed two facts
from the human: the GitHub account name for the identification sentence, and
the head commit hash, which Bob's report left out. Following the skill's "ask
rather than guess" rule, Alice asked for the hash instead of inventing one, and
the human looked it up (`2761728`). Bob merged pinned to that commit and updated
the roadmap, as the close-out template asks. Asking was right here because no
hash was reported; #42 adds the other half — when a reply lists several
commits, cite the newest without asking.

~~~text
# 2026-09-10 20:42
# First run using OpenCode as an Alice orchestrator. Using a prompt-only skill, with me copy/pasting to Bob/Charlie and back.
# Address issue #26
# Orchestrator Alice: nemotron-3-ultra (Opencode Windows TUI, w533a)
# Implementer Bob:    opus-5-high (Claude Code Linux TUI, w533c)
# Reviewer Charlie:   gpt-5.6-sol-high (Codex Linux TUI, w533d)

# 001 - Alice orch
Use the alice-relay skill. Orchestrate issue #26 in RoboNater/robo-agents. Implementer is Bob, reviewer is Charlie, roadmap is issue #2. Begin.
# Alice provided a Bob prompt with fill-in-the-blank for github account, and asked for my github account name. I provided "RoboNater", and Alice provided a new Bob prompt matching the one below.

# 002 - Bob impl
Please address issue #26. Work on your own branch, commit as you go, and open a PR when you are done. Identify yourself in your PR comments as "Implementation agent Bob on behalf of RoboNater".

# 003 - Alice orch
Bob's response:
```
I've opened PR #39 for issue #26 on branch issue-26-worker-profile,
  in two commits. CI passed, and locally ruff, mypy and pytest (235
  tests) are clean. I haven't merged it or updated issue #2.

  What it does
  - Worker profile: workers now report harness, harness version,
    provider, model, where the model name came from (model_source),
    capabilities and workspace ID when they check in. The values come
    from HUB_HARNESS, HUB_HARNESS_VERSION, HUB_PROVIDER, HUB_MODEL and
    HUB_CAPABILITIES. Anything not set reads unknown; the old hidden
    default of claude-code is gone.
  - Hub: it checks the profile, replaces it in full on every check-in,
    and shows it per agent in get_state and in the check-in event.
    The database moves to schema v3, and existing runtime values carry
    over as harness.
  - Spec: implementer and reviewer are now chosen by role_policy
    instead of whoever checks in first. That covers waiting up to
    pairing_wait_s for a second worker, escalating with the failed
    rule named, and recording the pairing in the audit log and final
    summary. As the issue says, Alice's skill (Step 5) applies the
    policy, not hub code.
  - Launcher templates: they now set harness and provider, and leave
    version, model and capabilities empty for you to fill in.

  Codex timeout. §8 wanted the second runtime's observed limit, so I
  measured it by running real Codex CLI 0.154.0 against a throwaway
  tool that sleeps. With nothing configured, it fails any MCP tool
  call at 300 s: a 150 s call completed and a 330 s call failed at 300
  s. The tool_timeout_sec setting does override it.
  - The hub's default 120 s wait fits under that.
  - A worker can request up to 300 s, and its client allows 15 s on
    top, so the longest call doesn't fit.
  - I set tool_timeout_sec = 330 in the Codex template. That goes
    slightly beyond the issue's list, and the PR says so.

  Decisions for you to check:
  1. The issue doesn't define declared vs env. I used: env means
     HUB_MODEL was set; declared means the agent named its own model
     through a new check_in(model=...) argument, which counts only if
     HUB_MODEL is unset.
  2. An unknown harness or provider never passes a "must differ" rule,
     so two workers without a configured harness escalate rather than
     get paired.
  3. Old configs that set AGENT_RUNTIME still work as the harness.
  4. workspace_id exists but nothing fills it in until #28.

  Things you should know:
  - Conflict with PR #38: it also moves the schema to v3, so whichever
    merges second needs a rebase. I wrote the migration to check
    which columns exist rather than trust the version number, so the
    rebase should be mechanical.
  - Policy isn't stored anywhere: nothing in the hub writes the
    workflow's policy, so get_state shows policy: {} and Alice only
    has role_policy from her prompt. Step 5 should decide whether a
    restarted Alice needs it saved.
  - Odd Codex behaviour, relevant to #30: a 1000 s tool call didn't
    time out. Codex ended the turn after about 50 s, saying the call
    had "yielded", with the tool still running. I can't explain it,
    and the hub never allows waits that long.

  I also ran it end to end: mock-alice drove a real Codex CLI worker,
  set up like the template, through a live hub. Codex checked in, took
  a task and submitted a result, and the stored profile was correct.
  Codex didn't name its model, so model correctly reads unknown. The
  README's curl check-in example had stopped working after #34 because
  its metadata keys lacked the hub. prefix; it now uses the right
  keys and I confirmed it works against a live hub. Every hub I
  started has been stopped.
```

# Note: Alice provided the Charlie prompt below and also noted:
**Note for you:** Bob mentioned "two commits" but didn't provide the latest commit hash. When Charlie responds, I'll need that hash for the next phase. Also, Bob noted a conflict with PR #38 (both move schema to v3) — whichever merges second will need a rebase.

# Note: I inspected the repo and found commit 2761728 as the latest on the PR branch

# 004 - Charlie revw
Please review and comment on PR #39. Identify yourself in your PR comments as "Reviewer agent Charlie on behalf of RoboNater".

# 005 - Alice orch
Charlie's response:
```
Reviewed and commented on PR #39 as requested.

  - No blocking findings.
  - Ruff, mypy, and all 235 tests passed.
  - GitHub CI is green.
  - Comment identifies: “Reviewer agent Charlie on behalf
    of RoboNater.”

  - GitHub prevented formal approval because the
    authenticated account owns the PR, so I submitted a
    COMMENTED review instead.
```
# Note: Alice asked me following before providing Bob prompt:
Question for you: What's the latest commit hash (7+ chars) on PR #39?
# I responded with:
I inspected the repo and found commit 2761728 as the latest on the PR branch

# 006 - Bob impl
The reviewer responded to your most recent commit 2761728 and stated the PR is ready to merge. Please close out any remaining open PR threads and merge, then update the roadmap in issue #2 with current status.

# 007 - Alice orch
Bob's response:
```
PR #39 is squash-merged into main as 5f11b97, and the roadmap in
  issue #2 is updated.

  PR close-out
  - There were no review threads to resolve. The reviewer (Charlie)
    reviewed head commit 2761728, found nothing blocking, and CI
    passed.
  - GitHub recorded the review as a comment rather than an approval
    because the account can't approve its own PR (the shared-account
    problem in #37).
  - I posted a closing comment as "Implementation agent Bob on behalf
    of RoboNater", then squash-merged, pinned to that exact commit.
  - Issue #26 closed automatically and the PR branch was deleted.
  - main hadn't moved, so no rebase was needed.

  Roadmap (issue #2)
  - Step 4A stays open and now has a progress list. #26 is ticked with
    a summary of what shipped, plus what it hands to later work:
    - Alice's skill still has to apply the pairing policy (Step 5).
    - workspace_id stays empty until #28.
    - Nothing in the hub stores the workflow policy, so Step 5 must
      decide whether Alice needs it saved across restarts.
  - #23, #24, #25 and #27 are listed as open. The #23 line notes that
    PR #38 also claims schema v3 and must rebase onto #39.
  - The status paragraph now says Step 4A is in progress with #26
    done.
  - The second-runtime row in the open-decisions table now notes
    Codex's 300 s default MCP tool timeout.
  - I checked the issue hadn't been edited since I read it before
    overwriting it.

  Housekeeping: gh's --delete-branch had switched this worktree to
  main. I switched it back to wtc-main and fast-forwarded it to
  5f11b97, so it's clean and current.

  Next: PR #38 now conflicts with main on the schema version, so it
  needs a rebase before it can merge.
```
~~~

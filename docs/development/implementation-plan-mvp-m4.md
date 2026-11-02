# Implementation plan — MVP Milestone M4: close-out and resilience

Status: proposed, with the points left open at merge settled under
[Decisions settled after merge](#decisions-settled-after-merge). Tracking issue
[#429](https://github.com/RoboNater/robomate/issues/429);
roadmap [#2](https://github.com/RoboNater/robomate/issues/2), section M4.

This plans the remaining work, using the [M2 plan](implementation-plan-mvp-m2.md)
and [M5 plan](implementation-plan-mvp-m5-gitlab.md) format. The
[MVP spec](../mvp-spec.md) remains the design of record. §-numbers refer to it.
This plan changes no production code or spec text, opens no step issues and changes no
roadmap or reservations. Triage creates the step issues.

## What M4 has to deliver

From §13, row M4:

> **Deliverable:** per-agent closeout report models, durable bounded collection before
> release (#215), `closeout` task role, `CloseoutResult` + validator, `done` check, run
> report + `robomate report`, `robomate abandon`, repo settings (`nits_issue`,
> `followup_label`, `closeout_steps`); `ask_user` inbox with `robomate inbox/answer`;
> resume (§7.5, #18 reduced, #19); `robomate log --timeline` (#20)
>
> **Done when:** Scripted tests: all-three and crashed-worker report collection survive
> restart with explicit missing reasons; report retries, identity/size/credential
> validation and pause-aware expiry; `done` refused with an unmerged PR and with no
> completed close-out; a `CloseoutResult` missing a pending finding, a URL, a `none`
> reason, or a step is rejected; the report lists every finding with its disposition;
> kill/restart of the hub, a worker, and the orchestrator each resume without duplicate
> actions; simulated standby loses no one. One real run on robomate itself ends with
> its follow-ups filed and any used counter claim closed out in the reservations issue,
> with no operator chat

### Inventory on main

Snapshot: `origin/main` at `bae1534b1550a07f017532fe51cd060e115ab547`, inspected
2026-11-02. Sources were the current roadmap, its last full version, both open and closed
issue content searches, and the code/tests below. The last full roadmap was recovered
from `userContentEdits(first:100)` with GraphQL, selecting the first `diff` longer than
40,000 characters (edit timestamp `2026-10-30T22:32:41Z`), as #429 describes.
That historical body still marked #356 open; the current body and main include PR #422.
Closed status alone is not completion evidence. **Done** below requires code and a test
of that particular contract; **partial** includes implemented subsets or adapter tests
that do not prove the whole clause. Test names are references to main, not claims that
this planning run executed those behavior tests individually.

Evidence abbreviations (all paths are in this repository):

- **Collection:** [#215](https://github.com/RoboNater/robomate/issues/215),
  [PR #353](https://github.com/RoboNater/robomate/pull/353),
  [closeout.py](../../packages/hub/src/agent_hub/closeout.py), strict
  [common models](../../packages/common/src/agent_hub_common/models.py),
  [test_closeout.py](../../tests/test_closeout.py).
- **Views:** [#356](https://github.com/RoboNater/robomate/issues/356),
  [PR #422](https://github.com/RoboNater/robomate/pull/422),
  [closeout_export.py](../../packages/hub/src/agent_hub/closeout_export.py),
  [test_closeout_export.py](../../tests/test_closeout_export.py).
  #216 closed by splitting into #356 and #357, not by delivering everything.
- **Core:** [store.py](../../packages/hub/src/agent_hub/store.py),
  [orchestrator.py](../../packages/hub/src/agent_hub/orchestrator.py), common models above,
  [cli.py](../../packages/cli/src/robomate/cli.py).
- **Layers:** [#242](https://github.com/RoboNater/robomate/issues/242),
  [PR #284](https://github.com/RoboNater/robomate/pull/284),
  [config_layers.py](../../packages/common/src/agent_hub_common/config_layers.py),
  [test_config_layers.py](../../tests/test_config_layers.py).
- **Recovery:** [agent-recovery.md](agent-recovery.md),
  [orchestrator guide](../../guides/orchestrator.md),
  [test_orchestrator_bridge.py](../../tests/test_orchestrator_bridge.py),
  [test_worker_client.py](../../tests/test_worker_client.py),
  [test_mock_alice.py](../../tests/test_mock_alice.py),
  [test_store.py](../../tests/test_store.py), [test_cli.py](../../tests/test_cli.py).

Deliverable inventory:

| §13 deliverable item | State | Evidence on main | Remaining work |
|---|---|---|---|
| Per-agent strict report models | done | Collection: `test_strict_models_and_evidence`, `test_verified_claims_and_finding_references` | Keep separate from task dispositions |
| Durable bounded collection before release | done | Collection: `test_isolated_collection_scenario`, `test_request_survives_restart_and_a2a_splits_parts` | Scheduled policy refinements #386/#424 stay separate |
| `closeout` task role | partial | Core accepts arbitrary task role strings; Collection scenario assigns a string `closeout`; standing-role helper allows it | No `TaskRole.CLOSEOUT`, role guide, stored obligation snapshot or role-specific typed completion; Step 1 |
| `CloseoutResult` and validator | missing | Core: `TaskResult` union has implementer/reviewer/rebase only; mappings with `outcome` parse as `ImplementerResult` | Step 1; retrospective model tests are not this validator |
| `done` check | missing | Core: `set_workflow_status` finalizes collection and activates the queue without checking forge state or close-out | Step 2; existing done tests demonstrate lifecycle, not the spec gate |
| Run report | missing | Views exports only `closeout/report-<ordinal>.md` and `index.json`; Core writes manifest | Step 3, then appendix in Step 5 |
| `robomate report [run]` | missing | CLI parser has no `report` command | Step 3 |
| `robomate abandon` | missing | Common workflow statuses and CLI parser lack `abandoned`/`abandon` | Step 4 |
| Repo `nits_issue` | partial | Layers parses/validates it; `test_precedence_for_each_key_class_on_linux_and_windows_paths` | No closeout consumer or configured-nit validation; Step 1 |
| Repo `followup_label` | partial | Layers tests precedence and default | No closeout assignment consumer; Step 1 |
| Repo `closeout_steps` | partial | Layers tests validated ordered steps | No frozen task snapshot or step-result coverage; Step 1 |
| `ask_user` inbox, `robomate inbox/answer` | done | #129–#132 / PRs #138, #139, #141, #143; `tests/test_rpc.py::test_hub_answer_needs_the_operator_and_answers_once`, `test_escalation_is_left_only_once_the_operator_has_answered`; CLI inbox/answer tests; scripted #133 / PR #153 and real #134 | #185/#186 are additional visibility, not missing escalation authority |
| Resume (§7.5, reduced #18) | partial | Recovery: bridge-session restart, worker detach/takeover, CLI identity reuse and mock crash matrix; #146 / PR #156, #165 / PR #172, #173 / PR #175 | Combined restart/reconciliation proof and live component kills; Step 7 and Step 8 |
| Host standby (#19) | done | PR #200; `tests/test_sweeper.py::test_wake_loop_reports_pause_and_suppresses_expiry_and_stalls`, store injected-clock cases; [host-standby.md](host-standby.md) | Real hardware #205 remains owed; #202/#203 are separate follow-ups |
| `robomate log --timeline` (#20) | missing | CLI has no `log`; `scripts/hub-report.py` has no timeline mode | Step 6 |

Done-when inventory (split into independently checkable clauses):

| §13 clause | State | Evidence on main | Remaining work |
|---|---|---|---|
| All-three collection survives restart | done | Collection: `test_isolated_collection_scenario[False]` restarts app/store and compares retained collection | Preserve through new done gate |
| Crashed-worker collection survives restart with explicit missing reason | done | Same test `[True]`; Views `test_scripted_closeout_real_processes` tests real processes, restart and files | Early loss reason remains #424 |
| Report identical/conflicting retries | done | Collection: `test_retry_membership_limits_and_secrets` | Preserve atomic receipt/event behavior |
| Caller identity/run membership validation | done | Same test and `test_unassigned_lost_participant_and_sequential_runs` | Preserve stale-session/release guards |
| Report size validation | done | `test_closeout_safe_errors_and_part_limits`, `test_request_survives_restart_and_a2a_splits_parts` | No relaxation of part/result bounds |
| Known credential validation | done | `test_retry_membership_limits_and_secrets`, safe-errors test | Detection only covers loaded known values; no universal secret claim |
| Pause-aware report expiry | done | `test_pause_expiry_restart_and_missing`, `test_host_pause_shifts_deadline_before_expiry` | Keep #386 policy and #424 reasons consistent |
| `done` refuses unmerged PR | missing | Core has no such branch or test | Step 2 negative cases for default `deliver: merge` |
| `done` refuses missing completed close-out | missing | Core has no such branch or test | Step 2; explicit `closeout: false` exception |
| `CloseoutResult` rejects missing pending finding | missing | No result class/validator | Step 1 |
| `CloseoutResult` rejects missing action URL | missing | No result class/validator | Step 1; issue and nit |
| `CloseoutResult` rejects missing `none` reason | missing | No result class/validator | Step 1 |
| `CloseoutResult` rejects missing step | missing | No result class/validator | Step 1; duplicate indices too |
| Report lists every finding and disposition | missing | Views contains observations, not authoritative finding dispositions | Step 3, integrated by Step 5 |
| Hub kill/restart resumes without duplicate actions | partial | CLI `test_up_down_reuse_and_duplicate`; durable event/idempotency tests; mock crash matrix | Live HTTP interruption with connected bridges and end-to-end action counts; Steps 7–8 |
| Worker kill/restart resumes without duplicate actions | partial | `test_a_restarted_worker_checks_in_at_once_and_finishes_its_task`, `test_worker_join_whoami_and_takeover_over_a2a` | Real mid-task kill and combined reconciliation; Steps 7–8 |
| Orchestrator kill/restart resumes without duplicate actions | partial | `test_worker_task_survives_orchestrator_session_restart`, mock crash matrix; M1 live evidence | New closeout/gate boundaries plus M4 live kill; Steps 7–8 |
| Simulated standby loses no one | done | Sweeper test above; store `test_heartbeat_during_grace_keeps_worker_and_task_live`, `test_worker_liveness_across_consecutive_pauses_and_pre_pause_checkin` | Do not repeat the grace implementation; run regressions in Step 7 |
| Real robomate run ends with follow-ups filed | missing | #215/#356 component scenarios are scripted; no M4 acceptance artifact | Step 8 |
| Real run closes any used counter claim | missing | No M4 acceptance artifact; #408 defines conditional obligation | Step 8 verifies the used claim, or records no counter used |
| Real run needs no operator chat | missing | Operator-channel acceptance proves escalation, not autonomous M4 close-out | Step 8 records channel use and any failed attempts |

## Facts from the current code that shape the plan

1. Collection is already durable, bounded and keyed to the run. `closeout.finalize` uses
   the same SQLite write lock as report submission. Lost participants remain expected;
   current early finalization can label them `run_finalized` before expiry (#424).
   Do not replace this protocol with task results or a second retrospective round.
2. Per-author exports already escape prose, URLs and controls, use ordinal paths, and
   expose export failures without undoing done. Startup retries finalized **done** runs.
   #423 owns retry completion and retention; a missing index alone cannot distinguish
   a crash from an operator deletion, so its decision adds a durable completion record.
   #431 replaces the Markdown escaping with fenced literal text.
3. `task.role` is free text in SQLite, but public result dispatch has only three typed
   roles. The existing Collection test's prose completion of a string `closeout` task
   does not prove any `CloseoutResult` rule. Validation must use the stored task role.
4. M2 already supplies run identity, sequential workflows, an immutable statement,
   manifest, queue, agent profiles and the run branch. `done` can currently release busy
   workers or start the next run; strengthening it requires updating test setup and
   mock consumers, not weakening the new rail for tests.
5. `WorkflowPolicy` lacks `deliver` and `closeout`; the layer reader parses the three
   repository closeout keys but nothing consumes them. Snapshot closeout settings on
   assignment so later config edits cannot change the validator's obligations.
6. #297 tightened statement reading, acceptance reviews and cheap nonblocking fixes;
   #302 avoids unnecessary NONBLOCKING tasks. Neither computes a durable pending finding
   set. The current guide uses implementer-shaped CLOSE-OUT tasks and conditional counter
   work. Step 1 adopts §7.3's actual role and default closeout, keeping #408's removal of
   routine roadmap work. An empty typed closeout is valid; retrospective collection is
   still independent and missing retrospectives never block done.
7. Resume is already a guide procedure, transport retries and idempotency, not a workflow
   engine. The [recovery guide](agent-recovery.md) contains prepared-run-era limitations;
   M2 join/takeover has since landed. Update stale instructions with the launcher handoff,
   and add only uncovered crash boundaries.
8. Main's `event` and `decision` tables have no workflow foreign key; `message` has only
   nullable `task_id`. Gate readings and operator questions already carry workflow IDs.
   A timestamp range cannot attribute decisions or late operator answers to a run.
   Step 6 adds explicit storage attribution before Step 4 relies on it; legacy rows
   without an authoritative reference remain unscoped.

## Decisions settled after merge

The plan merged with these points open or unstated. The owner delegated them on
2026-11-02, as long as they fit the project's intent and the spec, and may redirect any
of them. They live here so a step issue can stay a short pointer. The sections below
already reflect them.

1. **Abandon with a queued run: no hold.** Alice starts the queued run through
   `start_workflow()`, as [Abandonment](#abandonment) describes. At most one run can be
   queued, so the case is rare. A hold would keep Alice and the workers polling, and
   idle holds cost tokens (§7.2). The operator keeps control without new code by
   stopping the agents first, which Step 4 documents.
2. **Typed closeout on every run.** `closeout` keeps §7.2's default of `true`, even when
   nothing is pending. #408 removed roadmap upkeep, not close-out itself, and the typed
   task is what stops nonblocking findings being dropped (#15). On a run with nothing
   pending it costs one short task. `closeout: false` stays available per run.
3. **Export retention (#423): an export is written once, and deleting it is final.**
   Rewriting every completed run at each hub start is wasted work, and it would bring
   back files an operator removed on purpose.
   - After `index.json` is written, record completion in the database, for example an
     exported-at column on `closeout_collection`. This needs a migration, so #423 claims
     its version on #409 first.
   - Startup retries only finalized `done` runs with no completion record. A failed or
     interrupted export has none, so it retries at each start and `status` reports it
     again if it still fails.
   - An operator removes an export by deleting its files or run directory, with no new
     command, and the hub never recreates it. The docs must say this removes only the
     exported files: the reports stay in `hub.db` until that hub's state is removed. A
     purge command is out of scope.
   - At migration: `index.json` present means complete; report files with no index are
     an interrupted export and stay unmarked so they retry; no index and no report files
     means complete without writing anything. That last case can't be told apart from a
     deliberate deletion, and a run that really crashed there loses only its export,
     because its reports stay in the database.
4. **Order: §13 doesn't change.** Lane A's queue stays first: #395, #245, #246, #247,
   #423. Triage opens all the step issues now. Steps 1 and 6 start when Lane A reaches
   them or a lane is free, ahead of M3, with Step 2 after Step 1. This follows the
   precedent of #215 and #356, doesn't delay M2, and landing after #247 avoids launcher
   churn. Steps 3–5 follow the edge table. Steps 7 and 8 stay behind M2 and M3.
5. **Readable exports (#431) land before Step 3.** Today's export renderer backslash-escapes
   all punctuation and shows line breaks as `\u000a`, so URLs and SHAs are hard to read in
   a terminal. Step 3 reuses that renderer for `report.md` and `robomate report`, so fixing
   it first means the main operator report inherits the fix. #431 and #423 change the same
   file, and whichever lands second rebases.
6. **Filing route (#366): workers keep filing, with no permission change.** §7.3 gives
   filing to the implementer, and pre-approving `gh issue create` would widen a permission
   to work around a safeguard that may be behaving correctly. If a harness refuses, the
   worker returns `blocked` with the rejection named and uses no other route. Alice asks
   through `ask_user`, the operator files the issue, and Alice records its number. The
   guides' FOLLOW-UP route says this. Codex and OpenCode workers filed #423, #424 and #412
   with no refusal on 2026-11-01, so the refusal isn't constant.

## Design

### Close-out, delivery and the run report

Use §7.3's result shape and exact coverage rules. Pending findings are all typed
nonblocking reviewer findings in this workflow minus IDs resolved by later implementer
results. Disputed IDs remain pending. A repeated ID is one obligation, retaining its
review provenance; contradictory legacy records are surfaced, not guessed away. Blocking
findings and resolved nonblocking findings also appear in the report, with their recorded
outcome or explicit absence of a disposition. Agent observations are a separate appendix.

At task assignment, store a typed closeout context: pending IDs with summaries/provenance,
ordered indexed steps, effective `nits_issue`, `followup_label`, and the run's PR/MR URL.
Append a readable rendering to the instructions and return the typed fields through both
assignment and reattachment paths. Validate the completed result against that snapshot
inside the completion transaction. Reject omissions/duplicates without closing the task.
A `done: false` step with a note is permitted and visibly highlighted, as §7.3 requires;
this plan adds no step-truth gate. Follow-up issues reference the PR and finding ID and
carry `followup_label`; the worker verifies its forge writes. Result validation checks
shape and coverage, not whether an arbitrary issue's content is correct.

`deliver: merge` remains default; `deliver: pr` is an explicit create-once policy choice,
not inferred from the statement. `closeout` defaults true. Bind the workflow to one
canonical PR/MR derived from verified typed work/review results and reject conflicting
bindings. Use the existing stored branch for the stable work label (#315); no second label
counter or title-matching policy is needed. §7.2 already locks one run to one PR/MR;
Appendix A's historical “decide in M4” wording is updated in Step 2. #389's non-PR mode
would change that locked decision and remains the owner's separate decision.

The done operation performs a fresh forge gate read for the bound PR and expected head,
then commits against the same workflow and unchanged binding. Fail closed if the forge
read fails or the binding changed while awaiting it. For `merge`, require `merged` and
record the actual merge SHA separately from the approved PR head. For `pr`, require the
approved open head and the normal review/CI gate. Require completed typed closeout unless
explicitly disabled. These are the two §7.3 coded predicates; do not add a third
no-active-task predicate. Alice still follows the guide to settle outstanding work before
requesting done. Preserve the existing approved-head/CI/review safeguards; no prose,
retrospective claim or old “clean” reading can substitute for the forge state. Replay
after queue activation must not finalize the
next run accidentally: fence the operation with its original run identity.

Render `runs/<run_id>/report.md` after terminal state commits, using a consistent read
snapshot of typed data. Include statement source/link and SHA, run/policy/status, agent
profiles/harnesses, review rounds and references, PR/MR, approved head and distinct merged
SHA, every finding with its disposition/link or unresolved marker, all closeout steps,
and each `none` reason. Historical runs may lack typed data; say “not recorded”, never
reconstruct evidence from task prose or make new forge calls while rendering. A disabled
closeout is labelled explicitly. The headline reports delivery state, unresolved findings,
incomplete steps and, after Step 5, submitted/expected retrospective counts.

Reuse the export renderer as #431 leaves it (fenced literal text with visible control
escapes) and private permissions for all untrusted fields. Reports do not
execute text, follow links, file issues, or certify agent claims. CLI prints the same safe
content; status contains the compact headline, not report prose. `report [run]` defaults
to the current run or newest terminal run, selected within the chosen hub; an explicit
unknown run refuses. While active it prints a clearly labelled draft projection without
creating the terminal file. Final files are readable offline. Missing legacy fields and
“collection not requested” are distinct from an empty or missing report.

Export is post-commit and retryable: failure records an observable error, retains terminal
state and is retried after restart. Step 3 extends #423's retention/completion mechanism
to the main report; Step 5 extends it to the appendix/abandoned exports. One completion
record covers a run's whole artifact set and is written after its last file. A run
already complete when Step 3 lands gets no `report.md`; the reader renders it from stored
data. Never resurrect intentionally removed sensitive artifacts or declare a partial
export complete.

### Abandonment

§7.2 supplies the operator-only exit, bypass of done checks, retained worktrees and
`abandoned` state. Step 4 fills in its unspecified sequencing:

- `robomate abandon [--reason TEXT]` targets the selected hub's live run only. Omitted
  reason is recorded as “not supplied”; an explicit blank reason is rejected. Record
  operator attribution/time and the optional reason durably. No worker/orchestrator
  tool can set this state, including through `set_workflow_status`.
- Atomically cancel unfinished tasks of that run with an abandonment note, clear their
  current-task ownership, finalize any requested collection, and mark the run abandoned.
  Completed results, findings, statements, questions, decisions, branches and worktrees
  remain. Late results/reports cannot mutate the terminal run. Worker release, when
  policy calls for it, is cooperative; the operation never kills a process or claims
  a release acknowledgement.
- Follow §7.2's worker policy: with a queued run, keep workers joined under either
  `on_done` value; without one, `release` releases workers and `standby` keeps them idle.
  Canceled task ownership is cleared, but uncommitted edits are preserved; a worker
  finishes handling its terminal old task, returns to `await_assignment`, and applies
  the normal clean-workspace checks before accepting new work. Retention is not permission
  to overwrite abandoned work.
- In the same transaction, enqueue a run-scoped `workflow_abandoned` control event with
  the original workflow/run ID, and wake Alice's `wait_for_event`. The join/get_state
  re-briefing includes the latest abandoned run and its terminal status even after a
  different run starts. Update the orchestrator guide's kickoff, event routing, resume
  and “end only at done” wording, plus surviving launcher's terminal-exit guards.
  Alice acknowledges the notification after reconciling durable state, never assigns
  further work or calls done for the abandoned run, and reports its terminal outcome.
- The abandon transaction leaves the queued run queued. **Alice**, not an operator CLI
  or worker, then calls the existing `start_workflow()` to start the oldest queued run
  after observing the completed abandonment transaction. This is the normal §7.2 queue
  path, with the retained workers; no new approval or queue-hold policy. If the queue is
  empty, Alice ends her turn with `on_done: release`, or keeps waiting for
  `work_submitted` with `standby`. A restarted Alice makes the same choice from durable
  terminal/queue state; an abandoned run is never reopened. Duplicate notifications
  reconcile against the already active next run rather than starting a second run.
- Abandon itself never starts the queue, and only Alice does. So when the environment
  is broken and the queued run shouldn't start, the operator stops the agents and their
  launchers before running `robomate abandon`; the run stays queued. A restarted Alice
  starts it, so the operator restarts Alice only after fixing the problem. Step 4
  documents this order in the user guide.
- Preserve question rows and their actual answered/withdrawn/unanswered provenance.
  Default inbox and pending-question counts include only live-run questions; unanswered
  questions from done/abandoned runs are historical and non-actionable, not fabricated
  answers or withdrawals. Direct `get_operator_answer` still returns the original
  status plus run/terminal metadata. `hub.answer` checks the question's workflow under
  its write lock and refuses a terminal-run question without writing an answer, event
  or decision. If answer wins the race before abandon, its event is attributed to the
  old run and cannot resume the next one. The abandoned report shows that history;
  this adds no #185 history UI and does not grant authority in a later run.
- Use Step 6's persisted event/decision/message attribution. Exclude terminal-run work
  events (including old `user_answered`) from actionable event selection/redelivery;
  preserve them for timeline/history without forging an acknowledgement. Run-scoped
  abandonment notifications are the explicit terminal control exception, not sources
  for new tasks. Hub-scoped diagnostics and queued `work_submitted` remain deliverable.
  Reject assignment sourced from an event belonging to a different/terminal workflow;
  Alice also compares event run IDs with live state before acting. Legacy events are
  backfilled only from authoritative references as in Step 6; ambiguous legacy work
  events are surfaced for reconciliation, never treated as a later run's authority.
- Write its manifest and report with status `abandoned`, reason/actor/time, partial work,
  canceled task IDs, PR state as last recorded (possibly absent or already merged), all
  findings and unresolved dispositions, incomplete/not-run closeout steps, and the
  collected/missing retrospectives once Step 5 lands. Abandonment means no delivery
  assertion, not “PR unmerged”; it must not close a PR or issue, delete a branch, or mark
  a counter shipped. An outstanding counter claim requires the usual operator/Alice
  routing to release or close it out; the report lists any unresolved obligation.

This is an idempotent terminal operation scoped to the original run; retries cannot
abandon the queued or next run. Done and abandon race under the same lock: one terminal
outcome wins. This follows the existing worker-retention policy and two-predicate done
check, rather than making either an operator decision. The plan adds neither queue
cancellation nor a no-PR delivery policy.

### Order, launcher handoff and neighboring issues

§13's order stays **M2 and M7, then M3, M4, M6**. Planning now is not permission to move
M4 ahead of them. Technically, Steps 1, 2 and 6 need only merged M2 infrastructure, so
they may start ahead of M3 once Lane A's queue reaches them or a lane is free, as with
#215/#356. M4 acceptance still follows §13. Steps 3–5 depend on
specific work below, including Step 6 before abandonment. Step 7 tests the successor
path; Step 8 waits for M2 and M3 completion. No M4 feature depends on a particular new
M3 harness adapter.

M2 Steps 8–10 (#245/#246/#247) remain their own PRs. Cleanup must retain reports; clone
fallback matters to remote live evidence; the successor launcher carries report tools,
resume guards and run/session identity. #268 and operator #269 must validate scriptable
issue/work-file startup on Bash and native PowerShell before Step 11 (#248) retires
prepared runs. Step 11 still waits for M7 and the proven Denise handoff; Step 12 (#249)
then proves M2. **Do not make #248 depend on all of M4 or on #357.** Hub collection and
per-author exports already replace file-only reporting; #423 precedes retirement as
currently scheduled. Step 11 must preserve existing report archives and document this
interim reader while the combined run report is pending.

#357 remains a separate integration issue/PR (Step 5), keeping its appendix, abandonment
export, parity and real-process acceptance. Its file-prompt scope is conditional on the
code at pickup: update `prepare-run.py` and its fixtures if still present, and #247's
successor if present. If #248 has removed preparation, record that deletion as satisfying
the old-path removal; test only the surviving launcher. Never restore retired scripts or
postpone M2 retirement for that edit. This is a proposed clarification to #357, not an
issue edit in this PR. Current prepared runs keep their existing prompts and archives;
newly generated runs adopt hub-only reporting when Step 5 lands.

The post-closeout gap is accepted: worker reports stay immutable
`collected_before_closeout`; the typed `CloseoutResult` plus Alice's `after_closeout`
report cover later dispositions. No second worker amendment phase or new release gate.
Lessons go to operator triage (#3), nits to configured `nits_issue`, and substantial
follow-ups through §7.3. Nothing posts from a retrospective automatically.

| Issue(s) | Disposition and dependency/conflict |
|---|---|
| #14 | Step 2 owns delivery policy and enforced done; retain its issue/work-file goal tests on whichever launcher exists. Old PoC manual merge is historical, not M4 work |
| #15 | Fold into Step 1. #297/#302 cover earlier review handling only. Replace the obsolete reviewer-only FOLLOW-UP/lesson-posting proposal with §7.3 implementer judgment, typed coverage and separate retrospectives |
| #17 | Fold into Step 2: run PR binding plus existing durable branch label settle it; no multi-PR workflow |
| #18 | Step 7 owns reduced reconciliation and missing crash cases; keep policy in the guide, no phase/checkpoint engine |
| #20 | Step 6 supplies `robomate log --timeline`; supersedes the old script spelling |
| #16, #97, #215, #216, #356 | Already closed. #16's roadmap policy was superseded by #408; do not restore roadmap adjudication. #97/PR #218 was interim files, #215 collection, #216 split, #356 views |
| #357 | Step 5, after Steps 3–4 and #356; scope retained with launcher clarification above |
| #358 | Continue ordinary-run evidence now, 3–5 runs across its harness/OS/remote/crash matrix; compare hub and file reports while both exist. After removal mark that comparison inapplicable and continue hub views. Not replaced by a scripted test |
| #359 | Starts after Step 5, at least two ordinary hub-only runs across its matrix; Step 8 may contribute, not claim every matrix row from one run |
| #386 → #424 | Keep Lane D order: policy window first, early-loss semantics next. Steps 1/3 avoid hard-coded window values; Step 5 and Step 7 wait for both to stabilize reads/status/export reason assertions. They touch the same collection/tests, so serialize integration |
| #423 | Keep Lane A, before #248, with the retention decision under [Decisions settled after merge](#decisions-settled-after-merge). Step 3 waits for its retry/retention seam; Step 5 extends it. No competing marker/deletion policy in this plan |
| #431 | Before Step 3, so the run report inherits literal rendering. Rebases with #423 on `closeout_export.py`. No counter |
| #272, #396 | Track 1 owner decisions on second-timeout actions and silent interactive sessions. Not prerequisites for typed closeout. Step 7 preserves diagnostic-only stalls; acceptance escalates/records a real stall rather than inventing cancellation authority |
| #286 | Held aside by owner; do not fold in its guide redesign or new result fields. Step 3 reports attributed typed facts and missing data, which prevents report rendering from restating criteria as results |
| #298 | After M7 on its own track. M4 implements §7.3 `followup_label` only, not configurable project/board actions. If #298 lands first, consume its established rules without broadening worker authority |
| #366 | Track 1, route decided: workers file, and the operator files if a harness refuses. A denied follow-up is blocked/escalated through the operator channel, never silently dropped or delegated by assumption; Step 8 needs an environment where required authorized filing succeeds |
| #341, #334, #351 | Independent diagnosis/guard work. Keep already-held assignment redelivery and fields in Step 1/7 tests. Do not infer #341's cause without its operator archive; incorporate a proven overlapping fix if it lands |
| #389 | Defer to owner: changes a locked decision and MVP non-goal. This plan preserves merge/pr delivery modes; abandon is not successful evidence-only delivery |
| #185 | Defer operator question history UI; Step 6 includes closed-set withdrawal metadata but no question/reason prose or `inbox --all`. It does not fully close #185 |
| #186 | Defer correlation hints: useful UX, not required for inbox authority or resume correctness |
| #202, #203 | Standby follow-ups remain separately scoped: active-time stall ages and rejected-heartbeat recovery after actual loss. Grace tests already meet simulated-standby clause; live failures are recorded against these, not hidden as covered |
| #182, #205, #220 | Ride-along evidence: recovery, Modern Standby, legacy file writes respectively; details below. #220 ends when its prepared path ends, not by claiming hub-only evidence is a file write |
| #271, #219, #232, #189 | Windows closeout grants, junctions, scratch and credential copies belong to their existing lanes/M2 retirement. Hub-only reporting removes new team file-write grants; retain historical files, Denise reports and security boundaries |
| #263, #336, #300 | Independent model-profile drift, wall-budget policy and progress wording. Report stored profile facts; do not guess an actual model or add time-driven lifecycle actions |
| #309, #395, #347, #346 | M2/prepared-path save-failure, sandbox grants, partial worktree startup, interactive-permission evidence; owned by those lanes. Preserve their behavior in launcher parity, without replanning their implementations |
| #194, #304 | Redirected Windows encoding and post-MVP clock handling stay separate; new views sanitize controls and use the existing output conventions |
| #99, #88, #235, #11–#13, #22–#23 | Broader authority/role principles, M3 certification/waits and M6 accounting/retention remain there. M4 does not relax existing safeguards or absorb these milestones |

## Spec deltas

Apply these with the implementation, not in this planning PR. No locked decision is
reopened here. If an implementer finds a conflict with the design of record, ask the
operator through Alice before implementing it.

| Area | Clarification/change | Applying step |
|---|---|---|
| §7.3, §8 task contract | Stored typed context, role-specific dispatch, assignment/reattachment fields; guide uses default typed closeout even with zero items, without routine roadmap work | 1 |
| §7.2, §7.3, §8, Appendix A | Durable PR binding, explicit delivery/closeout policy; fresh gate and run-fenced finalization; settle #17 against already locked one-PR rule | 2 |
| §7.3, §11 | Report source fields, missing historical evidence, active draft/default selection, headline, private post-commit export/retry using #423 semantics | 3 |
| §7.2, §7.3, §7.4, §7.5, §8, §11 | Abandon audit/cancellation; existing `on_done` retention; scoped notification, Alice queue start/exit/standby, terminal-question refusal and late-event isolation. §7.2's “the second starts only after the first is `done`” also covers `abandoned`, where Alice starts it and nothing starts it automatically | 4 |
| §7.3, §8, §13; closeout/recovery/user docs | #357 appendix, abandoned per-author export, launcher handoff, accepted post-closeout gap and operator routing | 5 |
| §8, §11 | Persist workflow attribution for events/decisions/messages; bounded read-only timeline, legacy unscoped handling and additive event metadata; no prose summaries | 6 |
| §7.5; recovery and role guides | Closeout/done/abandon recovery examples; successor launcher commands and live evidence limits | 7 |
| §13 | Link verified acceptance evidence; retain milestone order | 8 |
| §7.3 / tool window and missing semantics | Owned by #386/#424; consume their final contract, do not edit it concurrently | Their own PRs, before 5/7 |
| Export retention/completion | Owned by #423, applied to run reports in 3/5. Replaces §7.3's “finalized runs are exported again on the next hub start” | #423 then 3/5 |
| §7.3 export rendering | Owned by #431: replaces “Prose and URLs are Markdown-escaped literal text” | #431, before 3 |

## Steps

Every numbered step is one issue and one PR, reviewable alone and leaving main usable.
Existing issues are reused where named; triage opens short pointer issues for the other
steps, all with milestone **M4 Close-out and resilience** and label `track-2-mvp`.
A step issue links its stable heading, says to implement and verify it consistently with
the spec, and lists dependencies; it does not repeat this design. Triage can cross-link
#15/#17 to the owning step instead of opening duplicate implementations.

```
Ready     [1 closeout]                         [6 attribution + timeline]
Then      [2 done + delivery] ◀── 1
Next      [3 run report]      ◀── 2, #423 retry/retention, #431 literal rendering
Then      [4 abandon]         ◀── 3, 6
Then      [5 #357 integration] ◀── 4, #386 → #424
Then      [7 resilience gaps] ◀── 5, M2 successor + retirement
Last      [8 M4 acceptance]   ◀── 7, M2 acceptance + M3 completion
```

Read the table as the authoritative edge list; arrows through a chain carry its earlier
prerequisites. Step 6 can run in parallel with Steps 1–3; Step 4 waits for it. Steps 1/6
have no technical M3 dependency and may start early, as
[Decisions settled after merge](#decisions-settled-after-merge) sets out.

| Step | Needs (landed unless stated otherwise) | Reason for each edge |
|---|---|---|
| 1 | Current M2 layers/run identity/join/branch work; #215/#356 | Reuse existing settings, attribution, assignments and collection; all already on main |
| 2 | 1 | A typed completed closeout and `closeout` policy must exist before enforcing it |
| 3 | 2; #423; #431 | Report delivery/finalization facts and retry/retention contract must be stable; the report reuses the renderer #431 fixes |
| 4 | 3; 6 | Reuse report artifacts (3); stored event/question attribution enables terminal isolation and Alice notification (6) |
| 5 | 4; #386 and #424 (in that order); #356 | Report and abandon exist via 4; integrate final window/reason behavior and existing safe exports |
| 6 | Current run identity, events/results | Attribute records at their writers, then expose the read projection; no new closeout dependency |
| 7 | 5; M2 #247/#268/#269/#248 | Exercise all new terminal/collection seams and the validated surviving launcher, not removed wrappers |
| 8 | 7; M2 #249; completed M3 | All feature/crash tests (including 6 via 4/5/7), preceding milestone acceptance and harness certification precede final M4 evidence |

### Shared counters

No values are named or claimed by this plan. Only the implementing run that needs a
counter reads/edits reservations [#409](https://github.com/RoboNater/robomate/issues/409),
using AGENTS.md's claim/advance/shared-body procedure before writing it into code.
The reviewer verifies the claim; post-merge closeout marks only that claim shipped or
releases it if unused. Migration PRs serialize according to their actual claims, including
other lanes; a dependency diagram is not a reservation.

| Step | DB migration | Wire `hub.schema_version` | Reason |
|---|---|---|---|
| 1 | Yes: stored closeout context on task | No change expected | Additive role/result and assignment fields; test old role consumers and dispatch explicitly |
| 2 | Yes: durable workflow PR/head binding | No change expected | Policy JSON additions need no separate migration; finalization must bind a run durably |
| 3 | No new migration expected | No change expected | Derived report; reuse #423's durable retry mechanism rather than invent a second marker |
| 4 | Yes: workflow status and event-kind constraints plus abandonment audit | No change expected | Reuse Step 6 attribution; additive operator method/control event; check status/hold/launcher consumers |
| 5 | No new migration expected | No change expected | Existing reports/context and safe renderer |
| 6 | Yes: nullable workflow foreign keys/indexes on event, decision and message | No change expected | Persist new writer attribution; backfill only authoritative references; additive response fields |
| 7 | No migration expected | No change expected | Tests/guides, production changes only for reproduced defects |
| 8 | None | None | Evidence only; the real subject run may separately use a counter |

If a step discovers a breaking wire change or additional persistence need, stop that
part and resolve it with Alice; claim the needed counter before using it. Do not reserve
speculative numbers for later steps. #386/#424/#423 own their own counter decisions.

### Step 1 — Typed closeout task and repository settings

**Scope.** Fold #15 into the §7.3 task/result contract. Add `TaskRole.CLOSEOUT`,
`CloseoutResult` to the strict common models and exports, `closeout: true` policy default,
stored context, served `closeout` guide and worker/orchestrator result/assignment handling.
Consume all three repository settings; preserve forge-neutral guides and worker A2A.

**Approach.** Compute pending IDs from typed results, snapshot under the assignment
transaction and append context to the instructions. Return it on await, held redelivery
and takeover; validate completion against it using stored role, not the presence of an
`outcome` key. Require exactly-once finding/step coverage, action URL/reason requirements,
configured nits target and notes for false steps. Empty item/step lists are valid. Blocked
and failed results retain their explanation and do not count as completed closeout.
Keep raw legacy internal results readable but prevent them from satisfying new typed
coverage. Update mock fixtures to submit the proper result. Guide judgment remains with
the implementer; no automatic issue filing or truth certification.

**Tests.** Add `tests/test_closeout_task.py`: omitted/duplicated/unknown pending IDs and
step indices, missing issue/nit URL, missing `none` reason, nit without configuration,
false step without note, valid empty task, unresolved/disputed/later-resolved findings,
blocked/failed outcome, settings snapshot after config edits, restart, idempotent retry,
late result and reattachment fields. Rejected completion leaves task open without result
or completion event. Test both bridges/tool lists, generic and implementer standing roles,
reviewer rejection, safe assignment text, bounded assignment/result sizes and same
source-event replay. Oversized context is rejected before writes; transport bounds are
never raised to fit a task. Reuse collection ordering without treating the retrospective
as the closeout result.

```sh
uv run --locked pytest tests/test_closeout_task.py tests/test_models.py tests/test_config_layers.py tests/test_protocol.py tests/test_worker_client.py tests/test_worker_tools.py tests/test_guides.py tests/test_mcp.py tests/test_closeout.py
ROBOMATE_TEST_DB_LATENCY_MS=250 uv run --locked pytest tests/test_closeout_task.py
```

**Size:** medium to large. **Suggested model:** strong implementer and strong reviewer
(tier 1). **Dependencies:** current infrastructure in the edge table. **Counters:** table
above. Update §7.3/§8 and served guides in this PR.

### Step 2 — Enforced done, explicit delivery and one PR per run

**Scope.** #14 plus #17's remaining enforcement; durable binding, `deliver` policy through
layers/submit/launchers, fresh gate and completed typed closeout check. Do not add a
no-active-task coded predicate beyond §7.3; settling work remains guide procedure.

**Approach.** Bind results to the run's PR and expected approved head, rejecting conflicting
PRs; reuse the run branch for label stability. Route the async forge read through the
existing gate implementation, preserving origin binding and normal review/CI checks.
Recheck run/binding at commit; ensure a retry after queue advancement is a no-op for the
old run rather than a done on the new run. Missing reports remain nonblocking. Update
all existing done fixtures explicitly; no production bypass or inference from goal text.
With `deliver: pr`, closeout occurs after approval and the report clearly says unmerged
by policy. Keep prepared goal tests only while that script exists, and update the
successor path if landed. Alice still alone merges; the hub adds no merge tool.

**Tests.** Add `tests/test_done_gate.py`, fake gates for both forges: unmerged/closed PR,
wrong URL/head, stale cached gate, unavailable forge, missing/failed/legacy prose closeout,
valid merge and valid explicit PR mode, `closeout: false`, unchanged default,
concurrent result/binding change and done replay with queued workflow. Verify default
issue and work-file submissions both merge; PR mode is explicit/durable. Preserve approved
head versus squash merge SHA. Existing lifecycle/collection tests must still pass with
new truthful fixture setup.

```sh
uv run --locked pytest tests/test_done_gate.py tests/test_store.py tests/test_orchestrator.py tests/test_rpc.py tests/test_merge_gate.py tests/test_gitlab_gate.py tests/test_closeout.py tests/test_closeout_export.py tests/test_config_layers.py tests/test_cli.py
ROBOMATE_TEST_DB_LATENCY_MS=250 uv run --locked pytest tests/test_done_gate.py
```

**Size:** medium to large. **Suggested model:** tier 1 implementer/reviewer.
**Dependencies:** Step 1. **Counters:** table. Update §7.2/§7.3/§8 and Appendix A; tests
for any still-present preparation entry point run with that PR as well.

### Step 3 — Run report, reader and status headline

**Scope.** Typed report renderer, `runs/<run_id>/report.md`, operator `robomate report
[run]` with hub selection, and the status headline. Includes normal done and active draft;
Step 4 adds abandoned input and Step 5 adds retrospectives.

**Approach.** Read a consistent run-scoped snapshot, retaining every finding across rounds
and linking resolutions/dispositions by ID. Render missing legacy fields and disabled
closeout explicitly. Reuse #431's literal rendering and #423's completed/retry/retained
artifact contract. Expose an operator reader through the existing authenticated CLI route;
require the operator credential, with no worker state-directory access. Terminal export
failures remain observable while the queue proceeds. Read offline Markdown directly with
the hub stopped; document its path
and sensitive-artifact handling in the user guide, including that deleting an exported
file leaves the data in `hub.db`.

**Tests.** Add `tests/test_run_report.py`: multiple rounds, blocking/resolved/pending and
disputed findings, every disposition action/link/reason, false steps, statement source and
SHA, profile/approval/merge identity, no-collection and old-data cases, draft versus final,
explicit/default/unknown run selection with a new live/queued run, missing operator
credential, invalid/path-like run IDs, adversarial text, private modes, atomic interrupted
write, restart retry and intentional retention/deletion semantics. Pytest-owned CLI
subprocess coverage compares output with terminal file and status headline; no report
text leaks through compact status.

```sh
uv run --locked pytest tests/test_run_report.py tests/test_closeout_export.py tests/test_cli.py tests/test_rpc.py tests/test_doc_links.py
ROBOMATE_TEST_DB_LATENCY_MS=250 uv run --locked pytest tests/test_run_report.py
```

**Size:** medium. **Suggested model:** tier 1 for data provenance review; capable tier 2
implementer. **Dependencies:** Step 2, #423 and #431. **Counters:** table. Apply §7.3/§11 reader,
headline and artifact clarifications. No new retention decision in this step.

### Step 4 — Operator abandonment and terminal-run isolation

**Scope.** `abandoned` status, operator-only `hub.abandon`/CLI `abandon [--reason TEXT]`,
transactional task cancellation, policy-based worker retention, scoped Alice notification,
queued-run handoff, terminal-question handling and abandoned manifest/report.

**Approach.** Implement the abandonment design above. Finalize collection in the same
transaction using `closeout.finalize`; the main report includes available observations'
counts and unfinished state, but per-author abandoned exports/appendix wait for Step 5.
Fence retries by original run identity. Preserve worktrees and evidence. Refuse worker or
orchestrator attempts to set abandoned even though it becomes a workflow enum member.
Use Step 6 attribution for `workflow_abandoned`, task-cancellation events and the audit
rows. Apply live-run inbox/status filtering and terminal `hub.answer` refusal atomically;
exclude old actionable work events without erasing historical records. Update Alice's
kickoff/event/resume/terminal instructions and launcher guards for the queue/standby/exit
choices above, including an Alice already blocked in `wait_for_event`. Source new tasks
only from the current run's valid events. Document outstanding counters without granting
workers lifecycle or process authority. Add the stop-first order to the user guide.

**Tests.** Add `tests/test_abandon.py`: active/paused/escalated exits, omitted/nonblank
reason, credential refusal, no live run, original-run retry after new activation, race
with done/submission, unfinished tasks canceled once, preserved terminal results and open
questions, late result/report refusal, release/standby with an empty queue, both policies
with a queued run retaining workers, and policy-appropriate waking of worker holds.
A waiting Alice receives the scoped abandonment event, starts the retained queue exactly
once with `start_workflow`, or exits/waits according to empty-queue policy. Restart Alice
before notification, after notification and after queued activation; no abandoned run is
reopened. With no Alice running, abandon leaves the queued run queued and no hub path
starts it. Fake-launcher tests prove abandoned/release does not cause endless auto-resume.
Retain dirty worktrees/branches and refuse destructive new-task setup. Cover terminal
question exclusion from inbox/counts, direct historical read retaining unanswered status,
late answer refusal with no writes, answer/abandon races, terminal-event non-redelivery,
old-source assignment refusal, no fake merge, manifests/report and export failure/restart.
Cover no/open/already merged PR. Operator attribution is durable and secret-free.

```sh
uv run --locked pytest tests/test_abandon.py tests/test_run_report.py tests/test_timeline.py tests/test_store.py tests/test_rpc.py tests/test_cli.py tests/test_closeout.py tests/test_database.py tests/test_orchestrator_bridge.py tests/test_guides.py
# Before #248, additionally:
uv run --locked pytest tests/test_alice_launcher.py tests/test_prepare_run.py
# With #247's successor, additionally (and alone after retirement):
uv run --locked pytest tests/test_agent_launcher.py
ROBOMATE_TEST_DB_LATENCY_MS=250 uv run --locked pytest tests/test_abandon.py
```

**Size:** medium to large. **Suggested model:** tier 1 implementer/reviewer.
**Dependencies:** Steps 3 and 6. **Counters:** table. Apply §7.2/§7.3/§7.4/§7.5/§8/§11
and served-guide lifecycle clarifications, including §7.2's sentence on when a second run
starts.

### Step 5 — Integrate agent reports and hub-only reporting (#357)

**Scope.** Implement #357 as its own PR after run report and abandon, with the conditional
launcher targeting above. Reuse #356's renderer, add per-agent appendix, counts in the
headline, abandoned per-author exports and hub-only prompts/config parity.

**Approach.** Display author/kind/timing, claims and evidence as attributed assertions,
outstanding URL/reason, and every missing author/reason beside authoritative dispositions.
Extend #423's artifact completion/retry semantics to the combined terminal artifact set.
Remove generated after-release team-file instructions from surviving generators, including
remote workers and Windows renders; preserve archives and Denise's separate reports.
Document the accepted post-closeout gap and routing to lessons/nits/follow-ups. Do not
reintroduce a post-release amendment path.

**Tests.** Extend `tests/test_closeout_export.py`'s real-process scenario: mock Alice and
two mock workers through real bridges; all-three, crashed-before-report and abandoned
runs, safe appendix and missing reasons, late rejection, status/CLI/file agreement after
hub stop/restart. Extend surviving launcher tests for every supported harness/role,
Codex Alice and worker separately, native Windows and remote worker rendering; tool
approval parity for worker submit and Alice request/submit/read. Extend fake Claude
supervision to answer a report request then wait until release. If prepared scripts have
been removed, record that fact and run the successor test equivalents, not missing files.

```sh
uv run --locked pytest tests/test_run_report.py tests/test_abandon.py tests/test_closeout_export.py tests/test_closeout.py tests/test_supervise_claude_code.py tests/test_worker_tools.py tests/test_mcp.py tests/test_runtime_content.py
ROBOMATE_TEST_DB_LATENCY_MS=250 uv run --locked pytest tests/test_closeout_export.py tests/test_run_report.py tests/test_abandon.py
# Before #248, additionally:
uv run --locked pytest tests/test_prepare_run.py
# With #247's successor, additionally (and alone after retirement):
uv run --locked pytest tests/test_agent_launcher.py
```

**Size:** medium. **Suggested model:** tier 1 reviewer, capable tier 2 implementer.
**Dependencies:** Step 4, #356, #386 then #424. **Counters:** table. Apply #357's
§7.3/§8/§13 and report/user documentation updates. #359 live evidence follows this PR;
workers neither launch the live team nor operate its hub.

### Step 6 — Run attribution and payload-free timeline (#20)

**Scope.** Persist attribution for event/decision/message writers and expose
`robomate log --timeline [--follow] [--json] [--run RUN]`, a selected-hub read-only operator
view of interleaved records, task/result metadata and gate readings. The bare `log` uses
the same safe view; this replaces #20's old script spelling. #185/#186 UI stays deferred.

**Approach.** Add nullable `workflow_id` foreign keys and indexes to `event`, `decision`
and `message`. Update every writer to take explicit attribution from the affected
workflow, task, operator question or collection; do not silently attach an old result or
answer to whatever workflow is live now. `log_decision` uses its current workflow in the
same write transaction; terminal transitions explicitly retain the original workflow.
True hub-level records (for example host-pause diagnostics with no run) remain NULL.
Expose workflow/run IDs additively in event records and re-briefing. During migration,
backfill messages from their task, and events only where a valid stored task/question/run
reference establishes one workflow. Old decisions with no authoritative reference stay
NULL; do not parse prose, decision keys or timestamp ranges to invent their owner.

Timeline `--run` selects matching attributed records and reports a count of excluded
legacy unscoped records; the unfiltered view labels those and hub-scoped records
explicitly. The report/evidence records that historical attribution is unavailable.
New decisions and taskless run messages are included in the correct run. Add source-event
attribution to the existing task-assignment consistency checks so Step 4 can reject a
terminal/other-run event as the source of new work without guessing from its payload.
This does not make hub diagnostics a source of workflow authority.

Use a bounded paginated projection with stable cursor and deterministic ordering
`(timestamp, source kind, row ID)`; follow tracks per-source ID watermarks so a backward
clock step does not skip newly inserted rows. Each page has a stable read snapshot.
Closed-set kind/state/role/outcome and validated IDs/SHAs/PR references only; unknown
kinds get a
fixed `unknown` label. Never print decision summaries, keys/rationales, task titles,
question/answer/withdrawal text, worker progress, report prose or event payload text.
Withdrawal metadata can show state, actor/time and settling event ID without implying an
answer. No inference of intermediate task transitions or past delivery attempts that
were never stored. Report current attempts/acked timestamps as snapshots, not a fabricated
history. Call/telemetry detail is deferred to M6; it is not required for useful #20 output.

**Tests.** Add `tests/test_timeline.py`: seeded same-time rows from all sources appear
once in deterministic order, run/hub scoping, multiple pages/follow/reconnect with no
omissions, valid JSON, bounded output, invalid cursors/run IDs, unknown labels, controls,
and payload-marker exclusion in every format. Migration/writer tests cover every event,
decision and message insertion path, taskless messages, old-question answers after a new
run starts, authoritative backfill versus unscoped legacy decisions, and isolation of
two sequential runs with overlapping/equal/backward timestamps. Check new run decisions
appear under `--run`, and hub-only records are never falsely assigned. Follow is a
read-only bounded hold and stops cleanly. Confirm no event ack/lease/expiry mutation,
and withdrawn remains distinct from answered. Pytest-owned CLI tests execute all new
flags; keep hub-report's existing
payload-marker regression green.

```sh
uv run --locked pytest tests/test_timeline.py tests/test_database.py tests/test_store.py tests/test_durable_events.py tests/test_idempotency.py tests/test_closeout.py tests/test_hub_report.py tests/test_rpc.py tests/test_cli.py
ROBOMATE_TEST_DB_LATENCY_MS=250 uv run --locked pytest tests/test_timeline.py
```

**Size:** medium to large (writer audit plus projection). **Suggested model:** tier 1
implementer and reviewer. **Dependencies:** existing run identity only; parallel with
Steps 1–3, required before Step 4. **Counters:** table. Update §8/§11 and #20's reviewer
command to this attribution and CLI contract; reserve the migration only at pickup.

### Step 7 — Close the resume test gaps (#18)

**Scope.** Reduced §7.5 reconciliation at the new boundaries and combined restart proof;
update recovery guidance to the surviving launcher. Reuse existing retry/standby tests,
not a replacement workflow engine. Changes to production behavior need a reproduced
failure and an explanation in the PR.

**Approach.** Extend the existing mock crash-point driver with a fake forge/action journal
and a pytest-owned real HTTP hub/bridges. Kill/restart the hub with a connected worker
holding work; restart/take over a worker with an uncommitted scratch change preserved;
restart Alice with a leased event. Reconcile typed state and the forge, logging a unique
safe next action or asking a concrete operator question. Include closeout assignment,
result commit before ack, merged PR with workflow still live, moved open head, pending
question/reply, report submission before receipt, terminal export interruption, and
queued activation after done. Count assignments, review/merge calls, filing, release and
receipts; duplicates must not be hidden by the test journal.

Main already covers component cases: worker detach and takeover in `test_worker_client`,
orchestrator session fencing/redelivery in `test_orchestrator_bridge`, crash points in
`test_mock_alice`, atomic retries in `test_idempotency`, restart manifest in `test_store`,
and CLI hub identity/port reuse. What is missing is their composition with the new
closeout/finalization seams and real harness evidence. Add those cases only. Run the
existing simulated standby cases for both wall-only jumps and matching-clock gaps,
renewal caps, event delivery and report expiry. Do not claim a process restart preserves
the previous in-memory standby sample; those are different mechanisms.

**Tests.** New `tests/test_resume.py` for the composition; keep the named existing suites.
Assert no duplicate task/review/merge/issue/release; one owner after takeover; original
result/receipt preserved; modified head re-reviewed; merged head checked before WRAP-UP;
ambiguous state escalates; no unintended loss during the controlled restart grace; and
late events cannot act on a later run using Steps 6/4 attribution and dispatch guards.
Terminal unanswered questions never become a later run's answers. Hub deadlines use
`frozen_clock.advance`, condition waits use `wait_until` and `LIVENESS_S`; no sleeps or
short scheduling budgets. Execute the
surviving launcher's `resume` entry through fake-harness tests on Linux/native Windows.

```sh
uv run --locked pytest tests/test_resume.py tests/test_mock_alice.py tests/test_orchestrator_bridge.py tests/test_worker_client.py tests/test_idempotency.py tests/test_store.py tests/test_sweeper.py tests/test_closeout.py tests/test_agent_launcher.py
ROBOMATE_TEST_DB_LATENCY_MS=250 uv run --locked pytest tests/test_resume.py tests/test_sweeper.py
```

**Size:** medium to large, test-heavy. **Suggested model:** tier 1 implementer/reviewer.
**Dependencies:** Step 5 and validated M2 successor/retirement (#247/#268/#269/#248).
**Counters:** table. Apply §7.5, role-guide and recovery-document updates. #341's diagnosis
and unrelated #272/#396 stall policy remain separate unless reproduced on this boundary.

### Step 8 — M4 acceptance run

**Operator prerequisites.** All prior steps and M2/M3 acceptance completed; a pinned
robomate checkout under test; authenticated forge CLI with ordinary issue/comment/merge
permissions; working required CI; certified Claude Code/Codex and any other chosen harness
on the hub host, with their static MCP entries and effective grants. The operator chooses
a real robomate issue small enough for one PR, with a legitimate follow-up observation to
dispose of. Configure `nits_issue`, `followup_label`, explicit closeout steps and default
merge delivery. If the subject uses a counter, supply its reservations target and require
a verified claim before code uses it; otherwise record “no counter used”. Do not consume
a counter solely to stage acceptance. Preserve a recovery/archive location outside clones.

The operator starts the hub/team through the supported post-M2 path and controls all
intentional kills, restarts, standby and eventual cleanup. Workers use pytest-owned hubs
for scripted checks and never operate the acceptance hub or other agents. No special
review/CI bypass, new operator authority or automatic approval of follow-up filing is
part of this plan. #366 must not prevent required authorized issue creation in this run:
its operator fallback would count as an intervention, so use a harness and route that
#366's live check has shown to work.

**Scope and procedure.**

1. Run the scripted acceptance suites from Steps 1–7 first. They prove rejection,
   validation, all-three/crashed report cases, terminal races, control escaping and
   simulated standby deterministically; the real run is not a substitute for them.
2. Operator: `uv run --locked robomate up` in the owning checkout; start Alice/Bob/Charlie
   with the supported launcher and submit the chosen issue with
   `uv run --locked robomate submit --issue N`. Retain versions, join identities,
   effective configuration and statement SHA. Keep this run distinct from any evidence PR.
3. While work is held, the operator kills and restarts the hub, one worker and Alice at
   separate recorded checkpoints using the recovery guide. Confirm same hub/run/task
   identity, retained edits and single ownership; inspect durable state and forge action
   references for no duplicate task, PR, review, merge, filing or release. No worker
   performs these lifecycle actions. Record if takeover rather than saved-conversation
   resume was used and which session was superseded.
4. Let normal review/CI/approved-head merge and typed closeout finish. Collect workers'
   reports before closeout, Alice's after; file follow-up issues with their label and
   source finding IDs/PR URL, record nits or `none` reasons, and close only any used
   counter claim after merge. Verify forge references directly. No routine roadmap edit
   or operator chat is needed. If a consequential issue requires an operator answer,
   use the inbox, record the attempt as failing the no-intervention criterion, fix/route
   it separately and repeat; never treat silence or a withdrawn question as approval.
5. Operator reads `uv run --locked robomate status --json`,
   `uv run --locked robomate report RUN_ID`, and
   `uv run --locked robomate log --timeline --run RUN_ID --json`. Stop that hub using
   `robomate down` from its owner; read the manifest, report and closeout index/files
   offline and compare with online data. Keep any export errors and retry results visible.
   Do not delete state/worktrees before archiving reviewed evidence.

**Evidence.** One PR adds `docs/evidence/m4-<run-id>.md`, following M5 Step 5. Record:

- Commit under test, platform, harness/bridge/forge CLI versions, configured versus reported
  model/effort/source, startup/submission/recovery commands and run/statement identifiers.
- Exact scripted commands/outcomes and CI for the tested head, distinguishing mock
  processes, real harnesses and operator actions. Keep every failed/partial attempt.
- Restart windows, old/new component/session IDs, task ownership/state and retained edits;
  retried deliveries/operations and the journal/forge references showing no duplicate actions.
- PR/review/finding URLs, approved full head, gate readings and CI checks, merge read-back
  with distinct merge SHA; typed disposition coverage, each follow-up issue/label and
  `none` reason, incomplete steps, and any used reservation claim's shipped read-back.
- Report timing/counts/missing reasons, terminal headline, manifest/report/index agreement,
  safe offline read and stop confirmation. Include bytes by role using existing accounting;
  leave performance targets to M6. Omit credentials, raw transcripts and sensitive prose.
- Operator-channel usage: no routine chat question or after-the-fact instruction to finish
  closeout. Report any inbox escalation honestly; never call a scripted operator answer
  evidence of autonomous real closeout.

**Live checks still owed.** These remain ride-along evidence with their existing host and
coverage requirements; a single Linux M4 run does not close them by implication:

| Existing obligation on #2 | Treatment |
|---|---|
| #19 / #205 Modern Standby on WSL2 and native Windows | Follow [host-standby.md](host-standby.md), including OS sleep events, active and idle/completed worker, grace and post-grace evidence. Operator hardware checks; simulated standby is already scripted. Failure gets a linked issue; retain #202/#203 limits |
| #165 OpenCode implementer and AntiGravity worker mid-task kills | Credit Step 8 only for the harness/role actually used; complete other row on another ordinary run |
| #173 / #182 real crash/resume, remote hosts and orphan bridges | Step 8 supplies local component checks; remote connectivity and operator-controlled orphan cleanup remain #182 rows until performed |
| #221 interactive Claude Code/Codex and native Windows ask checks | Carry to #247 successor `ask` and ordinary post-run consultation; not required to claim autonomous closeout, and never use ask output as authority |
| #146 Claude Code, Codex and AntiGravity Alice recovery | Credit the actual orchestrator used; adapters do not prove other harnesses live |
| #358 and #359 report matrices | Credit matching normal/crash/offline/harness/host rows with run links. #359 begins after Step 5; finish uncovered rows over ordinary runs, not invented acceptance results |

**Tests.** The real run and evidence review, plus
`uv run --locked pytest tests/test_doc_links.py` for the evidence PR. No new behavior
change is expected here. Defects get separate issues/PRs and the affected run is repeated;
even small fixes must receive their required tests/review/CI. **Size:** small evidence PR,
operator time depends on recovery windows. **Suggested model:** strong orchestrator and
reviewer, certified implementer. **Dependencies:** Steps 6/7, M2 #249 and completed M3.
**Counters:** none for evidence; subject-run claims handled conditionally as above.

### Validation for implementation steps

Each step runs the commands in its Tests section, every changed test file once with
`ROBOMATE_TEST_DB_LATENCY_MS=250` without `-q`, all touched scripts/entry points and the
repository's required checks. Implementers run the full `uv run --locked pytest -n auto`
once before submitting; reviewers use focused/acceptance checks plus CI's required Linux,
Windows, probe and probe-100 jobs on the reviewed head. New CLI entry points get
pytest-owned hub tests; any manual worker smoke uses the isolated procedure in
[the worker guide](../../guides/worker.md), never the run hub. Conditional launcher
commands above are selected by files actually surviving M2; their PR names the command
used, rather than skipping the handoff. No test command here authorizes a worker to start
or stop a live team, inspect protected state or change workflow status.

## Risks

| Risk | Mitigation |
|---|---|
| Existing prose CLOSE-OUT is mistaken for typed coverage | Inventory marks it partial; role-specific dispatch and tests in Step 1; no legacy prose gate pass |
| Gate adds async forge latency/race before finalization | Recheck the original run/binding under lock; fail closed, preserve state and idempotency |
| New done rail breaks mocks/lifecycle fixtures | Update them with explicit fake-gate/typed-result setup; no production test bypass |
| Queue starts while canceled work is unwinding | Follow §7.2 retention; Alice starts only after atomic abandonment; reject late old-run calls and preserve dirty-worktree checks |
| #423 retention or #386/#424 semantics move during report work | Explicit dependencies, shared retry seam and serialized integration; no competing policy |
| M2 deletes a generator that #357 expected to edit | Conditional surviving-launcher contract; preserve old archives, do not gate #248 on M4 |
| Typed report is read as verified prose | Source labels, missing-data markers, inert rendering and separate retrospective assertions |
| Live filing denied or harness stalls | Record blocker with evidence through existing channels; no authority workaround; repeat acceptance after resolution |
| Live evidence overclaims cross-host/harness coverage | Per-case evidence table and existing ride-along issues; mocked/scripted/live results kept distinct |

## Roadmap bookkeeping

After this plan merges, triage links it from M4 on #2 and opens/reuses one pointer issue
per step with the milestone/label above. Link #15 to Step 1, #14/#17 to Step 2, #18 to
Step 7 and #20 to Step 6; close or supersede the historical scope only when its owning
implementation is verified, not because a plan exists. #357 stays its own Step 5 with the
launcher clarification. Keep #386 → #424 and #423 on their current lanes, place #431
before Step 3, and keep #358/#359 and
other live checks as evidence rows, and owner-held issues held.

The roadmap should show the edge table, Step 6 before Step 4, M2 #268/#269/Step-11 handoff,
M3-before-M4 acceptance and all remaining live obligations. This is proposed bookkeeping
for triage, not an agent edit to #2. Step issues point here and the implementing PRs close
their issues. Counter-using runs alone maintain their claims in #409; this documentation
run has no reservation or reservation closeout. M4 completion is recorded only after
Step 8's evidence is reviewed and merged, with uncovered ride-along work still explicit.

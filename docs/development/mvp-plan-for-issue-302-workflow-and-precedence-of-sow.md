# Plan for #302: workflow and precedence of the statement of work

This is stage 1, [#360](https://github.com/RoboNater/robomate/issues/360):
consolidate the requirements and edit the design of record. Stage 2 is
[#302](https://github.com/RoboNater/robomate/issues/302): implement and test the
guide, prompt and prepare-run wording below after this plan merges. Stage 1
changes only this document and [the MVP spec](../mvp-spec.md).
After merge, this plan takes precedence over the #302 thread for stage 2;
a conflict between this plan and the spec goes to the operator. Neither stage
grants extra authority or changes the safeguards.

## Sources

The IDs below enumerate requirements, including scope limits, rather than the
historical examples that motivated them. Each ID is used again below. The four
substantive comments are the two at 11:38 on October 20, October 22 and October 23.
The additional October 20 triage comment and October 24 staging comment are
listed separately so they are not mistaken for extra substantive comments.

| ID | Requirement | Source |
|---|---|---|
| B1 | Follow the statement from `get_statement` where it modifies or conflicts with a guide procedure: validation commands/hosts, smoke runs, evidence, PR contents, review scope/cadence. | [#302 body, Proposed 1](https://github.com/RoboNater/robomate/issues/302) |
| B2 | Keep safeguards: no statement authorization to skip review/CI, merge an unapproved head, do worker operator-only actions, or gain extra authority over agents, credentials or the run hub. Confirmed operator answers remain required; safeguard-crossing conflicts are blocked. | [#302 body, Proposed 2](https://github.com/RoboNater/robomate/issues/302) |
| B3 | Record every replaced guide instruction and its replacement in the typed result `summary`. | [#302 body, Proposed 3](https://github.com/RoboNater/robomate/issues/302) |
| B4 | Reviewers also record departures in the review comment. | [#302 body, Proposed 3](https://github.com/RoboNater/robomate/issues/302) |
| B5 | Record departures in closeout reports; update prepare-run's rendered instruction. Carry forward to #268 and #215. | [#302 body, Proposed 3](https://github.com/RoboNater/robomate/issues/302) |
| B6 | Alice follows precedence for her own steps and preserves statement departures in worker assignment summaries. | [#302 body, Proposed 4](https://github.com/RoboNater/robomate/issues/302) |
| B7 | Exclude actual-work-file storage (#296) and mandatory reading/acceptance-checklist implementation (#297); this work pairs with the latter. | [#302 body, Not covered here](https://github.com/RoboNater/robomate/issues/302) |
| B8 | Pin guide/prompt wording; check served worker/reviewer guides, named safeguards and rendered closeout instruction; run the four-file pytest command. | [#302 body, Tests](https://github.com/RoboNater/robomate/issues/302) |
| S1 | Replace §7.1 optional reading with `get_statement(sha)` on every assignment carrying the statement; assignment text points to requirements but does not replace them. | [October 20, spec comment](https://github.com/RoboNater/robomate/issues/302#issuecomment-6289986631) |
| N1 | Skip NONBLOCKING for an approval whose every finding is optional cosmetic wording/formatting/line wrapping, marked `optional, no follow-up`; proceed on approved head to merge gate. | [October 20, exception comment](https://github.com/RoboNater/robomate/issues/302#issuecomment-6289986955) |
| N2 | A substantive nonblocking finding still triggers NONBLOCKING covering all findings, including cosmetic ones; do not make nonblocking generally skippable. | [October 20, exception comment](https://github.com/RoboNater/robomate/issues/302#issuecomment-6289986955) |
| N3 | Reviewer `optional, no follow-up` is cosmetic only; never acceptance, correctness, consistency or documentation-accuracy gaps. | [October 20, exception comment](https://github.com/RoboNater/robomate/issues/302#issuecomment-6289986955) |
| N4 | Pin the exception in runtime-content tests. | [October 20, exception comment](https://github.com/RoboNater/robomate/issues/302#issuecomment-6289986955) |
| C1 | Widen N1 to approvals where each finding is either optional cosmetic or post-merge close-out only: roadmap/shared-body edits after merge, no PR file change. | [October 22, widening](https://github.com/RoboNater/robomate/issues/302#issuecomment-6349809317) |
| C2 | Proceed to merge gate on approved head without NONBLOCKING; Alice carries close-out findings by ID into CLOSE-OUT. | [October 22, widening](https://github.com/RoboNater/robomate/issues/302#issuecomment-6349809317) |
| C3 | Every finding must qualify; a finding requiring a PR file change or an answer before merge triggers NONBLOCKING covering all findings. | [October 22, widening](https://github.com/RoboNater/robomate/issues/302#issuecomment-6349809317) |
| C4 | Reviewer explicitly marks close-out-only findings, e.g. `post-merge close-out only; no PR change`; Alice does not infer this classification. | [October 22, widening](https://github.com/RoboNater/robomate/issues/302#issuecomment-6349809317) |
| P1 | Apply precedence to the specific conflicting instruction; compatible requirements remain in effect. Timing for one action does not postpone all actions on the document. | [October 23, clarification](https://github.com/RoboNater/robomate/issues/302#issuecomment-6365814378) |
| P2 | Preserve the schema-reservation example: reserve at pickup before using the number, implement/review with it, mark shipped/complete after merge per reviewed roadmap decision. Do not invent a blanket pre-merge-edit prohibition. | [October 23, example](https://github.com/RoboNater/robomate/issues/302#issuecomment-6365814378) |
| P3 | An explicit ban on all pre-merge roadmap edits is a real conflict: clarify before using a schema number and record the decision. Reading the next number is not reservation. | [October 23, example](https://github.com/RoboNater/robomate/issues/302#issuecomment-6365814378) |
| P4 | This is a precedence/source-recording clarification, not routine-choice approval, extra worker authority or changed safeguards; regression coverage pins compatible requirements and specific timing. | [October 23, scope](https://github.com/RoboNater/robomate/issues/302#issuecomment-6365814378) |
| T1 | Separate issue/PR on the post-#297 base; original Track 1/Lane D scheduling was triage context. | [October 20, triage](https://github.com/RoboNater/robomate/issues/302#issuecomment-6280135816) |
| T2 | #360 first, then #302 per merged plan; plan takes precedence over thread; plan/spec conflict goes to operator. | [October 24, staging](https://github.com/RoboNater/robomate/issues/302#issuecomment-6386313153) |

The scope, required sections, docs-only boundary and consolidation rule come
from [#360](https://github.com/RoboNater/robomate/issues/360). Roadmap
[#2](https://github.com/RoboNater/robomate/issues/2) at pickup lists #360 then
#302 on Lane B, next DB schema v28 and wire `hub.schema_version` 1. This
supersedes the old scheduling context in T1; this work reserves neither counter.

## Combined spec changes

These are the complete edits made to `docs/mvp-spec.md` in stage 1:

1. **§7.1, assignment bullet (S1):** replace `when they need it (#8, #9)`
   with `on every assignment that carries it (#8, #9, #297)` and add
   `The assignment text points to the requirements but does not replace them.`
   Preserve verbatim delivery. This repairs the optional-reading wording that
   conflicts with the already merged #297 guides.
2. **§7.1, statement precedence bullet (B1–B6, P1, P4):** state that a
   statement's specific procedural change takes precedence, compatible
   requirements remain, timing does not postpone all actions on a document,
   safeguards remain, no extra authority or routine-choice approval requirement
   is added, every departure is recorded in result/reviewer comment/
   closeout, and Alice preserves departures in summaries and follows them herself.
   Reference §7.4/§12 for confirmed operator authorization; retain the worker
   prohibition on operator-only actions and blocked outcome for safeguard conflicts.
3. **§7.2, approved nonblocking findings bullet (N1–N3, C1–C4):** record the
   combined exception, the approved-head merge-gate route, close-out IDs and
   reviewer classifications. §7.2 is the existing home for workflow sequencing;
   it is a natural place for this rule, so no new section or hub state is needed.

The spec states the rule; the guide changes below carry the concrete reservation
example (P2–P3). No DB, wire, result schema or enforcement change is proposed.
B7 remains excluded: S1 fixes documentation of #297, rather than reimplementing it.

## Combined guide changes

### Shared precedence text and placement

Use the following wording once, identically in the four copies below. Insert
it immediately after the paragraph beginning `Task requirements describe the
intended outcome` and before `A reviewer judges the change` (B1–B6, P1–P4):

> When the statement of work served by `get_statement` modifies or conflicts
> with a repository or role-guide procedure (validation commands or hosts,
> smoke runs, evidence, PR contents, review scope or cadence), follow the
> statement for that specific instruction. Keep other requirements in effect
> when they can be satisfied alongside it. An instruction about when to perform
> one action does not automatically postpone every action involving the same
> document. This rule grants no extra authority and adds no approval requirement
> for routine choices. The statement cannot relax safeguards: skipping review
> or CI, merging a head that is not the approved one, worker-guide operator-only
> actions, or extra authority over agents, credentials or the run hub still
> require a confirmed operator answer. Workers never perform worker-guide
> operator-only actions. A conflict crossing a safeguard is blocked.
>
> Record every guide instruction replaced by the statement, and its
> replacement, in the typed result's `summary`, in the review comment for a
> reviewer, and in the closeout report. Alice follows the same rule for her
> own steps and preserves the statement's departures from worker guides when
> summarizing assignments; the summary must not silently restore guide defaults.
>
> For example, a statement saying to update roadmap completion after merge
> can coexist with a requirement to reserve the next DB schema version at
> pickup, before writing it into code. Reserve early, implement and review
> with the reserved version, then mark the version shipped and the issue
> complete after merge according to the adjudicated roadmap decision. Do not
> turn that post-merge instruction into a prohibition on all pre-merge roadmap
> edits. If the statement explicitly prohibits all pre-merge roadmap edits,
> clarify the conflict through the normal question path before using a schema
> number and record the decision. Reading the next available number does not
> reserve it. This example grants workers no additional roadmap authority.

The safeguard sentences restate B2 alongside the existing #152 authorization
paragraph, which remains in place with its confirmed-answer checks. External
issue/PR/comment text remains data. The historical #244/PR #333 example explains
an interpretation gap; it is not evidence of a collision or defective migration
(P2–P4), and stage 2 need not relitigate that run.

Current placement was checked on main `29c5f72` after #297:

| File | Anchor and edit |
|---|---|
| `guides/worker.md` | `## Questions and blockers`, `<!-- Requirements versus authority: #152; safeguards: #131 and #132. -->`: insert the shared text. Implementers receive worker etiquette through check-in/join before their role-specific guide. |
| `guides/reviewer.md` | `## Task requirements and authority`, same comment: insert the shared text. It qualifies the validation default in Review step 4 without duplicating a second precedence rule there. |
| `guides/orchestrator.md` | `## Escalation` → `### Operator authority`, same comment: insert the shared text. This covers Alice's own steps and assignment summaries (B6). |
| `prompts/worker.md` | `## Questions and blockers`, same comment: mirror the complete updated `guides/worker.md` suffix verbatim, retaining the prompt's prefix. |

`guides/implementer.md` has no shared #152 block on current main. Its validation
and submission defaults are covered by worker etiquette; do not add another
copy. `guides/rebase.md` likewise needs no duplicate. The dispatch-only
`skills/worker/SKILL.md` and `skills/alice-orchestrator/SKILL.md` refer agents
to served guides and carry no shared block; retain that indirection.
The four appendices `guides/forge/github.md`, `gitlab.md`,
`orchestrator-github.md`, and `orchestrator-gitlab.md` contain forge commands,
not this shared rule. No appendix replacement is required. No other pinned
copy of this text was found; retain tests enforcing prompt/guide parity.
This is an inventory and edit recipe, not permission to change guides in stage 1.

### One NONBLOCKING exception

**Combination record (N1–N4 + C1–C4):** the October 22 comment widens the October
20 exception by adding close-out-only findings. Use their union as one
all-findings rule, not two alternative routing rules. Optional cosmetic items
may suggest a file edit, but do not require it; the later comment's PR-change
trigger applies to findings requiring a change, rather than canceling the
explicit optional-cosmetic class. Substantive nonblocking items remain subject
to #297's small-fix default. An unmet acceptance item remains blocking under
#297 and never becomes eligible merely by relabeling it.

In `guides/orchestrator.md`, `## REVIEW, ADDRESS, and RE-REVIEW`, replace the
paragraph beginning `An approval with only nonblocking findings` through
`that head.` with:

> An approval whose every finding is either an optional cosmetic item
> (wording, formatting or line wrapping, explicitly marked `optional, no follow-up`)
> or an instruction for post-merge close-out only (roadmap or other shared-body
> edits after merge, no PR file change) gets no NONBLOCKING task.
> Proceed to the merge gate on the approved head. Alice carries close-out-only
> findings into the CLOSE-OUT assignment by ID.
>
> If any finding falls outside those classes, including a substantive PR file
> change or an answer needed before merge, assign a NONBLOCKING task covering
> all findings, including cosmetic ones. Fix a finding that is a small change
> to files the PR already touches by default; a decline gives a reason for
> each finding and Alice may overrule it with a new NONBLOCKING task naming
> the finding (#297). A finding
> the reviewer marks `optional, no follow-up` stays in the review comment and
> is never filed. Handle findings now and trigger RE-REVIEW if the head
> changes, or decline them. After a decline, give the reviewer a same-head
> task to file and reference a follow-up issue for each declined finding that
> is neither marked `optional, no follow-up` nor overruled. If the implementer
> reports the same head with no changes, the existing approval remains bound
> to that head.

Also in that file:

- In the action-key table row `Approved nonblocking choice`, qualify the
  condition as `Approved nonblocking choice requiring a task`; no new action
  key or synthetic task is needed for the exception (C1–C3).
- In `## WRAP-UP`, add a row to `Route to the merged head`:
  `Approving REVIEW -> all findings optional cosmetic or close-out-only -> MERGE`
  uses `Approving REVIEW completion; carry close-out findings by ID into CLOSE-OUT`.
  This is the existing direct-review source rule applied to the exception,
  preserving the unused terminal-completion rule and all merge checks (C2).

In `guides/reviewer.md`, Review step 5, replace the sentences beginning
`You may mark a finding` through `never filed (#297).` with (N3, C4):

> Use `optional, no follow-up` in a finding's typed `text` only for cosmetic
> wording, formatting or line wrapping, never for an acceptance, correctness,
> consistency or documentation-accuracy gap. It stays in the review comment
> and is never filed (#297). Explicitly mark a finding that only directs
> post-merge close-out work `post-merge close-out only; no PR change`.

**Combination record (B1, B6 + P1–P4):** the October 23 comment clarifies the
specific-instruction scope of the body's precedence rule. The shared text
incorporates it and its example rather than adding a competing hierarchy.
**Combination record (B3–B5):** one departure description supplies the typed
result, review comment where applicable, and closeout; none replaces another.

## Lower-level changes

For B5, change `scripts/prepare-run.py`, function `prompt_sections`, both report
paragraphs: Alice's `After you release bob and charlie` and workers'
`After you are released by Alice`. Add the same sentence to each:

> Record every guide instruction replaced by the statement of work, and its
> replacement, in your closeout report.

Keep the absolute `RUN_DIR/closeout/<name>.md` path, post-release timing,
uncommitted worker report, credential/transcript exclusions and Alice's
statement-hash comparison. Exercise the actual rendered prompts in tests.
There is no new typed result field: B3 uses existing `summary`; B4 uses the
existing review comment.

Carry-forward notes only (B5, B7): #268's replacement launcher must retain this
instruction; #215's persisted closeout path must carry the same information.
At this plan's base, #215 has already landed: worker collection happens before
CLOSE-OUT, and its later `CloseoutResult` is authoritative. This plan does not
reopen #215 or change the persisted report schema, hub request instructions,
collection timing, or successor launcher. Do not turn that observation into
additional #302 work. #296 statement storage and #297 reading/checklist
implementation are already present and stay outside scope.

## Tests

Stage 2 adds content regression tests, not a new workflow engine or authority
mechanism (B7–B8). Normalize whitespace for prose assertions, retain literal
markers, and check the substantive limits alongside the positive rule.

| Test file | Add or update; what it pins | IDs |
|---|---|---|
| `tests/test_guides.py` | Add checked-in composed worker/reviewer guide tests for both GitHub and GitLab, using `guide_response` and the real guide directory. Pin statement precedence, all safeguards, confirmed-answer/operator-only limits, departure `summary` and reviewer-comment wording. Retain composition and no-worker-merge-command tests. | B1–B4, B8 |
| `tests/test_runtime_content.py` | Add shared-paragraph coverage for worker, reviewer, orchestrator and mirrored worker prompt, including each safeguard: review, CI, approved head, worker-only exclusions, agents, credentials, run hub, and blocked conflict. Pin all three departure destinations, Alice's own steps and preservation in summaries. Preserve existing `prompt.endswith(worker)` and #297 checklist/cheap-fix assertions. | B1–B6, B8 |
| `tests/test_runtime_content.py` | Pin compatible requirements remaining active, action-specific timing, early schema reservation before code, implementation/review with that number, post-merge completion, no inferred blanket prohibition, explicit prohibition requiring clarification and decision recording, and reading-next-number not reserving it. Pin no routine-choice approval or extra worker authority. | P1–P4 |
| `tests/test_runtime_content.py` | Extend `test_statement_first_acceptance_checklist_and_cheap_nonblocking_fixes` or a focused adjacent test to pin the combined `every finding` exception, both explicit classes/markers, mixed eligible findings, and any substantive PR-change or pre-merge-answer finding causing the all-findings task. Pin cosmetic-only limits and forbidden gap classes, approved-head merge gate, close-out IDs, direct-review close-out source, and retained small-fix/decline/re-review defaults. Assertions must reject text that says all nonblocking items are skippable. | N1–N4, C1–C4 |
| `tests/test_prepare_run.py` | Extend `test_closeout_reports_leave_custom_clones_clean_on_rerun` to assert each rendered Alice/bob/charlie prompt asks for every replaced guide instruction and its replacement. Its harness parametrization covers Claude Code, Codex, OpenCode, AntiGravity and Devin; preserve rerun/absolute-path/clean-clone assertions. Direct `prompt_sections` checks may supplement actual rendering, not replace it. | B5, B8 |
| `tests/test_doc_links.py` | Run existing link coverage against the plan/spec and changed guide links; no new link checker required. | B8, S1 |

These are content contracts: pin wording and scope, including negative limits;
do not claim static tests prove an agent followed the procedure. The reviewer
also reads the served worker/reviewer guides, Alice's exception routing and
rendered closeout prompts, and checks traceability from every Sources ID.

Exact stage-2 implementer and reviewer acceptance commands (B8, N4, P4):

```sh
uv run --locked pytest tests/test_guides.py tests/test_runtime_content.py tests/test_doc_links.py tests/test_prepare_run.py -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
```

The implementer also runs the full suite once before submission:

```sh
uv run --locked pytest -n auto
```

Run the rendered-prompt test above to execute the changed `prompt_sections`
entry path; use its temporary, isolated preparation fixtures rather than
starting the run's hub. Reviewers run the named commands and affected tests;
CI supplies full Linux/Windows coverage on the reviewed head unless absent or
the test harness itself is at risk. A statement requiring other hosts, commands,
evidence or cadence takes precedence within the unchanged safeguards (B1–B2).

Stage-1 acceptance commands from #360 are:

```sh
uv run --locked pytest tests/test_doc_links.py -q
uv run --locked ruff check .
uv run --locked ruff format --check .
```

## PR sizing

Stage 2 fits one PR (T1–T2): the four mirrored guide/prompt copies, the
orchestrator/reviewer routing wording, two prepare-run report paragraphs and
focused content tests form one small, coherent documentation contract. Keep
spec edits in stage 1; stage 2 implements this merged plan. No schema or wire
reservation is needed. Do not bundle #268, #215, #296 or #297 follow-on work
(B5, B7), and do not edit #302's body.

## Open questions for the operator

No unresolved requirements remain in this consolidation. Known discrepancies
and their prescribed resolutions were raised through `ask_alice` on task
`bda0d5b93e9344338865c91e4e517aaf` (run `20261024_d2f85f9d`):

- §7.1 says reading is optional, contrary to #297; #360 explicitly prescribes
  S1's replacement. Apply it here.
- Current orchestrator text unconditionally routes approvals with nonblocking
  findings through NONBLOCKING; #360 explicitly prescribes combining N1–N4 and
  C1–C4. Record the replacement for stage 2.
- Shared #152 text remains the safeguard/issue-data policy; the statement-only
  procedural precedence text supplements it (B1–B2, P1–P4).
- #215 has landed since B5 was written; #360 limits #215 and #268 to carry-forward
  notes. Keep stage-2 lower-level edits in prepare-run wording.

Alice's replies 5 and 7 confirmed those resolutions as already prescribed by
#360/#302, and directed no fresh operator question. These are task
clarifications, not confirmed operator answers and not a grant of authority.
No other comment-vs-comment or plan-vs-spec conflict was found. If one emerges
in stage 2, ask through Alice for the operator's decision and record the answer;
do not silently resolve it or extend this plan.

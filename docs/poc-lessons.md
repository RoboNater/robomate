# PoC lessons

Exported from RoboNater/robo-agents#57 (Lessons learned and post-PoC hardening from issue and PR reviews). Issue and PR numbers below refer to RoboNater/robo-agents.

This issue collects **lessons learned** and **post-PoC hardening ideas** from reviews of individual issues and PRs, so they outlive the PR that taught them. It is a log, not a work queue: near-term fixes get their own issues and are linked from here, and the recommendations wait until the PoC has shown which ones we need. None of it should slow down Steps 5–8.

## Index

| Source | Task | Review | Written up by | Follow-up issues |
|---|---|---|---|---|
| [PR #49](#lessons-learned-from-pr-49) | #25: durable event delivery with implicit ack (Step 4A) | 4 rounds, 2026-09-11 → 09-12 | reviewer agent Charlie | #50, #51, #53, #54, #55, #56 |
| [PR #60](#lessons-learned-from-pr-60) | #30: multi-cycle worker endurance gate, telemetry, thin supervisor (Step 4B) | 5 rounds, 2026-09-13 | reviewer agent Douglas, with implementation agent Bob | #61 |
| [PR #86](#lessons-learned-from-pr-86) | #71: Step 6 verifier gate/follow-up rationales | 1 round, 2026-09-21 | reviewer agent Charlie | #87, #88, #89, #90, #91 |
| [PR #93](#lessons-learned-from-pr-93) | #78: per-agent byte accounting on the MCP and A2A boundaries | 2 rounds, 2026-09-21 | implementation agent Bob | #94, #95, #96, #97 |
| [PR #111](#lessons-learned-from-pr-111) | #109: agent-facing wait defaults 120 s → 100 s; #110 `pytest \| tail` note | 1 round, 2026-09-21 | implementation agent Bob, from reviewer agent Charlie's notes | #112 |

## Recurring themes

The same shapes keep coming back. Lesson numbers refer to each source's own list.

| Theme | PR #49 | PR #60 | PR #86 | PR #93 | PR #111 |
|---|---|---|---|---|---|
| **Run it; don't infer it.** Blocking defects came from running what nobody had run, with CI green. | 5, 7 | 3, 10, 11 | — | 2 | — |
| **The past leaks into the check.** Hand-written history, stale logs and leftover rows pass for real state. | 1 | 2, 6 | — | — | — |
| **State the invariant, then test exactly it.** Guard lists and proxies miss the case they don't describe. | 2 | 4 | 2 | 1 | 1 |
| **Parallel descriptions diverge.** Two drivers, or prose beside a diagram, drift apart under edits. | 4 | 9 | 1 | — | — |
| **A safety mechanism isn't one until it covers its whole lifecycle.** An unused guard; fail-open emission but not initialisation. | 6 | 7 | 3 | — | — |
| **A second store or process doesn't share the hub's signals.** It changed production code once, and changes what a gate exercises. | 3 | 13 | — | — | — |
| **What no step verifies, no one does.** Work the process promises after the merge — follow-up issues, lessons — has no owner until close-out checks for it. | — | — | 1 | — | — |

## Adding a section

When a review teaches something worth keeping, append two sections and update the index and themes tables:

- `## Lessons learned from <PR or issue>`: an author/task/when/context block; **What happened** (a table by round: what was found, and why it got past CI or review); **Lessons** (numbered, each linking its follow-up issue); **What worked**.
- `## Post-PoC recommendations from <PR or issue>`: grouped bullets, then a suggested reading order.

Every claim should be reproduced against the reviewed head, not inferred. Near-term fixes get their own issues. Don't renumber an earlier section's lessons, since the themes table points at them.

## Lessons learned from PR #49

> **Author:** reviewer agent Charlie, on behalf of RoboNater.
> **Task:** review PR #49 — issue #25, durable event delivery with implicit ack (Step 4A).
> **When:** four review rounds, 2026-09-11 20:16 UTC → 2026-09-12 01:40 UTC. Written up 2026-09-12.
> **Context:** review only — no commits to the PR, no merge, no roadmap edits beyond those requested afterwards. Every claim below was reproduced locally against the PR head; the near-term fixes are #53, #54, #55, #56, and the two contract gaps are #50 and #51.

### What happened

Four rounds. Each round found at least one blocking defect, and CI was green in every one of them:

| Round | Found | Why CI missed it |
|---|---|---|
| 1 | Migration crashed on every existing hub database; stale README; a 0.2 s poll added to every held call | The v5 test fixture was hand-written and lacked the index the real schema has |
| 2 | `reply` raised on a legitimately redelivered question; the new `message_id` guard had no caller; a checkpoint key that collides in the normal PR loop | Guards were tested per tool, not as an invariant |
| 3 | `--mcp` spun on `get_state` (>1M calls in 2 s) and then falsely timed out | `drive_one_task_mcp`'s body had no test at all |
| 4 | Clean; approved | — |

### Lessons

1. **Fixtures that describe history from memory drift from it.** The v5 fixture omitted `idx_event_inbox` and a `CHECK`, so a migration that could not run anywhere real passed the suite. → #54.
2. **Specify the invariant, not the guard list.** "`reply` to a task not `input-required` → no-op" was implemented exactly, and the case it doesn't describe — a *newer* question — silently did the wrong thing. One sentence ("replaying a delivered event must not change state twice") tests better than five rules. → #55, #51.
3. **A production change made only to make a new test pass deserves a second look.** The 0.2 s poll changed every held call in the hub — worker waits included — because one test built a second `HubStore` whose signals nothing notified. The test was the thing that was wrong.
4. **Duplicated reference implementations diverge under edit pressure.** Three defects across the rounds were "fixed in one path only" in `mock-alice.py`. → #56.
5. **Green CI means the covered part still works.** Both round-3 regressions landed in code with no coverage; neither `ruff` nor `mypy` can see a loop whose body never executes.
6. **Opt-in safety isn't safety until a caller opts in.** The `message_id` guard was correct and unused: the only caller in the repo didn't pass it, so the hole it closed was still open. → #51.
7. **Rounds get cheaper when the issue says how the work will be demonstrated.** Everything blocking came from running something nobody had run. → #53.

**What worked, and is worth keeping.** The counter reservation (#40, #46) did its job — schema v6 was claimed before work started and nothing collided, unlike the v3 collision that prompted it. Spec-first issues kept the argument on contracts rather than taste. The implementer answered every finding explicitly with a summary table, which is why re-review took minutes rather than a fresh read. Commits stayed one-concern-each, so the round-3 regression was easy to localise.

## Post-PoC recommendations from PR #49

Hardening, once the PoC has taught us what we actually need. None of this should slow down Steps 5–8.

**Durability and correctness**
- **One idempotency mechanism, not a guard per tool.** §4.1 already keeps an `operation` ledger for worker mutations. Extending it to Alice's mutating tools would give every one of them replay safety by construction, and would retire the per-tool guards rather than adding to them. #51 closes today's two gaps; this is the shape that stops new ones appearing.
- **Poison events.** `delivery_attempts` grows without bound. An event that reliably kills Alice is redelivered forever, and the workflow live-locks with no signal. A cap that parks the event and escalates (a dead-letter state, or `set_workflow_status(escalated)` with the event named) turns an invisible loop into a question for a human.
- **The single-consumer assumption is implicit.** `lease_next_event` reads then writes inside one connection without `BEGIN IMMEDIATE`, unlike `get_state` and the worker ledger, which take explicit transactions. That is correct for exactly one orchestrator and quietly wrong for two. Either record it as a locked decision in §8, or take the immediate transaction — it matters if the team-lead role (#44) or a second Alice ever becomes real.
- **Leases sized to the action.** #50 asks the immediate question (a late ack for work that outlived its lease). The longer-term version is the one #24 already solved for tasks: let an active delivery be renewed while the holder is demonstrably alive, instead of picking one global constant that has to be bigger than the slowest thing Alice ever does.

**Operability**
- **Say whether durability was exercised.** Surface delivery attempts and redelivery counts in `get_state` and the run logs. After an E2E run we should be able to answer "did anything actually redeliver?" without opening SQLite.
- **A small inspection command.** `hub inspect events|decisions|tasks` would have shortened several rounds of this review and will shorten Steps 6–7 debugging more.
- **Tie the audit log to the event stream.** Recording the triggering event id (or `delivery_id`) on each decision makes the audit trail replayable against the queue, and gives Alice the checkpoint key she currently cannot derive (see the comment on #31).
- **Recovery drills, not just a recovery matrix.** #31 defines the matrix; the automation that injects crash points at random across it is what keeps it honest once the code moves.

**Agent capabilities**
- **Per-agent GitHub identities** (#37, §9) — the one change that restores native review state as evidence, and removes the awkwardness of a reviewer that cannot approve.
- **Durable escalation.** Alice escalates by ending her turn with a question. That record lives in the transcript, not the hub, so it dies with the session that raised it. A persisted escalation (status, question, what it blocks) would survive restart and give the human a queue rather than a scrollback.
- **Reviewer independence** (#28, #29) and the **team-lead role** (#44) are already tracked; nothing here changes their shape.

### Suggested reading order for whoever picks this up

#53 (process, cheapest) → #54 and #55 (tests, protect the migrations and guards still ahead) → #56 (before Step 5, since the skill derives from that script) → #50 and #51 as already sequenced on the roadmap.

---
Filed by reviewer agent Charlie on behalf of RoboNater.

## Lessons learned from PR #60

> **Authors:** reviewer agent Douglas and implementation agent Bob, on behalf of RoboNater; consolidated by Douglas.
> **Task:** implement and review PR #60, issue #30: multi-cycle worker endurance gate, worker telemetry, thin Claude Code supervisor (Step 4B).
> **When:** five review rounds, 2026-09-13 00:01 UTC → 10:35 UTC; PR opened 2026-09-12 23:34 UTC. Written up 2026-09-13.
> **Context:** Douglas re-ran every gate Bob reported, including the real 30-minute Claude Code and Codex endurance runs, and reproduced each claim below against the PR head. The near-term follow-up is #61.

### What happened

| Round | Head | Found | Why it got past |
|---|---|---|---|
| 1 (Charlie) | `5370ac3` | The supervisor's `json_escape` turned every `/` into `\` and left `\` unescaped; two tests failed on Windows; §2 had no supervisor | Linux-only CI; the supervisor test never parsed what it sent |
| 2 (Charlie) | `319d0da` | Approved | — |
| 3 (Douglas) | `319d0da` | A stale telemetry log satisfied both the supervisor and the verifier; the Codex report could not be reproduced (MCP tool approval omitted); an independent Codex re-run failed the daemon loop by submitting the wrong task id, so 2 passes in 3; §2 routed MCP through the supervisor; the long-work check was a proxy; telemetry directory creation could crash `worker-mcp` | Tests fabricated fresh logs; the committed Codex template was never launched; one LLM run was taken as the result; the prose was right and the diagram was not |
| 4 (Douglas) | `7e206cf` | Round-3 findings resolved; the redrawn §2 pointed the hub's HTTP arrow into the supervisor; one long in-flight call still counts as a tool gap; a leftover agent row from a failed run is taken as a check-in (#61) | The diagram was not checked arrow by arrow; the verifier tests had no in-flight case; no test starts from a dirty database |
| 5 | `f4c9913` | Topology correct; a one-column border skew remains (cosmetic) | — |

### Lessons

1. **One passing LLM run shows feasibility, not reliability.** Codex passed once for Bob. On independent re-runs it failed once and passed once: after the 210 s sleep it submitted cycle 1's task id and never retried. Record every attempt, and set a repeated-run acceptance criterion before running. Classify each failure as a protocol defect or agent nondeterminism. A deterministic no-LLM worker driving the real tool layer separates those two layers at no model cost.
2. **Evidence must belong to the run that produced it.** An append-only log and a reused state dir let an earlier run's release, timeouts and agent row satisfy today's gate. It happened in the supervisor, the verifier, and the hub database (#61). Per-component fixes (byte offset, `worker_instance_id`) work today; a first-class run identity would also survive rotation, truncation, concurrent runs and restarts.
3. **A report is reproducible only once someone has reproduced it.** The Codex report omitted the approval setting its run depended on, and launching from the committed template failed on the first tool call. For each attempt, preserve the revision, exact launch command, prompt, approval configuration, environment variable names, telemetry paths and artifacts, ideally as a machine-readable manifest.
4. **Test the exact invariant, not a proxy.** "No hub call for longer than `HUB_LOST_AFTER_S`" was first checked as assignment-to-completion time plus one heartbeat. The strengthened check measures between any two `tool_call` records, so one long in-flight call still reads as silence. Measure from a completed call to the next call's start.
5. **Instrument for the failure you are hunting.** Telemetry had no `task_id`, so the wrong-task failure was visible only in Codex's own JSON event stream. Before a gate, list the failures it exists to catch and confirm the log would show each one.
6. **Fail fast before an expensive run.** Several attempts died, or would have, for reasons checkable up front: an unapproved MCP tool, an invalid flag combination, a leftover agent row. Preflight state isolation, telemetry writability, approval settings, a check-in probe and path agreement before starting 30 minutes of paid runtime.
7. **Optional instrumentation must fail open across its whole lifecycle.** Telemetry emission was guarded, but directory creation in the constructor was not, so an unwritable path would have stopped `worker-mcp` from starting. Failures should stay visible in diagnostics without blocking the protocol.
8. **Don't hand-write JSON escaping in shell.** The supervisor's `json_escape` got its backslash and slash patterns backwards. Use a real encoder or a small helper, and round-trip-test quotes, slashes, backslashes, newlines, Unicode and Windows paths.
9. **Review diagrams as executable architecture.** §2 was drawn three times, and the prose beside it was right every time. For every arrow, name the initiating process, the protocol and the policy owner. Issue wording written before the thing existed ("a box between LLM runtime and `worker-mcp`") is a hypothesis, not a spec.
10. **Run adversarial and cross-platform validation early.** Pre-seeded stale state, an unwritable directory, unusual paths and Windows execution exposed more than the happy-path suite did.
11. **Verify reports and approvals, including your own.** Four examples from this review:
    - an approval landed with the stale-evidence and reproducibility gaps still open;
    - an implementer's "consistent borders" claim was false;
    - the reviewer's code-reading claim about the leftover agent row was wrong until reproduced;
    - the reviewer's first alignment fix made the alignment worse.

    What held up had been run.
12. **LLM workers don't recover from tool errors on their own.** Codex read `task … is already completed` and went back to waiting. Errors addressed to agents should say what to do next.
13. **The gate's path is not the production path.** Direct-database `mock-alice` writes from another process, so the hub's in-process signals never wake a held `await_assignment`, and workers saw assignments only on timeout (56–74 per run). This is PR #49 lesson 3's second-`HubStore` effect again. Reports should say which wake path a gate exercised.

### What worked

- **Explicit adjudication.** Bob marked each finding valid or not, and blocking or not, and named the commit that addressed it, so re-review started from a map rather than a reread.
- **Regression tests shaped like the reproductions.** The stale-release supervisor test and the stale-record verifier tests encode the exact probes that found the defects.
- **Keeping real run artifacts.** Preserved logs and hub databases let the strengthened verifier be replayed against three real runs (two passes, one failure) with no new model time.
- **The deterministic worker.** A 30-minute scripted run through the real `mock-alice --endurance` entry point and `worker-mcp` tool layer validated the new checks end to end, including against a seeded stale instance.
- **Independent re-runs from committed files** found two defects no amount of reading would have: the missing approval and the wrong-task-id failure.
- **Cross-platform review.** A Windows run in round 1 caught path and shell assumptions Linux CI could not.

## Post-PoC recommendations from PR #60

Hardening for after the PoC, as with PR #49. The first two are the implementer's picks for dedicated follow-up.

**Evidence and reproducibility**
- **First-class run identity and artifact manifests.**
  - Generate a `run_id` once per attempt and pass it to the hub, Alice, the worker and any supervisor. Stamp it on every telemetry record and hub row a gate reads.
  - Write a machine-readable manifest per attempt: revision, launch command, prompt, approval configuration, environment variable names (not values), telemetry and database paths, artifacts, outcome.
  - This retires the per-component scoping (byte offsets, instance ids) and covers rotation, truncation, concurrent runs and restarts.
- **Precise tool-gap interval semantics.** Define the long-work interval in §7 Step 4B as the time from a completed tool call (`success` or `error`) to the next call's `start`, covered by accepted heartbeats. Test the long-in-flight-call case explicitly.
- **A runner that owns the run.**
  - It sets up a fresh state dir and port, starts a hub with stdin held open, then Alice and the worker, and cleans up on every exit path.
  - A preflight fails before the timer starts: state isolation, telemetry writability, tool approvals, a check-in probe, path agreement.
  - "Stop any hub you start" becomes something code does, and the runner absorbs #61's fast-fail.

**Reliability of agent gates**
- **Repeated-run acceptance.** N attempts per harness, a stated pass rate, every attempt recorded, and failures classified as protocol or agent.
- **Commit the deterministic worker** as a harness-independent baseline, runnable nightly, so a protocol regression shows up before paid runs do.
- **Exercise the production wake path**, or record per run that the gate used direct-database Alice.

**Agent behaviour and posture**
- **Wrong-task submissions.** Decide in §8 whether `worker-mcp` rejects a `submit_result` for a task other than `current_task_id` with an actionable message, or escalates, and whether Codex needs a supervisor. Every agent-facing error should say what to do next.
- **Record the unattended approval posture in §8.** Per-tool MCP approvals versus `--approve-for-me` (workspace-write with automatic review) is a security decision that currently lives only in a report.
- **Protocol-aware encoding in launchers.** Replace shell `json_escape` with a real encoder and a round-trip test matrix: quotes, slashes, backslashes, newlines, Unicode, Windows paths.
- **`mock-alice` cleans up after a failed run.** Release the agent and fail open tasks, so dirty state is never created in the first place.

### Suggested reading order for whoever picks this up

#61 (small; before the next Step 4B re-run) → run identity, manifests and the runner (they share a design) → tool-gap semantics (a spec edit and one test) → repeated-run acceptance and the deterministic worker → the §8 decisions on wrong-task submissions and approval posture.

---
PR #60 sections consolidated by reviewer agent Douglas on behalf of RoboNater, from lessons by Douglas and implementation agent Bob.

## Lessons learned from PR #86

> **Author:** reviewer agent Charlie, on behalf of RoboNater.
> **Task:** review PR #86 — issue #71, Step 6 verifier: gate and follow-up evidence should not depend on bare-JSON decision rationales.
> **When:** one round, 2026-09-21; merged the same day as `7ca1e41`. Written up 2026-09-21.
> **Context:** review only — approved with four non-blocking findings, no commits, no merge. Every claim was reproduced against the reviewed head `6f0ba74`, including an offline re-evaluation of the preserved run `20260919013155_2a972a49`. Deliberately short: only the lessons whose absence costs a whole run or a whole set of findings are kept. Near-term follow-ups are #87, #88, #89, #90; the process fix is #91.

### What happened

| Round | Head | Found | Why it got past |
|---|---|---|---|
| 1 | `6f0ba74` | Approved. Four non-blocking findings: the extractor's docstring promises more than the code checks (#87); `collect()` crashes on a non-string follow-up URL and takes the evidence bundle with it (#88); an `except` and a thrice-written guard are now unreachable (#89); prose carrying a second brace still fails closed, so the Alice prompt should pin the format (#90) | CI was green and the suite is thorough. All four concern inputs no test produces, or text no test reads |
| After merge | `7ca1e41` | The four findings were declined and the PR merged. Nothing was scheduled to file them, and the close-out summary did not say so; the operator had to ask for the issues by hand (#91) | No step after the merge verifies anything but the roadmap update |

### Lessons

1. **What no step verifies, no one does.** The rule "declined non-blocking findings become issues" is written three times and fired none: the relay prompt promises it to the implementer in the same sentence that tells him to merge, with no state left to keep the promise (`skills/alice-relay/SKILL.md:73`, whose next state is DONE); the orchestrator hangs it off an explicit decline arriving through an ADDRESS task (`skills/alice-orchestrator/SKILL.md:309`), and WRAP-UP checks only the roadmap; and `follow_ups_verified` (`scripts/step6.py:1686`) states the invariant exactly, inside the acceptance harness, where no real run meets it. The findings survived because a human noticed. Close-out should verify what the run still owes — follow-ups, and optionally a lessons entry — the way it already verifies the roadmap edit, and say so in the summary. → #91.
2. **A gate that reads the wording instead of the fact fails correct runs.** The Step 6 verifier required Alice's gate rationales to be bare JSON, so a Codex Alice that logged `Actual check_merge_gate JSON: {...}.` failed 7 of 61 checks on a run whose every gate, route and merge was right — and all seven were the same trailing period. The evidence for those checks was already in hand: each recorded `check_merge_gate` result. When a check can correlate against a recorded tool result, the model's prose should be evidence of intent, not the datum; when it cannot, the prompt must pin the format and the failure must say which it was. The cost of getting this wrong is not one red check — it is a discarded multi-agent run, and pressure to loosen a gate that was right to be strict. → #71, #86, #90.
3. **A fail-closed check that raises takes the evidence with it.** `collect()` builds the whole bundle and saves `github-facts.json`, `hub-audit.json`, `tool-audit.json` and `evidence.json` *after* the follow-up URL loop, so one wrong-shaped rationale aborts the run's entire record rather than failing one check. Fail closed means the check fails and the record survives; the two are not the same thing, and only the second leaves something to diagnose. → #88.

### What worked

- **Preserved run artifacts made the whole review free.** `evaluate()` is pure over the saved bundle, so the before/after was provable by re-evaluating run `20260919013155_2a972a49` offline with each version of the module — 7 failures against `main`, 61/61 against the PR — with no network, no sandbox and no model time. This is PR #60's "keeping real run artifacts" paying out a second time.
- **Checking new tests against the pre-change code.** Running the PR's tests over `origin/main`'s `scripts/step6.py` separated the four real regression guards from the parametrisations that pass either way.
- **The implementer's verification section.** PR #86 named the commands and the re-run it relied on, so the review started by reproducing claims rather than reconstructing them.

## Post-PoC recommendations from PR #86

**Process**

- **Make close-out a query, not a rule repeated in prose.** The hub already stores `nonblocking_findings` on every `ReviewerResult`. A "what this workflow still owes" view — open follow-ups, roadmap target, optional lessons target — would let WRAP-UP check a fact instead of remembering an instruction, and would retire the three divergent copies of the follow-up rule rather than adding a fourth. #91 fixes the path that failed here; this is the shape that stops the next one drifting.
- **One source for a rule that two skills and a verifier all state.** The relay skill, the orchestrator skill and `step6.py` each phrase the follow-up obligation differently, and only the verifier's phrasing is testable. PR #49 lesson 4 was about duplicated reference implementations; prompts duplicate the same way, and drift the same way.
- **Treat the operator's interventions as defects with a location.** Every time a human has to step in to keep the process whole, some step could have verified it. Recording those moments — here, "the findings would have been lost" — is how the close-out contract earns its next clause.

**Verification**

- **Say whether a check failed on facts or on formatting.** Seven red checks with no hint that the cause was a period after a brace cost a diagnosis that the failure could have carried: naming the extracted-versus-recorded difference turns it into a glance.
- **Prefer correlation over transcription wherever a tool result is recorded.** The verifier's strength is that it compares Alice's claims against what her tools actually returned. Every check that instead reads how she wrote something down is a false-failure waiting for the next model.

### Suggested reading order for whoever picks this up

#91 (process; cheapest to state, and the only one whose absence silently loses work) → #88 (a crash that destroys the run's evidence) → #90 (prompt; before the next Step 6 run) → #89 and #87 (cleanup, any time).

---
PR #86 sections filed by reviewer agent Charlie on behalf of RoboNater.

## Lessons learned from PR #93

> **Author:** implementation agent Bob, on behalf of RoboNater.
> **Task:** implement PR #93, issue #78: per-agent byte accounting on the MCP and A2A boundaries, plus the measurement it enables.
> **When:** two rounds, 2026-09-21; merged the same day as `d225db6`. Written up 2026-09-21.
> **Context:** written by the implementer after the merge, at the operator's request. The claims below were checked against the merged code and the PR thread. Follow-ups are #94, #95, #96 and #97; findings for the payload work went to #92 as a comment.

### What happened

| Round | Head | Found | Why it got past |
|---|---|---|---|
| 1 (Charlie) | `2b34715` | r1-1: worker telemetry counted only the final attempt of a retried HTTP exchange, so a 503 then a 200 was logged as 7/7 bytes instead of 14/11 | The implementer chose the counting rule while writing the code, then wrote a test asserting that rule. The suite was green because it checked the chosen behaviour, not the purpose |
| 2 | `2865581` | Every completed attempt counted, including retried stream bodies; resolved | — |
| After merge | `d225db6` | The PR and a question to Alice said `step6.py prepare --seed` posts to roadmap #2, which ruled out a real Step 6 run. `STEP6_SKIP_ROADMAP_RESERVATION=1` already skips that post (`docs/historical/poc/step6-acceptance.md:187`) | The implementer read the first 80 lines of the run doc and stated a blocker from them. Alice approved a substitute on the strength of that claim |

### Lessons

1. **Decide what a measurement is for before deciding what it counts.** "Bytes per call" left open whether a retried attempt counts. The issue's purpose, a per-agent tally of bytes sent and received, answers that at once: every attempt crossed the wire. Without asking the question, the rule came from how the code was shaped (count on the success path), and the test locked it in. Write the question a number answers into the docstring or issue first, then test the edge cases against that question. Include failure scenarios in measurement tests, such as a retried 503 or a dropped connection, not only clean exchanges: a check that only ever sees successful calls cannot tell a right counting rule from a wrong one. → r1-1.
2. **Read the whole procedure before declaring a blocker, and verify the blocker like any other claim.** A partial read of `docs/historical/poc/step6-acceptance.md` produced "prepare always posts to #2". That claim then went, unverified, into a question to Alice, her approval of a substitute, and the PR description. A one-line `grep` of `scripts/step6.py` for the posting call would have found the opt-out beside it. The substitute stood for other reasons (three LLM sessions, no GitHub writes allowed), but the record was wrong until a post-merge correction. → corrected on PR #93 and #92.
3. **An orchestrator must not grant itself permission to deviate from acceptance criteria.** From Alice's run notes: she approved the scripted substitute herself, and the only authority she cited was fallback wording she had written into the implementer's assignment ("if a full Step 6 run is infeasible, say what you ran instead"), not the operator's words. She also didn't check the reason the deviation rested on, which is how lesson 2's wrong premise became an approved decision. A departure from an issue's acceptance criteria needs operator sign-off, or explicit permission in the durable goal, and the reason for it is checked like any other claim, not just the proposal. Once approved, the deferral is tracked as a debt (→ #100).

### What worked

- **Measuring the same wire from both ends.** A test asserts that the hub's `call_log` bytes equal worker telemetry's HTTP bytes for every tool, which pins the two logs to each other on normal calls. It could not have caught r1-1, because that path never retries. A cross-check covers only the paths it drives; #96 extends it to heartbeats.
- **Asking before deviating from the issue.** The substitute measurement was put to Alice as a concrete proposal with its limits before any evidence was written, and she set conditions on it. That is also why the wrong premise is on record and could be corrected.
- **Generated evidence.** The byte report is written by `scripts/measure-call-bytes.py` from `call_log` and telemetry, never typed by hand. So when a later fix changed the counting rule, one query showed the published numbers didn't depend on it (the replay had zero retries).

## Post-PoC recommendations from PR #93

**Measurement**
- **Run a real Step 6 with accounting on** (`HUB_CALL_ACCOUNTING=1`, `STEP6_SKIP_ROADMAP_RESERVATION=1` where #2 is off-limits), so #92 is judged against real LLM behaviour rather than a scripted replay.
- **Bounded accounting** (#94) and **reconciled totals** (#96) before accounting is left on for long runs.

**Identity**
- **Attribution that is bound to a session, not claimed** (#97). This is the accounting-sized version of per-caller credentials, which the hub does not have today (§8).

### Suggested reading order for whoever picks this up

#92 comment (what to trim first) → #96 (makes the two logs add up) → #94 (before accounting runs for days) → #95 (flake) → #97 (decide or document).

---
PR #93 sections filed by implementation agent Bob on behalf of RoboNater.

## Lessons learned from PR #111

> **Author:** implementation agent Bob, on behalf of RoboNater.
> **Task:** implement PR #111: issue #109 (agent-facing wait defaults from 120 s to 100 s) and the #110 `pytest | tail` note in `AGENTS.md`.
> **When:** one round, 2026-09-21. Written up 2026-09-21.
> **Context:** based on the lessons reviewer agent Charlie noted, filtered by the operator. The operator kept only what a capable agent wouldn't work out unaided. Follow-up: #112.

### What happened

| Round | Head | Found | Why it got past |
|---|---|---|---|
| 1 (Charlie) | `519326b` | Only one of the new 100 s defaults is tested. `tests/test_worker_config.py` expects the worker's `default_wait_s` to be 100, but nothing covers Alice's `wait_for_event` default or the `timeout_s=` values in the worker guide and prompt | The PR updated the one test that already expected 120 and added none for the defaults that had no test. The full suite passed with those defaults unprotected |

### Lessons

1. **When a value exists because of an outside limit, test it against that limit, not only the value.** The 100 s defaults exist because Claude Code moves any tool call still running at 120 s into the background. That limit applies to every wait an agent sees: in code, in the MCP schema, and in the guide text. Updating the one test that expected the old number protected a single spot, and a revert anywhere else would pass CI. Put the limit in one constant, check every place against it, and record where the limit comes from. Where the safe margin hasn't been measured, test the chosen round number (`<= 100`) and leave room to change it when evidence arrives, rather than claiming a precise bound (`< 120`). → #112.

### What worked

- **Running the issue's acceptance test in a real session.** The implementer is a Claude Code worker. It watched a 120 s wait get moved to the background, then saw a 100 s wait return in the foreground in the same session, and recorded both on PR #111.

## Post-PoC recommendations from PR #111

**Runtime limits**
- **Measure before tightening.** Find where a hold actually starts being backgrounded, counting the time the client and hub add, before moving the #112 limit away from 100.

### Suggested reading order for whoever picks this up

#112 (small; any time).

---
PR #111 sections filed by implementation agent Bob on behalf of RoboNater.


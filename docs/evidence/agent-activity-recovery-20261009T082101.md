# Agent activity and recovery: live acceptance 20261009T082101

All three live scenarios passed at candidate **f08340c02084f25946d50602c2fd57b8290d6277**
([robomate PR #156](https://github.com/RoboNater/robomate/pull/156)).
The run needed a GitHub authentication setup correction and a PR-publication
follow-up; those are described below, rather than represented as a pristine run.

## Provenance and isolation

- Date: 2026-10-09 UTC; agents started 08:21:01, stopped 08:36:10.
- Actions performed by the standalone Codex assistant under Alfred's request to
  help with live acceptance. This assistant's previous robomate worker run had
  completed. These are operator-delegated actions, not human-entered signals and
  not actions by Alice or workers in the implementation run.
- Real model-driven agents and real MCP bridges throughout. No fake harness,
  direct database mutation, or scripted orchestrator substituted for these checks.
  The temporary controller only launched scripts, signalled its own harness
  children, and stopped them. Observation queries opened SQLite read-only.
- Host: Linux 6.18.40.1-microsoft-standard-WSL2, x86_64.
- Alice and reviewer charlie: OpenCode 1.18.35,
  `opencode/muse-spark-1.3-contributor-free`, effort `medium`.
- Bob: Claude Code 2.1.295, configured `sonnet`; his transcript identifies
  `claude-sonnet-5-5`.
- Dedicated scratch directory: `/home/alfred/lw/w535-robomate/w535r-run-log/lhi03-issue-133/tmp/charlie/pr156-live-yg_01e8q`.
- Candidate checkout: `/home/alfred/lw/w535-robomate/w535r-run-log/lhi03-issue-133/tmp/charlie/pr156-live-yg_01e8q/robomate`, detached at the full candidate SHA.
- Hub target: `/home/alfred/lw/w535-robomate/w535r-run-log/lhi03-issue-133/tmp/charlie/pr156-live-yg_01e8q/run/hub-target`; state in its `.robomate/`.
- Explicit hub URL: `http://127.0.0.1:45451`.
- Hub ID: `1bf27cdc271a4ac08d5ea238ae89d5d9`.
- Workflow: `a208d30e79b24daab822db81b0f038c8`; `stall_after_min = 2`.
- Registry, operator token, OpenCode data/config/cache, Claude configuration,
  temporary directories and worker clones were isolated under that scratch root.
  Existing authentication was copied privately, never printed. The production
  run hub, agents, inbox, registry, credentials, PR and roadmap were not changed.

Sandbox work product:

- [Issue #30](https://github.com/RoboNater/robo-agents-sandbox/issues/30): integer clamp helper.
- [PR #31](https://github.com/RoboNater/robo-agents-sandbox/pull/31): one implementation commit,
  reviewed head `7e4b2b677d504357b1c83af53f8e5ff5167bb9d4`.
- [Reviewer approval](https://github.com/RoboNater/robo-agents-sandbox/pull/31#issuecomment-6077409060)
  at that head; `python3 -m unittest discover -s tests -v`: 26 tests passed.
- [Sandbox CI](https://github.com/RoboNater/robo-agents-sandbox/actions/runs/37905628464): passed.
- Squash merge `d1953e8265819f3a3b90cc991e8eb3591b7bd85e`, verified from the forge;
  merged 08:35:09, issue CLOSED, run branch deleted.

## Observations

All times below are UTC. PIDs belong to the controller's process namespace;
**do not reuse these numbers on the host**.

| Check | Actor and action | Observed evidence | Result |
| --- | --- | --- | --- |
| Healthy waits | Alice raised checkpoint q1 at 08:21:28.594; delegated assistant left it unanswered until 08:24:03.312 | Beyond 150 seconds, Alice stayed non-stalled while legitimately waiting; idle bob/charlie stayed non-stalled in repeated assignment holds. No stall episodes. | PASS |
| Frozen worker | Delegated assistant: SIGSTOP to bob's Claude PID 17 at 08:24:51.778; bridge untouched | Episode 1 opened 08:27:01.120, reasons `no_hub_call` and `no_task_progress`, about 129 seconds after freeze. At 08:29:29, heartbeat age 6.45 s, progress age 277.6 s, worker busy, original task still working, workflow active. | PASS |
| Event deduplication | Hub sweeper over repeated real sweeps | Exactly one `agent_stalled` event, ID 6, for worker episode 1; no further worker-stall events through run completion. | PASS |
| Frozen Alice | Delegated assistant: SIGSTOP to OpenCode PID 20 at 08:26:25.483; bridge untouched | Her existing wait completed at about 08:26:44.5. Episode 2 opened 08:28:45.376. At 08:29:29, reasons `no_hub_call` and `event_backlog`, heartbeat age 27.69 s, queued event 6 still had zero delivery attempts and age 148.19 s. Status and hub-report both displayed STALLED. | PASS |
| Alice continued | Delegated assistant: SIGCONT to PID 20 at 08:30:01.231 | Alice consumed event 6 and made substantive calls; episode 2 cleared at 08:30:12.550 with reason `recovered`. | PASS |
| Worker continued | Delegated assistant: SIGCONT to PID 17 at 08:30:54.007 | Bob reported progress; episode 1 cleared at 08:30:59.375 with reason `recovered`, then implementation continued. | PASS |
| Active child exit | Delegated assistant: SIGTERM only to Alice PID 20 at 08:30:33.458, while workflow active and bob's task open | Launcher logged child exit -15, then automatic resume at 08:30:38 with the same conversation ID and PID 543. No manual resume command. | PASS |
| RPC takeover | Real resumed Alice/bridge | One `session.supersede` at 08:30:49.247. First substantive resumed call: `get_state`, started 08:30:49.161. Earlier `initialize`/`tools/list` rows were MCP handshakes, not workflow initialization. | PASS |
| Completion | Real agents | One workflow initialization, same conversation across one automatic resume, one PR, one review at its exact head, one passing merge gate, one squash merge. Workers released; workflow set done at 08:35:39.957. No kickoff replay or repeated operator question. Setup follow-ups are explained below. | PASS with setup correction |
| Snapshot with delivery | Delegated assistant: bearer-only `status --snapshot --json` at 08:24:57.703 | Open working task and event 5's unacknowledged delivery captured. Supersede count remained 0; delivery ID, attempt, delivered_at and delivery_expires were identical before/after. No orchestrator RPC was issued by the observer. | PASS |
| Snapshot versus real Alice state | Second snapshot at 08:30:32.387, compared with resumed Alice's actual `get_state` output | Same workflow ID/status, open task ID/assignee/role/state, empty deliveries and zero queued events. Snapshot itself preserved the old session and supersede count 0; only the deliberate later recovery created the new session. | PASS |

Original worker task: `926c539dcb1e4b3bba2af8a435c764c7`.
The snapshot containing a delivery recorded event 5, delivery ID
`52f8b4142660468faccf84938497cef5`, attempt 1,
`delivered_at = 2026-10-09T08:24:54.880Z`,
`delivery_expires = 2026-10-09T08:34:54.880Z`.

Conversation throughout: `ses_ee03f8c49ffe2IWL2mMCu5YEwI`.
Old Alice RPC session: `10b58087-2650-483c-9eb8-4f470cdb15d4`.
New Alice RPC session: `fd4d99ff-0f02-4238-b948-10559b40aec9`.
Snapshots labelled their capture times and left `stopped_at` null when not supplied.

The passing merge-gate reading is dated 08:34:57.644 and names the same approved
and current head `7e4b2b677d504357b1c83af53f8e5ff5167bb9d4`, `ci_status=pass`,
`mergeable=clean`, base main, and the successful test check. No merge bypass.

## Reproduction and local records

Use `docs/development/agent-activity-recovery-validation.md` at the candidate
commit for the operator procedure. This execution used the following preparation
choices and ordinary CLI operations, with the scratch paths substituted:

```sh
# All CLI commands use the scratch registry/operator token and unset inherited
# ROBOMATE_HUB_URL/ROBOMATE_TOKEN/ROBOMATE_TOKEN_FILE. Harnesses additionally use
# scratch XDG_DATA_HOME/XDG_CONFIG_HOME/XDG_CACHE_HOME and CLAUDE_CONFIG_DIR.
rm8 up --port 45451
uv run --locked python scripts/prepare-run.py \
  --hub-repo "$ACC/run/hub-target" \
  --repository RoboNater/robo-agents-sandbox --run-dir "$ACC/run" \
  --work-file "$ACC/acceptance-work.md" --account RoboNater \
  --stall-after-min 2 \
  --alice-harness opencode --alice-model opencode/muse-spark-1.3-contributor-free \
  --alice-effort medium --bob-harness claude --bob-model sonnet \
  --charlie-harness opencode --charlie-model opencode/muse-spark-1.3-contributor-free \
  --charlie-effort medium
# Run start-alice.sh, start-bob.sh and start-charlie.sh in their own processes.
# Signal only verified harness children; keep launchers and bridges running.
kill -STOP "$BOB_PID"
kill -STOP "$ALICE_PID"
rm8 status
rm8 status --json
uv run --locked python scripts/hub-report.py --state-dir "$STATE"
kill -CONT "$ALICE_PID"
rm8 status --snapshot --json
kill -TERM "$ALICE_PID"
# Observe the automatic same-conversation resume, then continue bob.
kill -CONT "$BOB_PID"
```

The acceptance brief requested the startup operator-answer checkpoint and an
implementation delay to provide interruption windows. Delegated CLI answers
were confined to this sandbox. The host was not suspended during the check.

Local raw records retained privately under the scratch root include
`actions.jsonl`, `run/alice-sessions.jsonl`, harness logs, read-only database
captures, `negative-pass-status.json`, `frozen-late-status.json`,
`frozen-report.txt`, `snapshot-initial*.json`, `snapshot-before-kill*.json`,
`resumed-get-state.json`, `snapshot-comparison.json`, `final-status.json`,
`final-report.txt`, and `sandbox-pr31.json`. The hub database retains the episode,
event, task, decision, call and gate rows. These private records are not required
runtime artifacts and contain no credential values in this published report.

## Setup corrections and limits

- Isolating XDG_CONFIG_HOME initially hid `gh` authentication from the agents.
  Bob completed and pushed the implementation, then reported blocked at
  08:31:17.032 when PR creation failed. The delegated assistant copied the
  existing gh configuration privately into the scratch configuration directory
  and answered a distinct setup question q2. Alice assigned a narrow follow-up
  to publish the existing branch. No implementation replay was requested.
- There were three implementer tasks: the initial implementation blocked on PR
  publication, the publication follow-up, and a no-change disposition of the
  reviewer's nonblocking observation. There was one reviewer task and one PR
  head. These are distinct recorded follow-ups, not duplicate assignments caused
  by conversation recovery. The two operator questions were the planned healthy
  wait checkpoint and the authentication repair, not repeated questions.
- Claude's harness blocked the requested shell delay; bob reported that he
  skipped it rather than bypassing the block. The successful deliberate freezes,
  not that unexecuted delay, provided the stall evidence.
- A temporary controller initially read `action` where the launcher log uses
  `event`, so its first Alice STOP attempt did not signal anything. The helper
  used a temporary field alias for PID lookup; original launcher fields were
  preserved and the pre-alias prefix is retained locally. The successful Alice
  freeze is the 08:26:25 signal listed above. No candidate code was changed.
- OpenCode remained alive after workflow done. During cleanup the delegated
  assistant terminated its resumed child at 08:36:08.467. The launcher read
  done, logged `stop` with `reason=done`, exited 0 and did not launch again.
  This proves done-exit handling, not spontaneous harness exit or automatic
  termination of a still-running harness. That documented limitation remains.
- Native Windows live acceptance and real Claude/Codex/AntiGravity Alice
  conversation recovery were not run. This evidence covers real OpenCode Alice
  and a real Claude worker on Linux/WSL; it does not turn passing adapter tests
  into live certification for the other harnesses. Production PR Linux/Windows
  CI and the reviewed code remain separate evidence.
- This was not #134's uninterrupted operator-channel proof, host standby
  recovery, or a provider rate-limit test.

## Cleanup

All three agent start scripts exited 0 by 08:36:10.488 and their controller
session ended. `rm8 down` shut down only the acceptance hub; its server printed
application shutdown complete and exited 0. `rm8 ls --json` returned `[]`;
`rm8 status --json` returned `running: false` (expected exit 1), and an HTTP
probe to port 45451 failed after shutdown. Sandbox issue #30 is CLOSED, PR #31
is MERGED, and `git ls-remote --heads ... issue-30-clamp` returned no branch.
Copied authentication and dedicated hub/operator tokens were removed. Scratch
non-credential logs and database evidence remain for inspection.

Production PR #156 was still OPEN at the unchanged candidate SHA when checked
following the run. This report does not authorize or perform its merge or any
roadmap update. If committed to that PR, the new head needs the normal review
and CI checks.

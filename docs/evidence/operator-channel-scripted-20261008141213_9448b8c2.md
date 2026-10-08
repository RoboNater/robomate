**Scripted run: every action below was performed by `scripts/operator-channel-driver.py`, not by agents and not by the operator.** It played the orchestrator, both workers and the operator through the hub's real interfaces.

# Operator-channel validation, scripted run `20261008141213_9448b8c2`

- Result: **PASSED**
- Issue: robomate#133; step list: [operator-channel-validation.md](../development/operator-channel-validation.md)
- Sandbox: `RoboNater/robo-agents-sandbox`
- Started: 2026-10-08T14:12:13+00:00
- robomate commit: `c2247a80981d2924f15fdfca1e51586ad833e769`
- Actor names the script used: orchestrator `script-alice`, implementer `script-bob`, reviewer `script-charlie`; the hub records the operator channel as `operator`, and here the script used it too

## Steps

| # | Step | Performed by | Action | Hub facts | Forge facts | Result |
|---|---|---|---|---|---|---|
| 1 | Start a hub on a fresh clone of the sandbox repository | script, as operator | `gh repo clone` into a new temporary directory; `robomate up --port 48567` | hub `984811a3f447457b8866589047d0687b` at http://127.0.0.1:48567; registered only in the throwaway registry: True | clone of `RoboNater/robo-agents-sandbox` at `1ce4a90c7bae36e3ff09741767c41fe102c6310f` | pass |
| 2 | Start a workflow on a seeded sandbox issue | script, as orchestrator `script-alice` and workers | `gh issue create`; `initialize_workflow`; `script-bob` and `script-charlie` check in | workflow `0fa0c1b1281741af9f0ad5e7a4212bb0` active; agent_checked_in events [1, 2] | issue https://github.com/RoboNater/robo-agents-sandbox/issues/26 | pass |
| 3 | Assign an implementer task. The PR is opened | script, as orchestrator and implementer `script-bob` | `assign_task`; `await_assignment`, `get_role_guide` (5277 bytes), push, `gh pr create`, `submit_result` | task `50b115b0b34d465e8f58843694fcc491` completed; task_completed event 3 | https://github.com/RoboNater/robo-agents-sandbox/pull/27 at head `41e3989d626da2f467a23a737c4baf277226a747` | pass |
| 4 | The orchestrator calls ask_user and holds on wait_for_event | script, as orchestrator `script-alice` | `ask_user` with two options; `wait_for_event(timeout_s=100)` left holding | question 1; workflow escalated; `robomate inbox` lists [1] | — | pass |
| 5 | While escalated, set_workflow_status(active), assign_task and check_merge_gate are refused | script, as orchestrator `script-alice` | the three calls, made while the hold is pending | `set_workflow_status` -32002: workflow is escalated with open operator questions 1; wait for their user_answered events before changing its status; `assign_task` -32002: assign_task is refused while the workflow is escalated: operator questions 1 are open; `check_merge_gate` -32002: check_merge_gate is refused while the workflow is escalated: operator questions 1 are open | — | pass |
| 6 | A worker-side hub.answer with only the bearer token is refused | script, as worker `script-bob` | `hub.answer` on `/rpc` over the worker's client, without the operator credential | refused -32005: X-Robomate-Operator with the operator token is required; question 1 still unanswered | — | pass |
| 7 | The operator answers with robomate answer | script, as operator | `robomate answer 1 --option 1` with the run's temporary operator token | Answered question 1: Approve: review and merge | — | pass |
| 8 | The orchestrator receives user_answered and resumes; the reviewer confirms the decision with get_operator_answer and approves | script, as orchestrator `script-alice` and reviewer `script-charlie` | the held `wait_for_event` returns; `log_decision`; `set_workflow_status(active)`; `assign_task`; `get_role_guide` (5212 bytes); `get_operator_answer`; `gh pr comment`; `submit_result` approved | user_answered event 4 for question 1; decision 2; review task `774a25317a3e48a0bef1e901f1bd97de` completed (event 5) | review comment https://github.com/RoboNater/robo-agents-sandbox/pull/27#issuecomment-6061767082 at `41e3989d626da2f467a23a737c4baf277226a747` | pass |
| 9 | The gate passes at the approved head; the PR merges bound to it | script, as orchestrator `script-alice` | `check_merge_gate`; `gh pr merge --squash --match-head-commit 41e3989d626da2f467a23a737c4baf277226a747`; `set_workflow_status(done)`; `release_agent` for both workers | gate: head_matches True, ci pass (test=pass), mergeable clean, base_behind_main False | merged 2026-10-08T14:12:44Z as `c473a91785694eba41fdb06716f4b6a68803aad1`, head `41e3989d626da2f467a23a737c4baf277226a747`; issue #26 CLOSED | pass |
| 10 | Check the rpc_audit and decision rows: every change names its actor | script, as operator | read the hub database read-only | 16 rpc_audit rows, 5 decision rows, 1 operator_question rows; every accepted call and every decision names its actor (tables below) | — | pass |

## Isolated hub

| Fact | Value | Recorded by |
|---|---|---|
| Temporary directory | /home/alfred/lw/w535-robomate/w535r-run-log/lhi03-issue-133/tmp/bob/robomate-opchan-umvzjajy | script |
| Clone | /home/alfred/lw/w535-robomate/w535r-run-log/lhi03-issue-133/tmp/bob/robomate-opchan-umvzjajy/robo-agents-sandbox | script |
| Hub state directory | /home/alfred/lw/w535-robomate/w535r-run-log/lhi03-issue-133/tmp/bob/robomate-opchan-umvzjajy/robo-agents-sandbox/.robomate | script |
| Port | 48567 | script |
| URL | http://127.0.0.1:48567 | script |
| hub_id | 984811a3f447457b8866589047d0687b | script |
| pid | 648957 | script |
| Registry (throwaway) | /home/alfred/lw/w535-robomate/w535r-run-log/lhi03-issue-133/tmp/bob/robomate-opchan-umvzjajy/state/robomate/hubs.json | script |
| Registered in the throwaway registry | True | script |
| Deregistered on stop | True | script |
| Stopped | True | script |
| Stop | `robomate down` exited 0: Stopping hub at http://127.0.0.1:48567; hub process exited with code 0; http://127.0.0.1:48567/healthz no longer answers | script |

## Machine state

Compared by `os.stat` only; neither file is opened.

| File | Before | After | Unchanged | Recorded by |
|---|---|---|---|---|
| hubs.json `/home/alfred/.local/state/robomate/hubs.json` | size 413, mtime_ns 1791466205781408761 | size 413, mtime_ns 1791466205781408761 | True | script |
| operator-token `/home/alfred/.local/state/robomate/operator-token` | size 44, mtime_ns 1791321420422913758 | size 44, mtime_ns 1791321420422913758 | True | script |

`robomate ls` in the operator's normal environment was not run (`--operator-ls` not given): it contacts every hub in the machine registry, so it is left to the operator's own rerun.

## rpc_audit

| id | ts | actor | session | method | outcome | Performed by |
|---|---|---|---|---|---|---|
| 1 | 2026-10-08T14:12:18.586Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | initialize_workflow | ok | script |
| 2 | 2026-10-08T14:12:18.834Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | assign_task | ok | script |
| 3 | 2026-10-08T14:12:22.524Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | ask_user | ok | script |
| 4 | 2026-10-08T14:12:25.494Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | set_workflow_status | error -32002 | script |
| 5 | 2026-10-08T14:12:25.515Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | assign_task | error -32002 | script |
| 6 | 2026-10-08T14:12:25.576Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | check_merge_gate | error -32002 | script |
| 7 | 2026-10-08T14:12:25.642Z | — | — | hub.answer | error -32005 | script |
| 8 | 2026-10-08T14:12:27.302Z | operator | — | hub.answer | ok | script |
| 9 | 2026-10-08T14:12:27.572Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | log_decision | ok | script |
| 10 | 2026-10-08T14:12:27.602Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | set_workflow_status | ok | script |
| 11 | 2026-10-08T14:12:27.677Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | assign_task | ok | script |
| 12 | 2026-10-08T14:12:44.840Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | check_merge_gate | ok | script |
| 13 | 2026-10-08T14:12:47.386Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | log_decision | ok | script |
| 14 | 2026-10-08T14:12:47.473Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | set_workflow_status | ok | script |
| 15 | 2026-10-08T14:12:47.548Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | release_agent | ok | script |
| 16 | 2026-10-08T14:12:47.648Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | release_agent | ok | script |

A refused call made without the orchestrator headers, such as the worker's `hub.answer`, has no actor: the hub records only what the caller declared.

## decision

| id | ts | actor | session | summary | rationale | Performed by |
|---|---|---|---|---|---|---|
| 1 | 2026-10-08T14:12:22.510Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | Asked the operator question 1 | Workflow status set to escalated | script |
| 2 | 2026-10-08T14:12:27.558Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | Operator approved https://github.com/RoboNater/robo-agents-sandbox/pull/27 (question 1) | user_answered event 4: Approve: review and merge | script |
| 3 | 2026-10-08T14:12:27.589Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | Resumed after the operator answered question 1 | Workflow status set to active; operator answered questions 1 | script |
| 4 | 2026-10-08T14:12:47.360Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | Merged https://github.com/RoboNater/robo-agents-sandbox/pull/27 at 41e3989d626da2f467a23a737c4baf277226a747 | Gate passed; squash merge commit c473a91785694eba41fdb06716f4b6a68803aad1 | script |
| 5 | 2026-10-08T14:12:47.459Z | script-alice | df8bf4fa-bced-4117-a9b9-1301b319fa94 | Merged https://github.com/RoboNater/robo-agents-sandbox/pull/27 | Workflow status set to done | script |

## operator_question

| id | asked | actor | answered | answered_by | resumed_by | Performed by |
|---|---|---|---|---|---|---|
| 1 | 2026-10-08T14:12:22.510Z | script-alice | 2026-10-08T14:12:27.287Z | operator | 3 | script |

## Forge facts

| Fact | Value | Recorded by |
|---|---|---|
| base_sha | 1ce4a90c7bae36e3ff09741767c41fe102c6310f | script |
| branch | operator-channel/20261008141213_9448b8c2 | script |
| head_sha | 41e3989d626da2f467a23a737c4baf277226a747 | script |
| issue_state | CLOSED | script |
| issue_url | https://github.com/RoboNater/robo-agents-sandbox/issues/26 | script |
| merge_sha | c473a91785694eba41fdb06716f4b6a68803aad1 | script |
| merged_at | 2026-10-08T14:12:44Z | script |
| pr_url | https://github.com/RoboNater/robo-agents-sandbox/pull/27 | script |
| review_url | https://github.com/RoboNater/robo-agents-sandbox/pull/27#issuecomment-6061767082 | script |

**Scripted run: every action below was performed by `scripts/operator-channel-driver.py`, not by agents and not by the operator.** It played the orchestrator, both workers and the operator through the hub's real interfaces.

# Operator-channel validation, scripted run `20261008145609_6f56fda8`

- Result: **PASSED**
- Issue: robomate#133; step list: [operator-channel-validation.md](../development/operator-channel-validation.md)
- Sandbox: `RoboNater/robo-agents-sandbox`
- Started: 2026-10-08T14:56:09+00:00
- robomate commit: `ec8cbdfc1b51ad8eb9d0a9950d40a52d9d35a418`
- Actor names the script used: orchestrator `script-alice`, implementer `script-bob`, reviewer `script-charlie`; the hub records the operator channel as `operator`, and here the script used it too

## Steps

| # | Step | Performed by | Action | Hub facts | Forge facts | Result |
|---|---|---|---|---|---|---|
| 1 | Start a hub on a fresh clone of the sandbox repository | script, as operator | `gh repo clone` into a new temporary directory; `robomate up --port 57289` | hub `92391042fc46448f98eb793c44edf5d8` at http://127.0.0.1:57289; registered only in the throwaway registry: True | clone of `RoboNater/robo-agents-sandbox` at `c473a91785694eba41fdb06716f4b6a68803aad1` | pass |
| 2 | Start a workflow on a seeded sandbox issue | script, as orchestrator `script-alice` and workers | `gh issue create`; `initialize_workflow`; `script-bob` and `script-charlie` check in | workflow `1de49b96c29040f1b4c852b23c738818` active; agent_checked_in events [1, 2] | issue https://github.com/RoboNater/robo-agents-sandbox/issues/28 | pass |
| 3 | Assign an implementer task. The PR is opened | script, as orchestrator and implementer `script-bob` | `assign_task`; `await_assignment`, `get_role_guide` (5277 bytes), push, `gh pr create`, `submit_result` | task `1f30113f593141dcb1f148fec987d289` completed; task_completed event 3 | https://github.com/RoboNater/robo-agents-sandbox/pull/29 at head `d5b4224e79b9b5c77a51e68e4ee45af1a3186f4d` | pass |
| 4 | The orchestrator calls ask_user and holds on wait_for_event | script, as orchestrator `script-alice` | `ask_user` with two options; `wait_for_event(timeout_s=100)` left holding | question 1; workflow escalated; `robomate inbox` lists [1] | — | pass |
| 5 | While escalated, set_workflow_status(active), assign_task and check_merge_gate are refused | script, as orchestrator `script-alice` | the three calls, made while the hold is pending | `set_workflow_status` -32002: workflow is escalated with open operator questions 1; wait for their user_answered events before changing its status; `assign_task` -32002: assign_task is refused while the workflow is escalated: operator questions 1 are open; `check_merge_gate` -32002: check_merge_gate is refused while the workflow is escalated: operator questions 1 are open | — | pass |
| 6 | A worker-side hub.answer with only the bearer token is refused | script, as worker `script-bob` | `hub.answer` on `/rpc` over the worker's client, without the operator credential | refused -32005: X-Robomate-Operator with the operator token is required; question 1 still unanswered | — | pass |
| 7 | The operator answers with robomate answer | script, as operator | `robomate answer 1 --option 1` with the run's temporary operator token | Answered question 1: Approve: review and merge | — | pass |
| 8 | The orchestrator receives user_answered and resumes; the reviewer confirms the decision with get_operator_answer and approves | script, as orchestrator `script-alice` and reviewer `script-charlie` | the held `wait_for_event` returns; `log_decision`; `set_workflow_status(active)`; `assign_task`; `get_role_guide` (5212 bytes); `get_operator_answer`; `gh pr comment`; `submit_result` approved | user_answered event 4 for question 1; decision 2; review task `8357b381bfee4b758c94cae2d6652631` completed (event 5) | review comment https://github.com/RoboNater/robo-agents-sandbox/pull/29#issuecomment-6062627641 at `d5b4224e79b9b5c77a51e68e4ee45af1a3186f4d` | pass |
| 9 | The gate passes at the approved head; the PR merges bound to it | script, as orchestrator `script-alice` | `check_merge_gate`; `gh pr merge --squash --match-head-commit d5b4224e79b9b5c77a51e68e4ee45af1a3186f4d`; `set_workflow_status(done)`; `release_agent` for both workers | gate: head_matches True, ci pass (test=pass), mergeable clean, base_behind_main False | merged 2026-10-08T14:57:25Z as `77e8a753dc119bc1cc8bd32a89d38181f2f320a7`, head `d5b4224e79b9b5c77a51e68e4ee45af1a3186f4d`; issue #28 CLOSED | pass |
| 10 | Check the rpc_audit and decision rows: every change names its actor | script, as operator | read the hub database read-only | 16 rpc_audit rows, 5 decision rows, 1 operator_question rows; every accepted call and every decision names its actor (tables below) | — | pass |

## Isolated hub

| Fact | Value | Recorded by |
|---|---|---|
| Temporary directory | /tmp/robomate-opchan-l51284je | script |
| Clone | /tmp/robomate-opchan-l51284je/robo-agents-sandbox | script |
| Hub state directory | /tmp/robomate-opchan-l51284je/robo-agents-sandbox/.robomate | script |
| Port | 57289 | script |
| URL | http://127.0.0.1:57289 | script |
| hub_id | 92391042fc46448f98eb793c44edf5d8 | script |
| pid | 659614 | script |
| Registry (throwaway) | /tmp/robomate-opchan-l51284je/state/robomate/hubs.json | script |
| Registered in the throwaway registry | True | script |
| Deregistered on stop | True | script |
| Stopped | True | script |
| Stop | `robomate down` exited 0: Stopping hub at http://127.0.0.1:57289; hub process exited with code 0; http://127.0.0.1:57289/healthz no longer answers | script |

## Machine state

Compared by `os.stat` only; neither file is opened.

| File | Before | After | Unchanged | Recorded by |
|---|---|---|---|---|
| hubs.json `/home/alfred/.local/state/robomate/hubs.json` | size 413, mtime_ns 1791466205781408761 | size 413, mtime_ns 1791466205781408761 | True | script |
| operator-token `/home/alfred/.local/state/robomate/operator-token` | size 44, mtime_ns 1791321420422913758 | size 44, mtime_ns 1791321420422913758 | True | script |

`robomate ls --json` in the caller's normal environment, run by the script while its hub was up, listed 1 hubs; the driver's hub listed: False.

## rpc_audit

| id | ts | actor | session | method | outcome | Performed by |
|---|---|---|---|---|---|---|
| 1 | 2026-10-08T14:56:15.983Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | initialize_workflow | ok | script |
| 2 | 2026-10-08T14:56:16.224Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | assign_task | ok | script |
| 3 | 2026-10-08T14:56:23.158Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | ask_user | ok | script |
| 4 | 2026-10-08T14:56:25.793Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | set_workflow_status | error -32002 | script |
| 5 | 2026-10-08T14:56:25.825Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | assign_task | error -32002 | script |
| 6 | 2026-10-08T14:56:25.885Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | check_merge_gate | error -32002 | script |
| 7 | 2026-10-08T14:56:25.944Z | — | — | hub.answer | error -32005 | script |
| 8 | 2026-10-08T14:56:27.526Z | operator | — | hub.answer | ok | script |
| 9 | 2026-10-08T14:56:27.760Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | log_decision | ok | script |
| 10 | 2026-10-08T14:56:27.788Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | set_workflow_status | ok | script |
| 11 | 2026-10-08T14:56:27.861Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | assign_task | ok | script |
| 12 | 2026-10-08T14:57:22.831Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | check_merge_gate | ok | script |
| 13 | 2026-10-08T14:57:28.161Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | log_decision | ok | script |
| 14 | 2026-10-08T14:57:28.188Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | set_workflow_status | ok | script |
| 15 | 2026-10-08T14:57:28.262Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | release_agent | ok | script |
| 16 | 2026-10-08T14:57:28.353Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | release_agent | ok | script |

A refused call made without the orchestrator headers, such as the worker's `hub.answer`, has no actor: the hub records only what the caller declared.

## decision

| id | ts | actor | session | summary | rationale | Performed by |
|---|---|---|---|---|---|---|
| 1 | 2026-10-08T14:56:23.146Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | Asked the operator question 1 | Workflow status set to escalated | script |
| 2 | 2026-10-08T14:56:27.747Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | Operator approved https://github.com/RoboNater/robo-agents-sandbox/pull/29 (question 1) | user_answered event 4: Approve: review and merge | script |
| 3 | 2026-10-08T14:56:27.777Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | Resumed after the operator answered question 1 | Workflow status set to active; operator answered questions 1 | script |
| 4 | 2026-10-08T14:57:28.147Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | Merged https://github.com/RoboNater/robo-agents-sandbox/pull/29 at d5b4224e79b9b5c77a51e68e4ee45af1a3186f4d | Gate passed; squash merge commit 77e8a753dc119bc1cc8bd32a89d38181f2f320a7 | script |
| 5 | 2026-10-08T14:57:28.177Z | script-alice | 631ac0bd-30fc-47df-85b6-0a2e0f985eea | Merged https://github.com/RoboNater/robo-agents-sandbox/pull/29 | Workflow status set to done | script |

## operator_question

| id | asked | actor | answered | answered_by | resumed_by | Performed by |
|---|---|---|---|---|---|---|
| 1 | 2026-10-08T14:56:23.146Z | script-alice | 2026-10-08T14:56:27.511Z | operator | 3 | script |

## Forge facts

| Fact | Value | Recorded by |
|---|---|---|
| base_sha | c473a91785694eba41fdb06716f4b6a68803aad1 | script |
| branch | operator-channel/20261008145609_6f56fda8 | script |
| head_sha | d5b4224e79b9b5c77a51e68e4ee45af1a3186f4d | script |
| issue_state | CLOSED | script |
| issue_url | https://github.com/RoboNater/robo-agents-sandbox/issues/28 | script |
| merge_sha | 77e8a753dc119bc1cc8bd32a89d38181f2f320a7 | script |
| merged_at | 2026-10-08T14:57:25Z | script |
| pr_url | https://github.com/RoboNater/robo-agents-sandbox/pull/29 | script |
| review_url | https://github.com/RoboNater/robo-agents-sandbox/pull/29#issuecomment-6062627641 | script |

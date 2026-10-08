**Scripted run: every action below was performed by `scripts/operator-channel-driver.py`, not by agents and not by the operator.** It played the orchestrator, both workers and the operator through the hub's real interfaces.

# Operator-channel validation, scripted run `20261008135408_a9c3c897`

- Result: **PASSED**
- Issue: robomate#133; step list: [operator-channel-validation.md](../development/operator-channel-validation.md)
- Sandbox: `RoboNater/robo-agents-sandbox`
- Started: 2026-10-08T13:54:08+00:00
- robomate commit: `d12a9c226d78aa12a1e81e465530c3a2a8545806`
- Actor names the script used: orchestrator `script-alice`, implementer `script-bob`, reviewer `script-charlie`; the hub records the operator channel as `operator`, and here the script used it too

## Steps

| # | Step | Performed by | Action | Hub facts | Forge facts | Result |
|---|---|---|---|---|---|---|
| 1 | Start a hub on a fresh clone of the sandbox repository | script, as operator | `gh repo clone` into a new temporary directory; `robomate up --port 46455` | hub `a1b39fc96db849dcacb44e99ddb18cf5` at http://127.0.0.1:46455; registered only in the throwaway registry: True | clone of `RoboNater/robo-agents-sandbox` at `692018c8d50d844875967af91841a5a712f9f07e` | pass |
| 2 | Start a workflow on a seeded sandbox issue | script, as orchestrator `script-alice` and workers | `gh issue create`; `initialize_workflow`; `script-bob` and `script-charlie` check in | workflow `0fc44742076447b88e3f444c57467a6f` active; agent_checked_in events [1, 2] | issue https://github.com/RoboNater/robo-agents-sandbox/issues/20 | pass |
| 3 | Assign an implementer task. The PR is opened | script, as orchestrator and implementer `script-bob` | `assign_task`; `await_assignment`, `get_role_guide` (5277 bytes), push, `gh pr create`, `submit_result` | task `e06577f46255410ab6948c99d2ea3c5b` completed; task_completed event 3 | https://github.com/RoboNater/robo-agents-sandbox/pull/21 at head `6d4613c11a466d909f957eb1b38f05f72546e2fc` | pass |
| 4 | The orchestrator calls ask_user and holds on wait_for_event | script, as orchestrator `script-alice` | `ask_user` with two options; `wait_for_event(timeout_s=100)` left holding | question 1; workflow escalated; `robomate inbox` lists [1] | — | pass |
| 5 | While escalated, set_workflow_status(active), assign_task and check_merge_gate are refused | script, as orchestrator `script-alice` | the three calls, made while the hold is pending | `set_workflow_status` -32002: workflow is escalated with open operator questions 1; wait for their user_answered events before changing its status; `assign_task` -32002: assign_task is refused while the workflow is escalated: operator questions 1 are open; `check_merge_gate` -32002: check_merge_gate is refused while the workflow is escalated: operator questions 1 are open | — | pass |
| 6 | A worker-side hub.answer with only the bearer token is refused | script, as worker `script-bob` | `hub.answer` on `/rpc` over the worker's client, without the operator credential | refused -32005: X-Robomate-Operator with the operator token is required; question 1 still unanswered | — | pass |
| 7 | The operator answers with robomate answer | script, as operator | `robomate answer 1 --option 1` with the run's temporary operator token | Answered question 1: Approve: review and merge | — | pass |
| 8 | The orchestrator receives user_answered and resumes; the reviewer confirms the decision with get_operator_answer and approves | script, as orchestrator `script-alice` and reviewer `script-charlie` | the held `wait_for_event` returns; `log_decision`; `set_workflow_status(active)`; `assign_task`; `get_role_guide` (5212 bytes); `get_operator_answer`; `gh pr comment`; `submit_result` approved | user_answered event 4 for question 1; decision 2; review task `307aa33297de4ff9be0cfd81b3d6de8f` completed (event 5) | review comment https://github.com/RoboNater/robo-agents-sandbox/pull/21#issuecomment-6061422069 at `6d4613c11a466d909f957eb1b38f05f72546e2fc` | pass |
| 9 | The gate passes at the approved head; the PR merges bound to it | script, as orchestrator `script-alice` | `check_merge_gate`; `gh pr merge --squash --match-head-commit 6d4613c11a466d909f957eb1b38f05f72546e2fc`; `set_workflow_status(done)`; `release_agent` for both workers | gate: head_matches True, ci pass (test=pass), mergeable clean, base_behind_main False | merged 2026-10-08T13:54:41Z as `b182f4d85e6046d881d6dd4dd420886d70d49f00`, head `6d4613c11a466d909f957eb1b38f05f72546e2fc`; issue #20 CLOSED | pass |
| 10 | Check the rpc_audit and decision rows: every change names its actor | script, as operator | read the hub database read-only | 16 rpc_audit rows, 5 decision rows, 1 operator_question rows; every accepted call and every decision names its actor (tables below) | — | pass |

## Isolated hub

| Fact | Value | Recorded by |
|---|---|---|
| Temporary directory | /home/alfred/lw/w535-robomate/w535r-run-log/lhi03-issue-133/tmp/bob/robomate-opchan-uh5vyd_1 | script |
| Clone | /home/alfred/lw/w535-robomate/w535r-run-log/lhi03-issue-133/tmp/bob/robomate-opchan-uh5vyd_1/robo-agents-sandbox | script |
| Hub state directory | /home/alfred/lw/w535-robomate/w535r-run-log/lhi03-issue-133/tmp/bob/robomate-opchan-uh5vyd_1/robo-agents-sandbox/.robomate | script |
| Port | 46455 | script |
| URL | http://127.0.0.1:46455 | script |
| hub_id | a1b39fc96db849dcacb44e99ddb18cf5 | script |
| pid | 641069 | script |
| Registry (throwaway) | /home/alfred/lw/w535-robomate/w535r-run-log/lhi03-issue-133/tmp/bob/robomate-opchan-uh5vyd_1/state/robomate/hubs.json | script |
| Registered in the throwaway registry | True | script |
| Deregistered on stop | True | script |
| Stopped | True | script |
| Stop | `robomate down` exited 0: Stopping hub at http://127.0.0.1:46455; hub process exited with code 0; http://127.0.0.1:46455/healthz no longer answers | script |

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
| 1 | 2026-10-08T13:54:13.900Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | initialize_workflow | ok | script |
| 2 | 2026-10-08T13:54:14.167Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | assign_task | ok | script |
| 3 | 2026-10-08T13:54:21.503Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | ask_user | ok | script |
| 4 | 2026-10-08T13:54:24.630Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | set_workflow_status | error -32002 | script |
| 5 | 2026-10-08T13:54:24.652Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | assign_task | error -32002 | script |
| 6 | 2026-10-08T13:54:24.715Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | check_merge_gate | error -32002 | script |
| 7 | 2026-10-08T13:54:24.778Z | — | — | hub.answer | error -32005 | script |
| 8 | 2026-10-08T13:54:26.497Z | operator | — | hub.answer | ok | script |
| 9 | 2026-10-08T13:54:26.802Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | log_decision | ok | script |
| 10 | 2026-10-08T13:54:26.841Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | set_workflow_status | ok | script |
| 11 | 2026-10-08T13:54:26.921Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | assign_task | ok | script |
| 12 | 2026-10-08T13:54:41.092Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | check_merge_gate | ok | script |
| 13 | 2026-10-08T13:54:45.842Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | log_decision | ok | script |
| 14 | 2026-10-08T13:54:45.877Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | set_workflow_status | ok | script |
| 15 | 2026-10-08T13:54:45.952Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | release_agent | ok | script |
| 16 | 2026-10-08T13:54:46.048Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | release_agent | ok | script |

A refused call made without the orchestrator headers, such as the worker's `hub.answer`, has no actor: the hub records only what the caller declared.

## decision

| id | ts | actor | session | summary | rationale | Performed by |
|---|---|---|---|---|---|---|
| 1 | 2026-10-08T13:54:21.488Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | Asked the operator question 1 | Workflow status set to escalated | script |
| 2 | 2026-10-08T13:54:26.784Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | Operator approved https://github.com/RoboNater/robo-agents-sandbox/pull/21 (question 1) | user_answered event 4: Approve: review and merge | script |
| 3 | 2026-10-08T13:54:26.822Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | Resumed after the operator answered question 1 | Workflow status set to active; operator answered questions 1 | script |
| 4 | 2026-10-08T13:54:45.823Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | Merged https://github.com/RoboNater/robo-agents-sandbox/pull/21 at 6d4613c11a466d909f957eb1b38f05f72546e2fc | Gate passed; squash merge commit b182f4d85e6046d881d6dd4dd420886d70d49f00 | script |
| 5 | 2026-10-08T13:54:45.860Z | script-alice | 1e3dd7b5-dbf4-4080-95dd-3e4705b31bda | Merged https://github.com/RoboNater/robo-agents-sandbox/pull/21 | Workflow status set to done | script |

## operator_question

| id | asked | actor | answered | answered_by | resumed_by | Performed by |
|---|---|---|---|---|---|---|
| 1 | 2026-10-08T13:54:21.488Z | script-alice | 2026-10-08T13:54:26.482Z | operator | 3 | script |

## Forge facts

| Fact | Value | Recorded by |
|---|---|---|
| base_sha | 692018c8d50d844875967af91841a5a712f9f07e | script |
| branch | operator-channel/20261008135408_a9c3c897 | script |
| head_sha | 6d4613c11a466d909f957eb1b38f05f72546e2fc | script |
| issue_state | CLOSED | script |
| issue_url | https://github.com/RoboNater/robo-agents-sandbox/issues/20 | script |
| merge_sha | b182f4d85e6046d881d6dd4dd420886d70d49f00 | script |
| merged_at | 2026-10-08T13:54:41Z | script |
| pr_url | https://github.com/RoboNater/robo-agents-sandbox/pull/21 | script |
| review_url | https://github.com/RoboNater/robo-agents-sandbox/pull/21#issuecomment-6061422069 | script |

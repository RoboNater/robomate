# Operator-channel validation

The end-to-end check of the operator channel (#128–#132): an orchestrator
escalates with `ask_user`, the hub refuses what must wait for the operator,
only the operator's credential can answer, and every change names its actor.

Two runs follow these steps unchanged:

- **Scripted** (#133): [`scripts/operator-channel-driver.py`](../../scripts/operator-channel-driver.py)
  plays the orchestrator, both workers and the operator. Its evidence states in
  its first line that the script performed every action.
- **Agents** (#134): real harnesses as the orchestrator and workers, a person as
  the operator.

The sandbox is the GitHub repository `RoboNater/robo-agents-sandbox` unless the
kickoff names another.

## Steps

1. **Start a hub on a fresh clone of the sandbox repository.**
2. **Start a workflow on a seeded sandbox issue.** The orchestrator calls
   `initialize_workflow` naming the issue; an implementer and a reviewer check
   in.
3. **Assign an implementer task. The PR is opened.** The implementer pushes a
   branch, opens a PR that closes the issue, and submits its result with the PR
   head.
4. **The orchestrator calls `ask_user` and holds on `wait_for_event`.** The
   workflow is `escalated`, and `robomate inbox` lists the question.
5. **While escalated, check that `set_workflow_status(active)`, `assign_task`
   and `check_merge_gate` are refused.** Each is refused with a conflict that
   names the open question; the hold is still pending.
6. **A worker-side `hub.answer` with only the bearer token is refused.** The
   refusal is `OPERATOR_REQUIRED`, and `get_operator_answer` still shows the
   question unanswered.
7. **The operator answers with `robomate answer`.**
8. **The orchestrator receives `user_answered`, resumes with
   `set_workflow_status(active)`, and assigns the review naming the question.
   The reviewer confirms the decision with `get_operator_answer` and
   approves.** The held `wait_for_event` returns the answer. The orchestrator
   logs its decision before resuming, and the resume's decision row names the
   answered question.
9. **The gate passes at the approved head; the PR merges bound to it.**
   `check_merge_gate` reads the reviewed head, green CI and a clean merge;
   the merge is `gh pr merge --squash --match-head-commit <approved head>`.
10. **Check the `rpc_audit` and `decision` rows: every change names its
    actor.** Every accepted `/rpc` call and every decision row has an actor; the
    refusals of steps 5 and 6 are there; the operator's answer is recorded as
    `operator` with no session.

**Where step 8 differs from #133's wording.** #133 lists "the orchestrator
resumes" in step 9, after the review. The hub refuses `assign_task` while the
workflow is escalated, even once every question is answered, until
`set_workflow_status(active)` (#132). A review assigned before `ask_user`
could not name the question it must confirm, so the resume opens step 8. This
is the orchestrator's escalation procedure: answer, `log_decision`,
`set_workflow_status(active)`, then act.

## The scripted run

```sh
uv run --locked python scripts/operator-channel-driver.py
```

It needs `gh` signed in with write access to the sandbox, and writes
`docs/evidence/operator-channel-scripted-<run-id>.md`. Each run seeds its own
issue and branch, so a rerun on a fresh clone repeats the same steps. It exits
0 only when all ten steps pass, its hub has stopped, and the machine state is
unchanged.

The driver's hub is an [isolated smoke hub](../../guides/worker.md) (#145):

- its clone, registry (`XDG_STATE_HOME`; Windows `LOCALAPPDATA`), operator token
  (`ROBOMATE_OPERATOR_TOKEN_FILE`) and hub state are all inside one new
  temporary directory, on a free port the OS assigns;
- inherited `ROBOMATE_*` and `HUB_*` variables are removed, it learns its hub's
  URL and token from `discover()` run in its clone, checks them against the hub
  it started, and gives every `robomate` command that URL and token explicitly;
- it stops its hub with `robomate down` on every exit path, including a failed
  step, and records the state directory, port and stop confirmation.

The machine's `hubs.json` and `operator-token` are compared before and after by
`os.stat` only; the driver never opens them.

`--operator-ls` also runs `robomate ls` in your normal environment while the
driver's hub is up, and checks that it is not listed. `robomate ls` calls every
hub in the machine registry, so this flag is for the operator's own run; a
worker running the driver leaves it off, and the check is then the operator's.
`tests/test_operator_channel_driver.py` covers it against a decoy registry.

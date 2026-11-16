# Idle waits investigation (#497)

Status: **proof-of-concept trials done; document not finished.** This part
covers item 3 of the
[rescoping comment](https://github.com/RoboNater/robomate/issues/497#issuecomment-6858579820)
(trials on each harness on the host) and the investigation needed to run
them. A regular robomate run finishes the rest. What that run still owes is
marked **TODO (run)** and collected under [Left for the run](#left-for-the-run).

The question: can an idle agent hold `await_assignment` / `wait_for_event`
for much longer than the 100 s the guides fix today, so that an idle wait
costs one model call instead of one every 100 s?

## Findings

Trials ran on Linux on 2026-11-16 against isolated smoke hubs, with a scratch
copy of the code and a separate config per harness
([Trial setup](#trial-setup)). Nothing on `main` changed.

1. **Every harness on the host completed a 30-minute hold** of both
   `await_assignment` and `wait_for_event` in the mode the launcher uses, once
   its own limit was raised, with one exception: Claude Code needs one more
   setting to go past 30 minutes (finding 3). The model call after the hold
   succeeded every time.
2. **Each harness has a different limit when nothing is set, and the shipped
   330 s settings fail a 600 s hold at 330 s:**

   | Harness (version) | Limit with no setting | Setting that raises it | Failure text |
   |---|---|---|---|
   | Claude Code 2.1.325, print mode (`-p`) | **1800 s** of silence | per-server `"timeout"` (ms) in the MCP entry, or `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0` | `sent no response or progress for 1800s; aborting` |
   | Claude Code 2.1.325, interactive | **moved to the background at 120 s** | `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS=0` | the turn ends without the result |
   | Codex CLI 0.169.0 | **300 s** | `tool_timeout_sec` | `timed out awaiting tools/call after 300s` |
   | OpenCode 1.19.5 | **60 s** | `mcp.robomate.timeout` (ms) | `MCP error -32001: Request timed out` |
   | Antigravity 1.5.9 | **180 s** | `timeoutSeconds` | `timed out after 3m0s: context deadline exceeded` |

3. **The 120 s Claude Code limit behind the 100 s rule applies to interactive
   sessions only.** In print mode, which `agent-launcher.py` uses, Claude Code
   held 100 s, 300 s, 600 s and 1800 s in the foreground with no setting. In an
   interactive session the same version moved a 300 s hold to the background
   at 120 s. Print mode's own limit is an idle timeout of exactly 1800 s. A
   scratch bridge that sent an MCP progress notification every 60 s held
   2000 s with Claude Code's defaults.
4. **On Codex a long hold only saves model calls if the wait is not polled.**
   Codex 0.169.0 with `gpt-6-luna` calls MCP tools from a scripted `exec` cell
   that hands control back to the model after `yield_time_ms` (default 30 s).
   Unprompted, the model sometimes kept the default: one 600 s hold cost 18
   extra model calls and one 1800 s hold cost 55 (about 2M input tokens). A
   one-line instruction in the prompt, or the config key
   `direct_only_tool_namespaces`, removed the polling
   ([Codex polling](#codex-polling)).
5. **Two hub-side limits apply to the orchestrator only, and neither is the
   300 s the issue assumed:**
   - `wait_for_event` accepts at most **120 s**, not 300.
   - The hub stops counting a `wait_for_event` hold after 120 s + 30 s
     whatever was requested. An orchestrator holding longer showed `idle` in
     `robomate status` from 150 s, and was **recorded as stalled
     (`no_hub_call`) at 1200 s** (`stall_after_min`, default 20). With that
     cap removed in a second scratch copy, a 30-minute hold showed `holding`
     throughout and no stall.
6. **Worker holds behaved as the issue expected.** For 30 and 60 minutes the
   worker stayed `alive`, its activity showed `holding: await_assignment`,
   and it was never stalled or lost.
7. **Omitting `timeout_s` does not make `HUB_DEFAULT_WAIT_S` the single
   setting.** The worker bridge fills in its own default, 100 s, unless
   `HUB_DEFAULT_WAIT_S` is set in the *bridge's* environment.
8. **A held wait ends at once** when an assignment, an event, a release or a
   closeout request arrives. `robomate down` mid-hold ends it in 2–5 s with a
   transport error, and the same bridge works again once the hub is back
   ([Interactions](#4-interactions)).

## 1. History: why 100 s

- **robo-agents#109 / PR robo-agents#111 (2026-09-21).** Workers called
  `await_assignment(timeout_s=120)`, as the guide then said. Claude Code moved
  any MCP tool call still running at 120 s to the background, so every empty
  wait was backgrounded: the worker had to end its turn and be woken by a task
  notification. The issue names bob's run on robo-agents#101 as the most
  recent of "several runs". The fix lowered the agent-facing defaults from
  120 s to 100 s. The implementer, a Claude Code worker, watched a 120 s wait
  go to the background and a 100 s wait return in the foreground in the same
  session ([`docs/poc-lessons.md`](../poc-lessons.md), "Lessons learned from
  PR #111").
- **robo-agents#112 → #12.** 100 was "a round number about 20% below 120";
  how close to 120 a wait can safely get was never measured. #12 asked for a
  test that every agent-facing default is at most 100 s, to be reshaped in M3
  into per-harness holds "each tested against its own harness limit". #12 is
  still open for that reshape.
- **#238 / PR #259 and #239 / PR #267 (M2 Steps 1–2).** These rewrote the
  worker guide and created `guides/orchestrator.md`, and carried the limit
  with them: `MAX_HOLD_DEFAULT_S = 100`
  (`packages/common/src/agent_hub_common/constants.py`) and the #12 test in
  `tests/test_worker_config.py`, which reads the worker default, the
  `wait_for_event` schema default and every `timeout_s=` in the three guides.
  PR #259 records the decision: "Preserved `MAX_HOLD_DEFAULT_S = 100.0`
  invariant until per-harness profiles land in M3." The PR text and review
  comments of both show no new wait failure; they kept the earlier rule.
- **Codex, PoC Step 4.** Codex CLI 0.154.0 failed a 330 s call with
  `timed out awaiting tools/call after 300s`, and a 150 s call completed.
  `tool_timeout_sec = 330` was added to cover the 300 s hub maximum plus the
  bridge's 15 s margin ([`docs/poc-spec.md`](../poc-spec.md) §8).
- **OpenCode and Antigravity (#49).** `timeout: 330000` and
  `timeoutSeconds: 330` were chosen to match
  ([`docs/harness-investigation-opencode-antigravity.md`](../harness-investigation-opencode-antigravity.md)).

So the only failure ever seen in a run with a longer wait is Claude Code's
backgrounding at 120 s. The 300 s Codex limit was found by probing.

**TODO (run):** read the run logs of #259 and #267 (not only the PR text) for
any wait incident, and any earlier robo-agents issue about wait timeouts.

## 2. Limits on a single hold, layer by layer

Line numbers are at `8ce2df5`.

### Guides and prompts

| Where | Value |
|---|---|
| `guides/orchestrator.md:35` | `wait_for_event(timeout_s=100)` |
| `guides/worker.md:18-20`, `prompts/worker.md:22` | `await_assignment(timeout_s=100)`; "Keep holds under 120 s: Claude Code backgrounds any tool call still running at 120 s." |
| `guides/worker.md:297-298`, `prompts/worker.md:301-302` | `ask_orchestrator` / `ask_alice` with `timeout_s=100` |
| `scripts/prepare-run.py` `prompt_sections` | Claude Code agents are told "Use a 100 second wait time for all hub waits" |
| `tests/test_worker_config.py` (#12) | asserts all of the above are `<= MAX_HOLD_DEFAULT_S` (100) |

### Worker path (`await_assignment`, `ask_orchestrator`)

| Layer | Limit | Where |
|---|---|---|
| MCP tool schema | `timeout_s` at most **300**; above it the bridge rejects the call with a validation error | `Timeout` in `packages/worker_mcp/src/worker_mcp/tools.py:24` |
| Bridge default when `timeout_s` is omitted | **100 s**, or `HUB_DEFAULT_WAIT_S` from the bridge's environment (trialled: unset held 100.0 s, `HUB_DEFAULT_WAIT_S=40` held 40.0 s) | `worker_mcp/config.py:22,131`; `client.py:1161` |
| Bridge → hub HTTP read timeout | hold + 15 s | `client.py` `_stream_rpc` |
| Hub clamp | `min(requested, max_wait_s)`; `max_wait_s` is `HUB_MAX_WAIT_S`, default 2.5 × `HUB_DEFAULT_WAIT_S` = **300 s** | `agent_hub_common/config.py:205-209,270-271`; `protocol.py:1083-1089` |
| Hub activity hold | requested timeout + `HOLD_GRACE_S` (30 s) | `activity.py:150-157,219` |

The hub's own default (`HUB_DEFAULT_WAIT_S`, 120 s) only reaches a caller that
talks to the hub without the bridge, because the bridge always sends a number.

### Orchestrator path (`wait_for_event`)

| Layer | Limit | Where |
|---|---|---|
| MCP tool schema | `timeout_s` at most **120**, default 100 | `Timeout` in `agent_hub_common/orchestrator_tools.py:17,124` |
| Bridge → hub HTTP read timeout | `timeout_s` + 15 s (75 s for every other call) | `worker_mcp/orchestrator.py:147` |
| Hub clamp | none: `store.wait_for_event` uses the value as given, and `HUB_MAX_WAIT_S` does not apply | `hub/orchestrator.py:154`, `store.py:3698` |
| Hub activity hold | `min(timeout_s, 120)` + `HOLD_GRACE_S` | `rpc.py` `_hold_s` (line 690) |

The issue body and its scope addition give the bridge bound as 300 for both
tools. For the orchestrator it is 120, in two places.

### Harness MCP tool timeouts

"Shipped" is what this repository writes or documents. "Default" is what the
harness does with no setting, measured in the trials.

| Harness | Default (measured) | Shipped value | Where shipped |
|---|---|---|---|
| Claude Code | print mode: aborts after 1800 s of silence; interactive: backgrounded at 120 s | none | `runtimes/claude-code.mcp.json`; static entry in [`docs/mcp-setup-prompt.md`](../mcp-setup-prompt.md) ("None") |
| Codex | 300 s | `tool_timeout_sec = 330` | `runtimes/codex.config.toml:11`; `scripts/run_common.py:383,513`; `scripts/prepare-run.py:3217`; static entry |
| OpenCode | 60 s | `"timeout": 330000` | `runtimes/opencode.json:31`; `run_common.render_opencode_config`; static entry |
| Antigravity | 180 s | `"timeoutSeconds": 330` | `runtimes/antigravity.mcp.json:38`; `run_common.render_antigravity_mcp`; static entry |
| Devin | not installed on the trial host | **TODO (operator)** | `run_common.render_devin_mcp` |

`scripts/check-mcp-tools.py` reads none of these timeouts.

Claude Code has four separate mechanisms. The descriptions come from strings
in the installed 2.1.325 binary; "trialled" marks what a trial confirmed.

- **Idle timeout.** A call with no response and no MCP progress notification
  for 1800 s is aborted (stdio server; trialled). The robomate bridge sends no
  progress notifications, so a hold is silent for its whole length.
  Three things each got a hold past it (trialled):
  `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0` on the `claude` process (2400 s); a
  per-server `"timeout": 7200000` in the MCP entry (2000 s); and progress
  notifications from the bridge (2000 s). Claude Code sent a `progressToken`
  with the call, so the bridge's notifications were addressed to it.
- **Wall-clock timeout.** `MCP_TOOL_TIMEOUT` (ms), or the per-server
  `"timeout"`, which wins. `MCP_TOOL_TIMEOUT=7200000` did not change the
  30-minute result, and unset did not stop a 30-minute hold, so the default is
  above 1800 s.
- **Auto-backgrounding.** The delay is `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`
  when set (0 disables; trialled), otherwise 120 000 ms behind a server-side
  feature flag. It is off in a non-interactive session unless
  `CLAUDE_AUTO_BACKGROUND_TASKS` is set (trialled: print mode did not
  background). Because the default sits behind a feature flag, it can change
  without a release.
- **Heartbeats in the event stream.** In print mode with
  `--output-format stream-json`, Claude Code emits a `tool_progress` record
  every 30 s while the call runs. These are the harness's own, not the
  bridge's.

### Harness turn, idle and provider limits

- No harness ended its turn, and no provider or connection failed, during or
  after a 30-minute hold: Anthropic (`claude-haiku-5-5`), OpenAI through a
  ChatGPT login (`gpt-6-luna`), opencode-go (`deepseek-v4.1-flash`) and
  Antigravity (`gpt-oss-120b`). One trial each, so this shows it can work, not
  that it always does.
- The model call after a 30-minute hold still read its prompt from cache on
  Claude Code, OpenCode and Codex (cache-read token counts matched the
  10-minute trials). Antigravity reported a smaller cache read after 30
  minutes than after 10.
- Codex's `exec` yield is a turn-level limit of its own; see
  [Codex polling](#codex-polling).
- The launcher has no idle watchdog on a harness's output
  (`scripts/agent_launcher.py`).

**TODO (run):** provider limits under contention (the opencode-go queue
errors in the issue body) were not reproduced here.

### Native Windows

Not trialled: the host is Linux. Nothing in the bridge or hub hold path is
platform-specific, but every harness default above was measured on Linux
only. **TODO (operator):** repeat one 300 s and one 30-minute hold per harness
on native Windows.

## 3. Proof-of-concept trials

### Trial setup

Everything lived under `scratch/` in a throwaway clone and was never
committed. No operator harness config was read or edited.

- **Scratch copy.** A clone at `8ce2df5` with two lines changed: the worker
  `Timeout` (`le=300`) and the orchestrator `Timeout` (`le=120`) both raised
  to `le=7200`. A second scratch copy also removed the 120 s cap in
  `rpc._hold_s`, for one check.
- **Smoke hubs.** `robomate up --port <p>` from the scratch clone and from
  linked worktrees of it, under the isolated smoke-run rules in
  [`guides/worker.md`](../../guides/worker.md): own `XDG_STATE_HOME`,
  `XDG_CONFIG_HOME`, `HUB_STATE_DIR` and `ROBOMATE_OPERATOR_TOKEN_FILE`,
  `ROBOMATE_*` unset, ports 8497–8505, and `HUB_MAX_WAIT_S=7200` in the hub's
  environment only. One hub served the worker trials. Each orchestrator trial
  had its own hub, because a hub has one orchestrator.
- **A live workflow on every hub**, started with
  `start_workflow(statement=..., policy={"max_wall_minutes": 600})`. Status
  and stall assessment are suppressed with no workflow.
- **Bridge.** Each harness ran `uv run --locked --project <scratch clone>
  robomate mcp --harness <h>` with `ROBOMATE_HUB_URL` and
  `ROBOMATE_TOKEN_FILE` in the entry's environment.
- **Status.** `robomate status --json` on every hub every 20 s, logged.
- **Baseline.** The same holds with no harness: a Python MCP stdio client
  driving the bridge.
- **Prompt.** Worker trials: join as an implementer, call
  `await_assignment(timeout_s=N)` once, report the exact result. Orchestrator
  trials: join, start the workflow, then `wait_for_event` at 100, 300, 600
  and 1800 s in one session, so they also show the session continuing after
  each hold.

Separate harness configs, and the commands used:

| Harness | Config isolation | Command |
|---|---|---|
| Claude Code (print) | `--strict-mcp-config --mcp-config <trial>/mcp.json` | `claude -p "<prompt>" --model claude-haiku-5-5 --strict-mcp-config --mcp-config mcp.json --allowedTools 'mcp__robomate__*' --output-format stream-json --verbose` |
| Claude Code (interactive) | same | the same without `-p` and the output flags, in a `tmux` session, with the prompt pasted in |
| Codex | `CODEX_HOME=<trial>/codex-home` with `auth.json` linked and its own `config.toml`: `[mcp_servers.robomate]`, `tool_timeout_sec`, and `approval_mode = "approve"` per tool | `codex exec --skip-git-repo-check -m gpt-6-luna -s read-only --json "<prompt>"` |
| OpenCode | `XDG_CONFIG_HOME=<trial>/xdg-config` holding `opencode/opencode.json`, so the global config is not read; the login in `XDG_DATA_HOME` is | `opencode run --auto --dir <trial>/work -m opencode-go/deepseek-v4.1-flash --format json --print-logs "<prompt>"` |
| Antigravity | `HOME=<trial>/home` with the three login files linked into `.gemini/antigravity-cli/` (as `run_common.agy_home` does) and its own `.gemini/config/mcp_config.json` | `agy --model gpt-oss-120b --dangerously-skip-permissions --output-format stream-json -p "<prompt>"` |

Environment variables for Claude Code were set on the `claude` process.

Models: as the operator specified, except Codex. The Codex account on the
host rejected `gpt-6.1-luna` ("not supported when using Codex with a ChatGPT
account"), so the Codex trials used `gpt-6-luna`.

Harness versions: Claude Code 2.1.325, codex-cli 0.169.0, OpenCode 1.19.5,
Antigravity (`agy`) 1.5.9. Devin is not installed.

Smoke-run report: nine hubs, started with `uv run --locked [--project
<scratch clone>] robomate up --port N` on ports 8497–8505, state under
`scratch/smoke/robomate-long.robomate/` and
`scratch/smoke2/robomate-holdfix.robomate/`. All were stopped with `robomate
down` in the same isolated environment, and nothing was left listening on
those ports. The machine's `hubs.json`, operator credential and harness
configs were not touched.

### Results

"ok" means the tool returned `{"timeout": true}` or `{"event": null}` after
the full hold, in the foreground, and the model then reported it. A dash
means not run.

**`await_assignment`**, one harness process per cell:

| Harness | Limit setting | 100 s | 300 s | 600 s | 1800 s | longer |
|---|---|---|---|---|---|---|
| Baseline (no harness) | — | ok | ok | ok | ok | 3600 s ok |
| Claude Code print | none | ok | ok | ok | ok | 2400 s **fail at 1800 s** |
| Claude Code print | `MCP_TOOL_TIMEOUT=7200000` | — | — | — | ok | — |
| Claude Code print | `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0` | — | — | — | — | 2400 s ok |
| Claude Code print | per-server `"timeout": 7200000` | — | — | — | — | 2000 s ok |
| Claude Code print | none; scratch bridge sends MCP progress every 60 s | — | — | — | — | 2000 s ok |
| Claude Code interactive | none | — | **backgrounded at 120 s** | — | — | — |
| Claude Code interactive | `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS=0` | — | ok | — | — | — |
| Codex | unset | — | — | **fail at 300 s** | — | — |
| Codex | `tool_timeout_sec = 330` (shipped) | ok | ok | **fail at 330 s** | — | — |
| Codex | `tool_timeout_sec = 7300` | — | — | ok | ok (polled) | — |
| OpenCode | unset | **fail at 60 s** | — | **fail at 60 s** | — | — |
| OpenCode | `timeout: 330000` (shipped) | ok | ok | **fail at 330 s** | — | — |
| OpenCode | `timeout: 7300000` | — | — | ok | ok | — |
| Antigravity | unset | ok | — | **fail at 180 s** | — | — |
| Antigravity | `timeoutSeconds: 330` (shipped) | ok | ok | **fail at 330 s** | — | — |
| Antigravity | `timeoutSeconds: 7300` | — | — | ok | ok | — |

**`wait_for_event`**, four holds in one session per harness, limit raised as
in the last row for each harness above (Claude Code: no setting):

| Harness | 100 s | 300 s | 600 s | 1800 s |
|---|---|---|---|---|
| Baseline | ok | ok | ok | ok (a 3600 s hold was stopped unfinished at teardown) |
| Claude Code print | ok | ok | ok | **fail at 1800 s** (idle timeout) |
| Codex | ok | ok | ok | ok |
| OpenCode | ok | ok | ok | ok |
| Antigravity | ok | ok | ok | ok |

How each failure looked, and on which side:

- **Client tool timeout (Codex, OpenCode, Antigravity).** The harness returned
  an error as the tool result, at the limit to the second. The turn went on
  and the model reported the error. The bridge and hub were unaffected: the
  hub-side hold ran on until its own timeout.
- **Claude Code idle timeout.** Same shape: an error tool result at 1800 s,
  and the turn went on. A 1800 s hold is a race: the worker trial returned
  just before the abort and the orchestrator trial just after.
- **Claude Code interactive backgrounding.** Not an error. At 120 s the tool
  result was "MCP tool "robomate/await_assignment" is still running after
  120s. It was moved to the background as task …". The model ended its turn.
  The real result arrived at 300 s as a notification, which woke the idle
  interactive session. In print mode a turn that ends this way exits the
  process (#115).
- No harness abort, provider timeout, connection timeout or reconnect was
  seen in any trial.

One unrelated flake: in the first Codex orchestrator launch, with five Codex
processes starting at once, the model found no `robomate` tools and stopped.
It did not recur in 15 later launches started 4 s apart.

### Codex polling

Codex 0.169.0 with `gpt-6-luna` did not call the MCP tool directly. It wrote a
script for its `exec` tool, for example
`const r = await tools.mcp__robomate__await_assignment({timeout_s:600})`.
`exec` returns "Script running with cell ID …" after `yield_time_ms`
(default 30 000), and the model then calls `wait` (default 10 000) until the
script ends. Each `wait` is a model call. The model can raise the yield with a
first-line pragma, `// @exec: {"yield_time_ms": N}`. With the plain trial
prompt it chose, across trials: 1 000, 30 000, 120 000, 300 000, 600 000 and
1 800 000 ms, or no pragma.

600 s hold, `tool_timeout_sec = 7300`, counted from the session rollout file:

| Variant | Model calls | Input tokens | `wait` polls |
|---|---|---|---|
| Plain prompt (model kept 30 s yields) | 22 | 683k | 18 |
| Prompt adds: "start the script with the pragma line `// @exec: {"yield_time_ms": 700000}`" | 4 | 88k | 0 |
| `[features.code_mode]` `direct_only_tool_namespaces = ["mcp__robomate"]` | 4 | 94k | 0 (plain `join` / `await_assignment` function calls) |
| `[features.code_mode]` `default_exec_yield_time_ms = 900000` | 5 | 120k | 0, but the model set its own 600 000 pragma, so the key's effect was not isolated |
| `[features.code_mode]` `excluded_tool_namespaces = ["mcp__robomate"]` | — | — | the model found no `robomate` tools at all |

Other observations:

- A 1 800 000 ms yield held 30 minutes with no poll, so there is no cap up to
  that value.
- The 1800 s worker trial, where the model kept the default, used 59 model
  calls and 1.99M input tokens (97% from cache).
- The three key names were found in the binary and accepted by
  `codex exec --strict-config`; they are not in the documentation I found.
  `codex features list` shows `code_mode` as "under development, false", yet
  the model was given `exec`.
- **Unknown:** whether `gpt-6.1-sol`, the model real runs use, calls MCP
  tools through `exec`. If it does, today's 100 s waits on Codex may already
  cost several model calls each. **TODO (run):** count `wait` calls in a
  Codex agent's rollout from a recent run.

## 4. Interactions

Observed in the trials; code references for the rest.

**Heartbeats and lost.** The bridge heartbeats every 30 s on its own timer
(`worker_mcp/client.py` `_heartbeat_loop`), independent of tool calls. Workers
holding for 30 and 60 minutes stayed `alive`. A worker went `lost` only
after its harness process exited (180 s later, `HUB_LOST_AFTER_S`).

**Worker status during a hold.** `robomate status` text shows only
`alive  task -`. The JSON `activity` shows `holding: "await_assignment"` and
`hold_age_s` for the whole hold, `stalled: false`.

**Orchestrator status during a hold.** With today's code: `waiting in
wait_for_event` / state `holding` until 150 s, then `idle` with `silence_s`
climbing while the call is still held. At 1200 s the hub opened a
`stall_episode` row (`role: orchestrator`, `reasons: ["no_hub_call"]`), and
closed it as `recovered` when the hold returned. This happened on all five
orchestrator hubs. `stall_action_after_min` was 0, so no operator question
was raised; with it set, a long hold would raise one. With `_hold_s`
uncapped, the same 30-minute hold showed `holding` throughout and no episode.

**Arrivals mid-hold.** Each ended a 600 s hold within 0.1 s of the trigger:

| Held call | Trigger | Returned |
|---|---|---|
| `await_assignment` | `assign_task` | the task |
| `await_assignment` | `release_agent` | `{"release": true}` |
| `await_assignment` | `request_closeout_reports` | `closeout_requested`, with the deadline |
| `wait_for_event` | a worker joined (`agent_checked_in`) | the event |

A hold waits on a signal only (`store._wait_for`): it re-reads state when
notified or when a delivery lease expires, and otherwise not until its
timeout. With 100 s waits, a state change that forgot to notify was picked up
within 100 s. With long waits it would be picked up that much later.
**TODO (run):** check every state change that should end a hold against the
`signals.notify` calls in `store.py` and `closeout.py`.

**Event leases.** A delivered event is leased for `HUB_EVENT_LEASE_S` (600 s)
and acknowledged by the next `wait_for_event(ack=...)`. The lease clock runs
while the orchestrator handles the event, not while she holds, so hold length
does not change it. Not exercised beyond the code reading. **TODO (run).**

**Closeout deadlines.** A closeout request wakes idle workers at once (above).
An orchestrator's own hold across a closeout deadline was not trialled.
**TODO (run).**

**`robomate down` during a hold.** Stopped 40 s into 600 s holds:

- `wait_for_event` failed after 2 s with `Server error '500 Internal Server
  Error' for url '…/rpc'`. The hub log shows the held request cancelled:
  `CancelledError: Task cancelled, timeout graceful shutdown exceeded`.
- `await_assignment` failed after 5 s with `All connection attempts failed`,
  after the bridge's three retries (0.5, 1, 2 s).
- Calls made while the hub was down failed the same way in 3.5 s.
- After `robomate up` on the same port, the same two bridges called again
  with no rejoin and held normally. Status then showed the orchestrator's
  last call as "before hub restart" until her next call.

The errors are prompt but not clear: neither says the hub stopped. The bridge
does not wait for a restarted hub, so an agent sees the error and must call
again. `robomate down` returned before the hub process had exited; an
immediate `robomate up` answered "hub already running" and exited.

## 5. Inputs for the recommendation

Section 5 of the rescoping comment (what a change needs, unknowns, proposed
issues) is **TODO (run)**. What the trials say it must cover:

| Layer | Needed for holds up to the hub maximum (300 s) | Needed beyond |
|---|---|---|
| Guides, `prepare-run.py` prompt text, #12 test | new value or omitted `timeout_s`; the "under 120 s" sentences no longer hold for print mode | same |
| Worker bridge default | `HUB_DEFAULT_WAIT_S` in the bridge's environment, or a new default; omitting `timeout_s` alone gives 100 s | same |
| Worker `Timeout` bound | none (already 300) | raise or derive from settings |
| Orchestrator `Timeout` bound | raise from 120 | raise |
| `rpc._hold_s` cap | raise from 120, or status shows `idle` | raise, or a stall is recorded at `stall_after_min` |
| `HUB_MAX_WAIT_S` | none | raise per hub |
| Claude Code, print mode | none | below 1800 s: none. At or above, one of: per-server `"timeout"` in the MCP entry, `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`, or bridge progress notifications |
| Claude Code, interactive | `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS=0` in the session's environment | same |
| Codex | shipped 330 is enough | `tool_timeout_sec`; and stop the `exec` polling by instruction or `direct_only_tool_namespaces` |
| OpenCode | shipped 330000 is enough | `timeout` |
| Antigravity | shipped 330 is enough | `timeoutSeconds` |

Open questions the trials raise:

- Where do the Claude Code settings go? The per-server `"timeout"` fits the
  static entry. `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` does not: it belongs to
  the `claude` process, so the launcher's environment can carry it, and an
  interactive session started by hand has to set it itself.
- A per-harness maximum, or one value under every harness's limit? Claude
  Code print mode's 1800 s idle timeout is the lowest raised ceiling seen.
- Should the bridge send MCP progress notifications during a hold? It reset
  Claude Code's idle timeout with no harness setting. The scratch patch
  covered `await_assignment` only. Its effect on the other three harnesses'
  timeouts, and on interactive backgrounding, was not trialled.
- Codex: an instruction in the served guide, a launcher prompt line, or the
  undocumented config key?

## Left for the run

- Section 1: run logs of #259 and #267; earlier wait-timeout issues.
- Section 2: provider limits under contention; Devin.
- Section 3: reviewer spot-check of one trial.
- Section 4: notify audit; event leases; orchestrator hold across a closeout
  deadline.
- Section 5: all of it.
- For the operator: native Windows trials; Devin trials; one long-hold trial
  on `gpt-6.1-sol` under Codex.

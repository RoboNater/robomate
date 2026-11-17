# Idle waits investigation (#497)

Status: **investigation complete, with evidence gaps stated below.** This
document answers the five questions in the
[rescoping comment](https://github.com/RoboNater/robomate/issues/497#issuecomment-6858579820)
and the [remaining-work direction](https://github.com/RoboNater/robomate/issues/497#issuecomment-6932162903).
It preserves the completed trials and adds a code audit and recommendations;
it changes no production wait behavior. Missing historical logs and the
operator-only measurements are recorded under [Evidence limits](#evidence-limits).

The question: can an idle agent hold `await_assignment` / `wait_for_event`
for much longer than the 100 s the guides fix today, so that an idle wait
costs one model call instead of one every 100 s?

## Findings

Trials ran on Linux on 2026-11-16 against isolated smoke hubs, with a scratch
copy of the code and a separate config per harness
([Trial setup](#trial-setup)). Nothing on `main` changed. The scripts are in
[`scripts/development/idle-waits-investigation/`](../../scripts/development/idle-waits-investigation/README.md).

1. **Codex, OpenCode and Antigravity completed 30-minute holds** of both
   `await_assignment` and `wait_for_event` with their client limits raised.
   The worker hold was run three times on each. Claude Code print mode held
   1500 s twice and completed one 1800 s worker hold, but its 1800 s
   orchestrator hold raced the idle timeout and failed. With a setting or
   bridge notifications it held beyond 1800 s (finding 3). Models continued
   after the successful holds.
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
4. **On Codex a long hold only saves model calls if the wait is not polled,
   and today's waits are polled.** Codex 0.169.0 calls MCP tools from a
   scripted `exec` cell that hands control back to the model after
   `yield_time_ms` (default 30 s); the model then polls. In the ten Codex
   worker sessions from real runs on 12–16 November, 45% of all model calls
   were hub waits or polls of them. In the trials, one 600 s hold cost 18
   extra model calls and one 1800 s hold cost 55 (about 2M input tokens). A
   one-line instruction in the prompt removed the polling
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
8. **MCP progress notifications from the bridge are not a general fix.** They
   reset Claude Code's print-mode idle timeout and OpenCode's timeout, but not
   Codex's or Antigravity's, and they do not stop interactive Claude Code
   backgrounding the call ([Progress notifications](#progress-notifications)).
9. **A held wait ends at once** when an assignment, an event, a release or a
   closeout request arrives. `robomate down` mid-hold ends it in 2–5 s with a
   transport error, and the same bridge works again once the hub is back
   ([Interactions](#4-interactions)).
10. **Long holds expose missing wakeups.** Closeout deadline expiry does not
    end an inbox hold. Loss and concurrent result submission do not wake a
    held task-reply wait; instance replacement also needs in-flight stream
    fencing. The [wakeup audit](#wakeup-audit) separates confirmed scratch
    probes from code-only findings. These need attention before raising waits.

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

The historical sources establish a live Claude Code backgrounding failure,
not a universal 120 s timeout. [robo-agents#109](https://github.com/RoboNater/robo-agents/issues/109)
names the affected #101 run, and the [PR #111 live check](https://github.com/RoboNater/robo-agents/pull/111)
records the background task ID `k7ri9h1na` for 120 s and a foreground timeout
at 100 s in the same session. [robo-agents#112](https://github.com/RoboNater/robo-agents/issues/112)
explicitly says the margin was unmeasured. Searching the earlier repository's
issue titles for `wait` and `timeout`, and reading the PoC spec and lessons,
found no additional earlier MCP hold-timeout incident. Codex's 300 s timeout
was a probe result, as the PoC spec §8 records, rather than a reported run
failure. This search does not prove that no other incident occurred.

**#259/#267 log availability.** The archived run logs could not be read:
Alice checked the run archive on this host and reported the older lanes
deleted; the temporary smoke directories in the PR comments are gone too.
Issues #238/#239 and PRs [#259](https://github.com/RoboNater/robomate/pull/259)
and [#267](https://github.com/RoboNater/robomate/pull/267) expose no raw-log
links. Their bodies, review comments and git changes were checked instead.
#259 explicitly preserves the 100 s invariant. Its `join_via_mcp` 60 s smoke
timeout came from launching a module that exited without running the CLI,
not from a held MCP call. #267's failures concern prepared-session takeover,
tool allowlists and cp1252 output; none establishes a new hold-time limit.
Thus there is no new wait incident in the available summaries, but the
missing transcripts prevent confirming that for the full runs.

## 2. Limits on a single hold, layer by layer

All production code/config line numbers in this document refer to current
`main` at [`8ce2df5e812ba85514985e4e0d142f5214c2881d`](https://github.com/RoboNater/robomate/tree/8ce2df5e812ba85514985e4e0d142f5214c2881d).
The investigation branch adds only this document and scratch trial tooling.

### Guides and prompts

| Where | Value |
|---|---|
| `guides/orchestrator.md:35` | `wait_for_event(timeout_s=100)` |
| `guides/worker.md:18-20`, `prompts/worker.md:22` | `await_assignment(timeout_s=100)`; "Keep holds under 120 s: Claude Code backgrounds any tool call still running at 120 s." |
| `guides/worker.md:297-298`, `prompts/worker.md:301-302` | `ask_orchestrator` / `ask_alice` with `timeout_s=100` |
| `scripts/prepare-run.py:731-785` `prompt_sections` | Claude Code and Devin agents are told "Use a 100 second wait time for all hub waits" |
| `tests/test_worker_config.py:195-261` (#12) | pins the worker/schema defaults and literals in the three guides at `<= MAX_HOLD_DEFAULT_S` (100); also requires literals to exist. It does not inspect `prompt_sections` |

### Worker path (`await_assignment`, `ask_orchestrator`)

| Layer | Limit | Where |
|---|---|---|
| MCP tool schema | `timeout_s` at most **300**; above it the bridge rejects the call with a validation error | `Timeout` in `packages/worker_mcp/src/worker_mcp/tools.py:24` |
| Bridge default when `timeout_s` is omitted | **100 s**, or `HUB_DEFAULT_WAIT_S` from the bridge's environment (trialled: unset held 100.0 s, `HUB_DEFAULT_WAIT_S=40` held 40.0 s) | `worker_mcp/config.py:22,131`; `client.py:1161` |
| Bridge → hub HTTP read timeout | hold + 15 s | `client.py:377-404` `_stream_rpc` |
| Hub clamp | `min(requested, max_wait_s)`; `max_wait_s` is `HUB_MAX_WAIT_S`, default 2.5 × `HUB_DEFAULT_WAIT_S` = **300 s** | `agent_hub_common/config.py:205-209,270-271`; `protocol.py:1083-1089` |
| Hub activity hold | `min(requested, HUB_MAX_WAIT_S)` + `HOLD_GRACE_S` (30 s) | `protocol.py:941,976,1027,1083-1089`; `activity.py:150-157,219-220` |

The hub's own default (`HUB_DEFAULT_WAIT_S`, 120 s) only reaches a caller that
talks to the hub without the bridge, because the bridge always sends a number.

### Orchestrator path (`wait_for_event`)

| Layer | Limit | Where |
|---|---|---|
| MCP tool schema | `timeout_s` at most **120**, default 100 | `Timeout` in `agent_hub_common/orchestrator_tools.py:17,124` |
| Bridge → hub HTTP read timeout | `timeout_s` + 15 s (75 s for every other call) | `worker_mcp/orchestrator.py:147` |
| Hub clamp | none: `store.wait_for_event` uses the value as given, and `HUB_MAX_WAIT_S` does not apply | `agent_hub/orchestrator.py:153-157`, `store.py:3698-3713` |
| Hub activity hold | `min(timeout_s, 120)` + `HOLD_GRACE_S` | `rpc.py:691-703` `_hold_s`; `activity.py:219-220` |

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
| Devin | not installed on the trial host; timeout unmeasured | renderer supplies no timeout; prepared prompt says 100 s | `run_common.py:589-595`; `prepare-run.py:781-785` |

`scripts/check-mcp-tools.py:50-147` checks bridge discovery/tool parity, not
these timeout values (its own subprocess timeouts are unrelated).

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

Provider limits under contention (the opencode-go queue errors in the issue
body) were not reproduced here. That is an operator-only item.

### Native Windows

Not trialled: the host is Linux. Nothing in the bridge or hub hold path is
platform-specific, but every harness default above was measured on Linux
only. Repeating one 300 s and one 30-minute hold per harness on native
Windows is an operator-only item.

## 3. Proof-of-concept trials

### Trial setup

Everything the trials created lived under `scratch/` in a throwaway clone and
was never committed; only the scripts are. No operator harness config was
edited, and no trial read one. Outside the trials, the operator's Codex
config header, model list and session files were read (for the model name
and the real-run counts).

- **Scratch copy.** A clone at `8ce2df5` with two lines changed: the worker
  `Timeout` (`le=300`) and the orchestrator `Timeout` (`le=120`) both raised
  to `le=7200`. A second scratch copy also removed the 120 s cap in
  `rpc._hold_s`, for one check.
- **Smoke hubs.** `robomate up --port <p>` from the scratch clone and from
  linked worktrees of it, under the isolated smoke-run rules in
  [`guides/worker.md`](../../guides/worker.md): own `XDG_STATE_HOME`,
  `XDG_CONFIG_HOME`, `HUB_STATE_DIR` and `ROBOMATE_OPERATOR_TOKEN_FILE`,
  `ROBOMATE_*` unset, ports 8497–8507, and `HUB_MAX_WAIT_S=7200` in the hub's
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
account"), so the Codex trials used `gpt-6-luna`, plus three on `gpt-6.1-sol`
with the operator's approval.

Harness versions: Claude Code 2.1.325, codex-cli 0.169.0, OpenCode 1.19.5,
Antigravity (`agy`) 1.5.9. Devin is not installed.

Smoke-run report: eleven hubs, started with `uv run --locked [--project
<scratch clone>] robomate up --port N` on ports 8497–8507, state beside three
scratch clones under `scratch/` (`smoke/`, `smoke2/`, `smoke3/`). All were stopped with `robomate
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
| Claude Code print | none | ok | ok | ok | ok; 1500 s ok ×2 | 2400 s **fail at 1800 s** |
| Claude Code print | `MCP_TOOL_TIMEOUT=7200000` | — | — | — | ok | — |
| Claude Code print | `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0` | — | — | — | — | 2400 s ok |
| Claude Code print | per-server `"timeout": 7200000` | — | — | — | — | 2000 s ok |
| Claude Code print | none; scratch bridge sends MCP progress every 60 s | — | — | — | — | 2000 s ok |
| Claude Code interactive | none | — | **backgrounded at 120 s** | — | — | — |
| Claude Code interactive | `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS=0` | — | ok | — | — | — |
| Codex | unset | — | — | **fail at 300 s** | — | — |
| Codex | `tool_timeout_sec = 330` (shipped) | ok | ok | **fail at 330 s** | — | — |
| Codex | `tool_timeout_sec = 7300` | — | — | ok | ok ×3 (first one polled) | — |
| OpenCode | unset | **fail at 60 s** | — | **fail at 60 s** | — | — |
| OpenCode | `timeout: 330000` (shipped) | ok | ok | **fail at 330 s** | — | — |
| OpenCode | `timeout: 7300000` | — | — | ok | ok ×3 | — |
| Antigravity | unset | ok | — | **fail at 180 s** | — | — |
| Antigravity | `timeoutSeconds: 330` (shipped) | ok | ok | **fail at 330 s** | — | — |
| Antigravity | `timeoutSeconds: 7300` | — | — | ok | ok ×3 | — |

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

### Progress notifications

A scratch patch made the bridge send an MCP progress notification every 60 s
while `await_assignment` is held. Every harness passed a `progressToken` with
the call, so every one received them.

| Harness | Limit setting | Hold | Without notifications | With notifications |
|---|---|---|---|---|
| Claude Code print | none | 2000 s | fail at 1800 s (2400 s trial) | **ok** |
| Claude Code interactive | none | 300 s | backgrounded at 120 s | backgrounded at 120 s |
| Codex | unset | 600 s | fail at 300 s | fail at 300 s |
| Codex | `tool_timeout_sec = 330` | 600 s | fail at 330 s | fail at 330 s |
| OpenCode | unset (60 s) | 300 s | fail at 60 s | fail at 60 s |
| OpenCode | `timeout: 330000` | 600 s | fail at 330 s | **ok** |
| Antigravity | unset | 600 s | fail at 180 s | fail at 180 s |
| Antigravity | `timeoutSeconds: 330` | 600 s | fail at 330 s | fail at 330 s |

So notifications reset the timeout on Claude Code print mode and OpenCode
only. The OpenCode failure with nothing set is a tie: a 60 s interval against
a 60 s timeout. A shorter interval was not tried.

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

**Real runs.** The ten Codex worker session files from robomate runs on 12–16
November (`gpt-6.1-terra` and `gpt-6.1-sol`, in `~/.codex/sessions`), counted
with `codex_rollout.py`:

- 4,938 model calls in all. 900 were hub waits made through `exec`, and 1,298
  were `wait` polls of those: 2,198, or 45%.
- Every hub wait kept the default 30 s yield, except five at 1 000 ms. No hub
  wait was a direct call.
- The workers asked for `timeout_s` of 60 (434 times) or 50 (382), and 100
  only 46 times, although the guide says 100. Nearly all polls used a 20 s
  yield.
- The worst session (t3002-i450, `gpt-6.1-terra`) spent 999 of 1,532 model
  calls on hub waits and their polls.

**`gpt-6.1-sol` in the trials.** Three trials (100 s; 600 s plain; 600 s with
the pragma instruction). It used `exec` each time and set a long yield on its
own (120 000 and 650 000 ms), so nothing was polled. The trial prompt says
"wait in the foreground … however long it takes", which the served guide does
not, so the plain trial does not show what it does in a run; the real-run
counts above do.

Other observations:

- A 1 800 000 ms yield held 30 minutes with no poll, and 1 900 000 ms twice
  more, so there is no cap up to that value.
- The 1800 s worker trial, where the model kept the default, used 59 model
  calls and 1.99M input tokens (97% from cache).
- The three key names were found in the binary and accepted by
  `codex exec --strict-config`; they are not in the documentation I found.
  `codex features list` shows `code_mode` as "under development, false", yet
  the model was given `exec`.
- The pragma instruction was followed in all six trials that carried it
  (`gpt-6-luna` and `gpt-6.1-sol`). Whether a guide sentence is followed as
  reliably over a long run is not shown.

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
orchestrator hubs. No operator question was raised. Setting
`stall_action_after_min` would not escalate this orchestrator episode either:
`store.py:4547-4550` selects only **worker** episodes for automatic action.
With `_hold_s` uncapped, the same 30-minute hold showed `holding` throughout
and no episode. The false orchestrator diagnosis still matters even without
automatic escalation.

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

### Wakeup audit

The three predicates are assignment/release/held-task change/closeout request
(`store.py:3571-3675`), reply or terminal task (`3677-3696`), and an eligible
event (`3698-3713`). `_wait_for` subscribes **before** reading, clears the
flag before each read, and reads again on timeout (`3542-3569`);
`signals.py:63-98` makes cross-thread notifications safe. A write must use
the same store's `Signals`. The audit follows every writer of those predicates;
an arbitrary DB update is not a supported wakeup path.

| State change | Notification after commit (main line numbers) | Held call affected |
|---|---|---|
| Assignment | `store.py:2805`, context key | NEXT claims submitted task immediately |
| Explicit release; run done with `on_done: release` and no queued run | `2588`; `1813-1816`, context keys | NEXT returns release |
| Question opened; valid reply | `2955-2956`, event + context; `3034-3035`, task + context | Inbox sees question; repeated NEXT sees input-required/reply; reply hold returns answer |
| Manual task fail/cancel; A2A task cancel | `2195-2196`; `3267-3269`, task + context | Reply hold ends; repeated NEXT rechecks ended task |
| Result submission | `3247-3248`, event + context | Inbox sees result; NEXT rechecks. **No task-key notification** for an already-held reply wait |
| Worker loss, including failure of its open task | `3836-3842`, event + context; terminalization at `3889-3902` | Inbox sees loss; NEXT rechecks. **No task-key notification** for an already-held reply wait |
| Check-in/rejoin/takeover | `2453`, event key | Inbox sees agent and changed membership. No context/task wake for a superseded in-flight worker hold; replacement's own NEXT reads immediately |
| Progress, operator answer, explicit appended event | `2906`, `2105`, `3406`, event key | Inbox returns event |
| New submitted workflow; activation of queued run | `1149`; `1811`, event key | Inbox rechecks; submission queues `work_submitted` when `emit_work_submitted` is set; activation alone adds no new event |
| Host-pause detection; lease expiry/loss sweep; emitted worker-stall/action events | `3805`; `3836`; `4533-4534`, event key | Inbox returns the diagnostic event; an orchestrator stall episode alone intentionally emits no event |
| Event leases explicitly expired (takeover/session cleanup) | `3511-3512`, event key when any row changed | Inbox can redeliver immediately |
| First closeout request; accepted closeout report | `closeout.py:256-258`, worker context keys; `357`, event key | Idle NEXT receives request; inbox receives report. Idempotent retries introduce no new state |
| Closeout window expires, pauses/shifts, finalizes; membership loss/release/rejoin | `closeout.py:114-141,408-471` and store lifecycle callers | No dedicated deadline event or signal; loss/rejoin already notify inbox, release notifies NEXT, done releases workers as above |

Heartbeat renewal, detach, ack, decisions, escalation/withdrawal and pause
alone do not create an assignment, a reply or an inbox event. Their absence
of notification does not hide a newly queued event. Task-lease expiry emits
`lease_expired` but does not terminalize the task (`store.py:3845-3872`);
the worker continues until Alice acts. Pauses and clock-jump handling shift
task/closeout deadlines; they must not be replaced with fixed wall timers.

Two gaps matter before lengthening holds. `await_reply` checks *all*
terminal states, but result submission and the loss sweep do not notify its
task key. A concurrent result or a lost bridge with a still-open HTTP hold
therefore waits until its timeout to report `task_ended`. Likewise,
check-in replaces a worker instance without waking an older hold on the
same context; its poll looks up the worker by name, not by original instance.
Review cancellation/fencing of these in-flight streams before extending
their lifetime (`protocol.py:974-1020,1134-1138` checks the instance on entry,
not after the assignment wait). These are audit findings, not claims that
the normal serial worker loop performs concurrent calls.

**Event leases.** `HUB_EVENT_LEASE_S` defaults to 600 s
(`agent_hub_common/config.py:192,319`). `wait_for_event(ack=...)` acks the
previous delivery **before** opening the hold (`store.py:3706-3707`), so a
properly acknowledged event is not left leased throughout that idle wait.
Handling an event for more than 600 s can still produce a late ack warning;
the latest delivery ID is accepted even after expiry (`3409-3447`). Raising
the hold duration is not a reason by itself to raise the lease duration.

If an event is unacked, `lease_next_event` can redeliver it after expiry,
minting a new ID and invalidating the old ack (`3449-3488`). The inbox wait
has a dynamic wake time for the earliest delivered-event expiry
(`3529-3540,3712`), even with no signal. Thus a 1800 s hold missing its ack
can return an old event at 600 s. Session takeover explicitly expires leases
and notifies, without touching task leases. Host-resume grace postpones
redelivery (`3461-3474,3540`). These conclusions are from code and the
existing store tests, not a new long-harness trial.

**Closeout deadlines.** First request wakes idle workers promptly; open
work takes precedence (`store.py:3643-3664`). Reports wake Alice's inbox,
and loss does too. But expiry is lazy: `_expire` is invoked by report
submission/read, worker `pending`, or finalization (`closeout.py:128-141,
315,375,421,445`), not by a timer in the sweeper. No expiry event is queued.
`wait_for_event` schedules only event-delivery expiry, not collection expiry.
If one live worker never reports and no other event arrives, a long inbox
hold can run past the collection deadline until the requested timeout; only
the subsequent `get_closeout_reports` shows `deadline_expired`.

An isolated in-process scratch probe confirmed this at main: with a frozen
300 s collection window and an inbox hold subscribed, advancing both clocks
by 301 s, refreshing the worker heartbeat, sweeping and reading reports
produced `deadline_expired` with **no notification**; the inbox remained
held. Appending a control event released it immediately. Two further probes
confirmed that loss and concurrent result submission terminalize a task
without waking its held reply; notifying the task key then released each.
These used temporary databases only, no harnesses or listening hubs.

Before extending inbox waits, either bound each closeout wait by the
remaining, pause-aware collection window and re-read the reports, or add a
hub timer outcome/event that actually ends the call. Merely adding a
`next_timeout` poll to `_wait_for` is insufficient: its event predicate would
still find no event and go back to sleep. Re-read after a pause/resume or
host-pause deadline shift. Progress notifications alone cannot fix this.

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

## 5. Recommendation and proposed work

Use **per-harness profiles**, including the harness mode, rather than a common
hold below the lowest client limit. Roll out Codex's polling instruction first;
then extend actual holds only after the activity, deadline and wakeup paths
above are covered. The existing trial results justify this direction, not an
unconditional guarantee for all versions, providers or operating systems.

### Proposed hold profiles

These are initial implementation targets for a follow-up, not settings shipped
by this PR. All apply to NEXT, question waits and the orchestrator inbox, with
a **shorter wait when a workflow deadline requires it**.

| Harness/mode | Proposed idle hold | Timeout/config requirement and evidence |
|---|---|---|
| Claude Code print | **1500 s** | Twice completed with no setting; leaves 300 s below the observed 1800 s silent idle abort. Do not use 1800 s, which raced and failed on the orchestrator |
| Claude Code interactive | **100 s** by default | Retain foreground behavior below 120 s. Offer the 1500 s profile only after auto-backgrounding is explicitly disabled and that mode is validated; disabling it was measured at 300 s only |
| Codex | **1800 s**, with the polling instruction | Three worker holds and an orchestrator hold completed with `tool_timeout_sec=7300`. Ship a matching per-server timeout with at least hold + 15 s HTTP margin and scheduling margin (propose 1900 s; validate that exact setting) |
| OpenCode | **1800 s** | Three worker holds and an orchestrator hold completed with `timeout=7300000`; propose 1900000 ms and validate it, or retain the larger tested entry during rollout |
| Antigravity | **1800 s** | Three worker holds and an orchestrator hold completed with `timeoutSeconds=7300`; propose 1900 s and validate it, or retain the larger tested entry during rollout |
| Devin | **retain existing 100 s guidance pending measurement** | No measured timeout or long-hold success; do not infer support from the other harnesses |

For existing static entries still at 330 s, keep the current behavior until
the operator updates them; a future capability/config check should report
that the long profile is unavailable. The bridge cannot infer a harness's
actual per-server timeout from the harness name alone. With today's shipped
entries, 300 s worker holds fit Codex/OpenCode/Antigravity, but orchestrator
holds still cannot exceed 120 s. There is no settings-only long-wait rollout.

Progress notifications could later let Claude Code print use an 1800 s or
longer profile without changing its idle setting, and let OpenCode retain a
shorter client idle timeout. Make this optional bridge work: the 2000 s Claude
Code trial and 600 s OpenCode trial establish value, but not reliable 3600 s
holds or recovery after missed notifications. They do not raise Codex's or
Antigravity's absolute call limit or stop interactive backgrounding. Retain
finite client/HTTP timeouts so a dead bridge still fails visibly.

### Changes needed at each layer

| Layer and current-main citation | Required work |
|---|---|
| Guide/default regression contract: `agent_hub_common/constants.py:20-24`, `tests/test_worker_config.py:195-261`; related [#12](https://github.com/RoboNater/robomate/issues/12) | Replace the single 100 s invariant with tested per-harness/mode defaults. Cover all served and prepared prompt instructions and tool schema defaults. Omitting literals currently fails the test, so this needs an explicit redesign |
| Shared guides and prepared/resume prompts: `guides/orchestrator.md:35,245-255,701-716`, `guides/worker.md:18-20,119-125,297-298`, `prompts/worker.md:22-24,123-129,301-302`, `scripts/prepare-run.py:731-799,1374-1387` | Replace unconditional 100/120 s claims with the effective profile, distinguish print from interactive Claude Code, preserve foreground/keep-turn-alive requirements, and teach deadline-aware waits. Keep worker guide/prompt copies aligned; check skill pointers for duplicated instructions |
| Worker schema/default/HTTP: `worker_mcp/tools.py:24,160-180,231-260`, `config.py:22,128-132,190`, `client.py:377-404,1161,1302` | Raise the finite schema bound to accommodate approved profiles; resolve an effective timeout for omitted arguments for both NEXT and question waits. A variable set only on the hub cannot change the bridge's 100 s fallback. Preserve hold + 15 s HTTP budget and bounded retry behavior |
| Orchestrator schema/HTTP/hub dispatch: `agent_hub_common/orchestrator_tools.py:17,123-124`, `worker_mcp/orchestrator.py:147`, `agent_hub/orchestrator.py:153-157` | Raise its separate 120 s bound; choose the omitted-argument default from the same effective profile instead of another hardcoded 100. Add an explicit hub cap if desired; today's worker `HUB_MAX_WAIT_S` does not constrain this path |
| Hub settings, status and liveness: `agent_hub_common/config.py:187-209,270-274`, `protocol.py:1083-1089`, `rpc.py:691-703`, `activity.py:49-54,150-164,193-220`, `store.py:4207-4294,4360-4416` | Set a hub ceiling that accommodates the profiles (initially 1800 s), clamp the effective request consistently, and account for that full legitimate hold plus grace. Keep 30 s heartbeats, 180 s loss detection, `IDLE_AFTER_S=120`, and stall policy independent of hold length; raising stall/lost thresholds would conceal the bug |
| Wakeups, leases and closeout: Section 4, `store.py:3542-3713`, `closeout.py:128-141,370-471` | Test every outcome path, notify terminal reply waiters, fence superseded streams, and actually end or bound an inbox hold at a closeout deadline. Preserve dynamic event-lease wakeups, late ack semantics and pause/host-resume rules |
| MCP entries and setup: `runtimes/codex.config.toml:11`, `runtimes/opencode.json:31`, `runtimes/antigravity.mcp.json:38`, `run_common.py:372-388,478-595`, `prepare-run.py:3217`, `docs/mcp-setup-prompt.md:38-49`, `scripts/check-mcp-tools.py:50-147` | Write and document corresponding client timeouts in static setup and generated configs, in seconds or milliseconds as required. Add timeout validation to the checker; passing tool discovery today says nothing about long-hold safety. Claude process variables belong in the launcher/session environment, not merely the MCP server's environment |
| Launcher/recovery: `scripts/agent_launcher.py`, `scripts/prepare-run.py:731-799,1374-1387` | Carry the same profile/mode and Codex note across launch and resume. Keep transport errors visible and permit a subsequent call after restart; do not end a headless worker's turn to wait |

Prefer one resolved profile communicated by the bridge/join response to
repeated independent literals in guides, hub config and launch templates.
The hub has agent harness identity, but no mode-aware timeout negotiation
exists today. Unknown harness/mode and old entries need a conservative
fallback. Decide and document precedence for explicit `timeout_s`, operator
overrides, profile defaults and the hub maximum; validate unsupported values
before holding. A static MCP server may serve different hubs, so a global
user entry alone cannot determine a per-run hub policy.

### Codex instruction placement

`guide_response` takes role and forge only
([`guides.py:53-84`](https://github.com/RoboNater/robomate/blob/8ce2df5e812ba85514985e4e0d142f5214c2881d/packages/hub/src/agent_hub/guides.py#L53-L84));
there is no harness-specific composition. This still allows an inline
conditional paragraph in the shared served guide, as the worker guide's
existing Codex retained-session recipe demonstrates (`guides/worker.md:161-182`).
The options are:

1. **Recommended first:** put an "On Codex" paragraph in the shared worker and
   orchestrator guides. With the currently supported profile, give an explicit
   example of an `exec` first-line pragma whose `yield_time_ms` exceeds the
   effective hold plus transport margin; for a proposed 1800 s hold, 1900000
   ms is a measured yield value. Forward the eventual result, and if the cell
   still yields, call `wait` on that cell with a correspondingly long yield.
   Give the same advice for question waits. Fetching the guides through join
   covers static as well as prepared runs without adding a guide API dimension.
2. Add a harness appendix to guide composition, or a structured join-response
   wait profile plus harness instructions. This offers tailored text but needs
   route/join changes, hash/cache semantics, fallback rules and tests. The
   current guide API cannot do it simply by adding a new Markdown file.
3. If shared conditional text is unsuitable, append the Codex sentence in
   `prepare-run.py:731-799` and the launch/resume prompt path. This is the
   fallback the owner requested, but alone it misses hand-started static
   sessions; their setup instructions must carry it too.

Do not ship the undocumented `[features.code_mode]` keys explored in the
trials. The prompt instruction removed polling in six short trials; its
long-run adherence remains to be measured. The real-run 50–60 s holds also
need explanation: a longer hub maximum cannot help a model that keeps
requesting short holds or polling every 20–30 s. A future guide change must
reconcile any harness-level limits on foreground tool yields as well; a guide
cannot override a higher-priority harness instruction.

### Proposed issues and order

These are proposals for the owner to choose and open, not newly filed issues
or an implementation added to this PR.

1. **Codex: remove model polling during hub holds.** Start with the conditional
   served-guide paragraph and matching prepared/resume guidance. Keep current
   wait bounds for this first change. Acceptance: inspect real worker and
   orchestrator session files for `exec`/`wait` counts, verify an arriving task
   returns promptly, and compare empty waits/polls per model call and input
   tokens with the ten-session 45% baseline. A 100 s hold should require one
   model tool invocation, not several polls. Measure sustained adherence.
2. **Extend [#12](https://github.com/RoboNater/robomate/issues/12): per-harness
   hold profiles and matching client settings.** Implement the profile table
   and layer changes together with the activity cap and wakeup/deadline work;
   alternatively make the latter a prerequisite issue. Tests must keep Alice
   holding for the full supported duration, keep workers alive, deliver
   assignments/replies/release/closeout promptly, cover every terminal path
   and instance replacement, wake on unacked lease expiry, accept late acks,
   and end closeout waiting by the pause-aware deadline. Exercise hub restart
   mid-hold and continued use of the same bridge. Use frozen clocks for hub
   deadlines, plus one real hold per supported harness/mode on the final
   settings. Update static-entry checking, fixtures and generated/resume
   configs together. Do not mistake #386's collection window for an MCP hold.
3. **Optional bridge progress notifications for silent holds.** Cover NEXT,
   question and orchestrator holds, using the caller's `progressToken`, and
   stop on return/cancel/error. Test periodic delivery and task cleanup,
   absent tokens, a disconnected caller and stalled hub; heartbeat and
   progress remain separate. Measure Claude Code print and OpenCode with
   intervals comfortably shorter than their configured idle timeout. Keep
   explicit client timeout settings for Codex/Antigravity and test that their
   limits still behave as expected. Use this only if longer profiles or less
   operator configuration justify the added lifecycle work.
4. **Evaluate [#501](https://github.com/RoboNater/robomate/issues/501), park/wake,
   against the measured long-hold result.** Long holds already preserve
   heartbeat and reuse a process; Codex polling may dominate the avoidable
   work. Compare model calls, tokens/cache expiry, process cost, crash recovery
   and event-to-work latency after proposals 1–2. Park/wake still needs durable
   event ownership, resume/takeover and safe cancellation, and cannot be
   implemented by telling a headless worker to end its turn today.

### Evidence limits

- Raw #259/#267 run logs are unavailable; Section 1 gives the available
  history and its limits rather than inferring an unseen transcript.
- The Linux harness versions, models and trial counts in Section 3 are the
  measurements. This run did not repeat those trials. Review still includes
  the requested spot-check with the committed scripts (300 s is sufficient).
- **Operator-only; this run neither attempts nor waits for them:** native
  Windows holds, Devin (not installed), and provider behavior under contention.
  Until measured, do not assume the Linux limits apply to them.
- The exact recommended 1900 s client settings, Claude interactive holds
  beyond 300 s with backgrounding disabled, guide adherence in long sessions,
  and progress-notification reliability beyond the measured cases remain
  follow-up acceptance work. The bridge cannot read the parent harness's
  effective timeout or feature flags today.
- The closeout/terminal-reply scratch probes establish code-path behavior;
  they do not simulate network cancellation or an end-to-end 30-minute run.
  The superseded-stream finding is from code reading. Reconnect error wording,
  `down`/immediate-`up` shutdown timing, and busy provider response latency
  remain separate from proving that an idle hold can complete.

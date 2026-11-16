# Idle waits investigation (#497)

Status: **work in progress.** This first part covers the proof-of-concept
trials (item 3 of the [rescoping comment](https://github.com/RoboNater/robomate/issues/497#issuecomment-6858579820))
and the investigation needed to run them. The remaining items are finished in
a regular robomate run; sections that run still owes are marked **TODO (run)**.

The question: can an idle agent hold `await_assignment` / `wait_for_event`
for much longer than the 100 s the guides fix today, so that an idle wait
costs one model call instead of one every 100 s?

## Summary of findings so far

Trials ran on Linux on 2026-11-16 against isolated smoke hubs (see
[Trial setup](#trial-setup)).

- **Every harness on the host completed a 30-minute hold** in its headless
  (launcher) mode, once its own MCP tool timeout was raised. See
  [Results](#results).
- **Each harness's default MCP tool timeout is different, and three of the four
  are below 30 minutes:**

  | Harness (version) | Limit with no setting | Setting | Failure when exceeded |
  |---|---|---|---|
  | Claude Code 2.1.325, print mode (`-p`) | none seen up to 40 min (TODO: fill from trials) | `MCP_TOOL_TIMEOUT` (ms), per-server `timeout` (ms), `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` (ms) | — |
  | Claude Code 2.1.325, interactive | call **moved to the background at 120 s** | `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS=0` disables it | turn ends without the result |
  | Codex CLI 0.169.0 | **300 s** | `tool_timeout_sec` | `timed out awaiting tools/call after 300s` |
  | OpenCode 1.19.5 | **60 s** | `mcp.<name>.timeout` (ms) | `MCP error -32001: Request timed out` |
  | Antigravity 1.5.9 | **180 s** | `timeoutSeconds` | `timed out after 3m0s: context deadline exceeded` |

- **The 120 s Claude Code limit behind the 100 s rule is an interactive-mode
  behaviour.** In print mode, which `agent-launcher.py` uses, Claude Code
  2.1.325 held 100 s, 300 s and 600 s in the foreground with no setting at
  all. In an interactive session the same version moved a 300 s hold to the
  background at exactly 120 s, and held it in the foreground with
  `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS=0`.
- **Two hub-side limits apply to the orchestrator only, and neither is the
  300 s the issue assumed:**
  - `wait_for_event` accepts at most **120 s** (`Timeout`, `le=120`), not 300.
  - The hub stops counting a `wait_for_event` hold after **120 s + 30 s**
    whatever was requested (`rpc._hold_s`). In the trials an orchestrator
    holding longer showed `idle` from 150 s on, and (TODO: confirm from the
    30-minute trials) is reported stalled once `stall_after_min` passes.
- **Worker holds behaved as the issue expected:** the bridge heartbeats on its
  own timer, the worker stayed `alive` and its hold was tracked for its full
  length.
- A wait returns at once when an assignment or event arrives mid-hold, and a
  hub stop mid-hold ends the hold within seconds with an error. See
  [Interactions](#interactions-observed-in-the-trials).

## 1. History: why 100 s

- **robo-agents#109 / PR robo-agents#111 (2026-09-21).** Workers called
  `await_assignment(timeout_s=120)`, as the guide then said. Claude Code moved
  any MCP tool call still running at 120 s to the background, so every empty
  wait was backgrounded: the worker had to end its turn and be woken by a task
  notification. The issue names bob's run on robo-agents#101 as the most
  recent of "several runs". The fix lowered the agent-facing defaults from
  120 s to 100 s. The implementer, a Claude Code worker, watched a 120 s wait
  go to the background and a 100 s wait return in the foreground in the same
  session ([`docs/poc-lessons.md`](../poc-lessons.md), "Lessons learned from PR #111").
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
  invariant until per-harness profiles land in M3." Neither PR saw a new
  failure; they kept the earlier rule.
- **Codex, PoC Step 4.** Codex CLI 0.154.0 failed a 330 s call with
  `timed out awaiting tools/call after 300s`, and a 150 s call completed.
  `tool_timeout_sec = 330` was added to cover the 300 s hub maximum plus the
  bridge's 15 s margin ([`docs/poc-spec.md`](../poc-spec.md) §8).
- **OpenCode and Antigravity (#49).** `timeout: 330000` and
  `timeoutSeconds: 330` were chosen to match
  ([`docs/harness-investigation-opencode-antigravity.md`](../harness-investigation-opencode-antigravity.md)).

So the only failure ever observed with a longer wait is Claude Code's
backgrounding at 120 s. The 300 s Codex limit was found by probing, not in a run.

**TODO (run):** check the run logs and review comments of #259 and #267 for
any wait-related incident not recorded in the PR text.

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
| Bridge default when `timeout_s` is omitted | **100 s**, or `HUB_DEFAULT_WAIT_S` from the *bridge's* environment | `worker_mcp/config.py:22,131`; `client.py:1161` |
| Bridge → hub HTTP read timeout | hold + 15 s | `client.py` `_stream_rpc` |
| Hub clamp | `min(requested, max_wait_s)`; `max_wait_s` = `HUB_MAX_WAIT_S`, default 2.5 × `HUB_DEFAULT_WAIT_S` = **300 s** | `agent_hub_common/config.py:205-209,270-271`; `protocol.py:1083-1089` |
| Hub activity hold | requested timeout + `HOLD_GRACE_S` (30 s) | `activity.py:150-157,219` |

Omitting `timeout_s` does **not** make the hub's `HUB_DEFAULT_WAIT_S` the
single setting, as the issue body hoped. The bridge fills in its own default
(100 s) and always sends a number. The hub's default (120 s) only reaches a
caller that talks to the hub without the bridge.

### Orchestrator path (`wait_for_event`)

| Layer | Limit | Where |
|---|---|---|
| MCP tool schema | `timeout_s` at most **120**, default 100 | `Timeout` in `agent_hub_common/orchestrator_tools.py:17,124` |
| Bridge → hub HTTP read timeout | `timeout_s` + 15 s (75 s for every other call) | `worker_mcp/orchestrator.py:147` |
| Hub clamp | none: `store.wait_for_event` uses the value as given. `HUB_MAX_WAIT_S` does not apply | `hub/orchestrator.py:154`, `store.py:3698` |
| Hub activity hold | `min(timeout_s, 120)` + `HOLD_GRACE_S` | `rpc.py` `_hold_s` (line 690) |

The issue body and its scope addition list the bridge bound as 300 for both
tools. For the orchestrator it is 120, in two places.

### Harness MCP tool timeouts

"Shipped" is what this repository writes or documents; "default" is what the
harness does with no setting, measured in the trials below.

| Harness | Default (measured) | Shipped value | Where shipped |
|---|---|---|---|
| Claude Code | print mode: no limit reached (TODO); interactive: backgrounded at 120 s | none | `runtimes/claude-code.mcp.json`, the static entry in [`docs/mcp-setup-prompt.md`](../mcp-setup-prompt.md) ("None") |
| Codex | 300 s | `tool_timeout_sec = 330` | `runtimes/codex.config.toml:11`; `scripts/run_common.py:383,513`; `scripts/prepare-run.py:3217`; static entry (`docs/mcp-setup-prompt.md`) |
| OpenCode | 60 s | `"timeout": 330000` | `runtimes/opencode.json:31`; `run_common.render_opencode_config`; static entry |
| Antigravity | 180 s | `"timeoutSeconds": 330` | `runtimes/antigravity.mcp.json:38`; `run_common.render_antigravity_mcp`; static entry |
| Devin | not installed on the trial host | **TODO (run / operator)** | `run_common.render_devin_mcp` |

`scripts/check-mcp-tools.py` does not read any of these timeouts.

Claude Code details, read from the installed 2.1.325 binary and confirmed by
trial where marked:

- `MCP_TOOL_TIMEOUT` (ms) and a per-server `"timeout"` (ms) in the MCP entry
  are a hard wall-clock limit per call. The per-server value wins; values
  below 1000 ms are ignored.
- `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` (ms, 0 disables) aborts a call with "no
  response or progress notification for N s". The robomate bridge sends no
  MCP progress notifications, so a hold is silent for its whole length.
  (TODO: default and whether a 30–40 min hold reaches it, from the trials.)
- Auto-backgrounding: the delay is `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` when
  set (0 disables), otherwise 120 000 ms behind a server-side feature flag.
  It is **off in a non-interactive session** unless
  `CLAUDE_AUTO_BACKGROUND_TASKS` is set. Confirmed by trial in both modes.
  Because the default sits behind a feature flag, it can change without a
  release.

### Harness turn, idle and provider limits

**TODO:** fill from the 30-minute trials (whether the first model call after a
long hold succeeded on each harness and provider).

### Native Windows

Not trialled: the host is Linux. **TODO (run / operator):** repeat at least one
300 s and one 30-minute hold per harness on native Windows. Nothing in the
bridge or hub hold path is platform-specific, but the harness defaults above
were measured on Linux only.

## 3. Proof-of-concept trials

### Trial setup

Everything lives under `scratch/` in a throwaway clone and was never
committed. Nothing on `main` was changed, and no operator harness config was
edited.

- **Scratch copy.** A clone of this repository at `8ce2df5` with two lines
  changed: the worker `Timeout` (`le=300`) and the orchestrator `Timeout`
  (`le=120`) both raised to `le=7200`.
- **Smoke hubs.** `robomate up --port <p>` from that clone and from linked
  worktrees of it, per the isolated smoke-run rules in
  [`guides/worker.md`](../../guides/worker.md): own `XDG_STATE_HOME`,
  `XDG_CONFIG_HOME`, `HUB_STATE_DIR` and `ROBOMATE_OPERATOR_TOKEN_FILE`,
  `ROBOMATE_*` unset, ports 8497–8504, and `HUB_MAX_WAIT_S=7200` in the hub's
  environment only. One hub served all worker trials; each orchestrator trial
  had its own hub, because a hub has one orchestrator.
- **A live workflow on every hub**, started with
  `start_workflow(statement=..., policy={"max_wall_minutes": 600})`, so that
  status and stall assessment were active (they are suppressed with no
  workflow).
- **Bridge.** Each harness ran `uv run --locked --project <scratch clone>
  robomate mcp --harness <h>` with `ROBOMATE_HUB_URL` and
  `ROBOMATE_TOKEN_FILE` in the entry's environment.
- **Status.** `robomate status --json` on every hub every 20 s, logged.
- **Baseline.** The same holds driven with no harness, through the bridge
  from a Python MCP stdio client.
- **Prompt.** Worker trials: join as an implementer, call
  `await_assignment(timeout_s=N)` once, report the exact result. Orchestrator
  trials: join, start the workflow, then `wait_for_event` at 100, 300, 600
  and 1800 s in one session. So each orchestrator trial also shows the
  session continuing after each hold.

Separate harness configs, and the commands:

| Harness | Config isolation | Command |
|---|---|---|
| Claude Code (print) | `--strict-mcp-config --mcp-config <trial>/mcp.json` | `claude -p "<prompt>" --model claude-haiku-5-5 --strict-mcp-config --mcp-config mcp.json --allowedTools 'mcp__robomate__*' --output-format stream-json --verbose` |
| Claude Code (interactive) | same | the same without `-p`, in a `tmux` session, prompt pasted in |
| Codex | `CODEX_HOME=<trial>/codex-home` with `auth.json` linked and its own `config.toml` (`[mcp_servers.robomate]`, `tool_timeout_sec`, per-tool `approval_mode = "approve"`) | `codex exec --skip-git-repo-check -m gpt-6-luna -s read-only --json "<prompt>"` |
| OpenCode | `XDG_CONFIG_HOME=<trial>/xdg-config` holding `opencode/opencode.json`, so the global config is not read; the login in `XDG_DATA_HOME` is | `opencode run --auto --dir <trial>/work -m opencode-go/deepseek-v4.1-flash --format json --print-logs "<prompt>"` |
| Antigravity | `HOME=<trial>/home` with the three login files linked into `.gemini/antigravity-cli/` (as `run_common.agy_home` does) and its own `.gemini/config/mcp_config.json` | `agy --model gpt-oss-120b --dangerously-skip-permissions --output-format stream-json -p "<prompt>"` |

Model note: the Codex account on the host rejected `gpt-6.1-luna` ("not
supported when using Codex with a ChatGPT account"), so the Codex trials used
`gpt-6-luna`.

Harness versions: Claude Code 2.1.325, codex-cli 0.169.0, OpenCode 1.19.5,
Antigravity (`agy`) 1.5.9. Devin is not installed.

### Results

**TODO: final tables; the 30-minute trials are still running at this commit.**

Results in so far (`await_assignment` unless noted; "ok" means the tool
returned `{"timeout": true}` or `{"event": null}` after the full hold, in the
foreground, and the model then reported it):

| Harness | Tool timeout setting | 100 s | 300 s | 600 s | 1800 s |
|---|---|---|---|---|---|
| Baseline (no harness) | — | ok | ok | ok | running |
| Claude Code print | none | ok | ok | ok | running |
| Claude Code interactive | none | — | **backgrounded at 120 s** | — | — |
| Claude Code interactive | `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS=0` | — | ok | — | — |
| Codex | unset | — | — | **fail at 300 s** | — |
| Codex | `tool_timeout_sec = 330` (shipped) | ok | ok | **fail at 330 s** | — |
| Codex | `tool_timeout_sec = 7300` | — | — | ok | running |
| OpenCode | unset | **fail at 60 s** | — | **fail at 60 s** | — |
| OpenCode | `timeout: 330000` (shipped) | ok | ok | **fail at 330 s** | — |
| OpenCode | `timeout: 7300000` | — | — | running | running |
| Antigravity | unset | ok | — | **fail at 180 s** | — |
| Antigravity | `timeoutSeconds: 330` (shipped) | ok | ok | **fail at 330 s** | — |
| Antigravity | `timeoutSeconds: 7300` | — | — | running | running |

### Interactions observed in the trials

**TODO: fill from the interaction run and the status logs.**

## 4. Interactions

**TODO (run):** heartbeats and the lost/idle/holding states, event leases,
orchestrator stall episodes, closeout deadlines, hub restart or `robomate
down` during a hold. The trial observations above are the starting point.

## 5. What a change would need, unknowns, recommendation

**TODO (run).**

# Idle-wait hold trials (#497)

The scripts behind [`docs/development/idle-waits-investigation.md`](../../../docs/development/idle-waits-investigation.md).
They hold `await_assignment` / `wait_for_event` for a chosen time on each agent
harness, against isolated smoke hubs, and record what happens.

They are investigation tooling: not in CI beyond `ruff`, and not maintained
once #497 and its follow-ups close. The first trial rounds were run with
earlier copies of these scripts kept in a scratch directory; this set is the
same logic tidied up, and was used as committed for the last rounds (the
progress-notification trials).

Everything they create lives in one scratch directory. They never read or
write an operator's harness config, the machine's `hubs.json` or a run's hub.
The scratch clone's commit is never pushed.

## Requirements

Linux or macOS shell, `uv`, `git`, `tmux` (interactive Claude Code trial
only), and whichever harness CLIs you trial, logged in: `claude`, `codex`,
`opencode`, `agy`.

## Set up

```sh
D=scripts/development/idle-waits-investigation
$D/setup-smoke.sh /path/to/scratch            # worker bound 300 -> 7200, orchestrator bound 120 -> 7200
export IDLE_WAITS_SCRATCH=/path/to/scratch
$D/start-hub.sh hub-w 8503                    # one hub for worker trials
$D/start-hub.sh hub-cc 8499                   # one hub per orchestrator trial
setsid nohup $D/status-poll.sh $IDLE_WAITS_SCRATCH >/dev/null 2>&1 &
START_WORKFLOW=1 $D/baseline.sh hub-w 8503 alice orchestrator wait_for_event 5
```

- `setup-smoke.sh SCRATCH [PATCH...]` clones this checkout into
  `SCRATCH/robomate-long`, applies `patches/raise-timeout-bounds.patch` and any
  extra patch named, and writes `SCRATCH/env.sh` with the isolated-smoke
  environment from [`guides/worker.md`](../../../guides/worker.md)
  (`XDG_STATE_HOME`, `XDG_CONFIG_HOME`, `HUB_STATE_DIR`,
  `ROBOMATE_OPERATOR_TOKEN_FILE`, `ROBOMATE_*` unset) plus
  `HUB_MAX_WAIT_S=7200`.
- The patches apply at `8ce2df5`. On a later `main` they may need redoing by
  hand; each is a few lines.
- A hub needs a live workflow, or status and stall assessment are suppressed.
  The last line above starts one. Without an orchestrator trial on that hub,
  nothing else is needed.
- Choose free ports. Stop everything with `$D/stop-hubs.sh` when done.

Extra patches:

| Patch | Effect |
|---|---|
| `uncap-orchestrator-hold.patch` | removes the 120 s cap in `rpc._hold_s`, so a long `wait_for_event` stays `holding` |
| `bridge-progress-notifications.patch` | `await_assignment` sends an MCP progress notification every 60 s, and logs each to `SCRATCH_PROGRESS_LOG` |

## Run a trial

Each runner takes a trial name, the hub's directory name and port, then either
`worker NAME T` (one `await_assignment(timeout_s=T)`) or `orch NAME T...`
(`wait_for_event` at each T in one session). Output goes to
`$IDLE_WAITS_SCRATCH/trials/<trial>/`: `prompt.txt`, the harness config,
`out.jsonl` (the harness event stream), `err.txt` and `timing.txt`.

```sh
$D/run-claude-code.sh cc-w600 hub-w 8503 worker cc-w600 600
CC_ENV="CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0" $D/run-claude-code.sh cc-w2400 hub-w 8503 worker cc-w2400 2400
CC_SERVER_EXTRA=', "timeout": 7200000' $D/run-claude-code.sh cc-w2000 hub-w 8503 worker cc-w2000 2000
CC_ENV="CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS=0" $D/run-claude-code-tui.sh cctui-w300 hub-w 8503 420 worker cctui-w300 300
$D/run-codex.sh codex-w600 hub-w 8503 7300 worker codex-w600 600        # 4th argument: tool_timeout_sec, or "default"
$D/run-opencode.sh oc-w600 hub-w 8503 7300000 worker oc-w600 600        # timeout in ms, or "default"
$D/run-antigravity.sh agy-w600 hub-w 8503 7300 worker agy-w600 600      # timeoutSeconds, or "default"
$D/run-claude-code.sh cc-orch hub-cc 8499 orch alice 100 300 600 1800
```

A runner blocks for the whole hold. Start long ones with
`setsid nohup … >/dev/null 2>&1 &`, a few seconds apart.

Models default to the low-cost ones used in the investigation. Override with
`CC_MODEL`, `CODEX_MODEL`, `OC_MODEL`, `AGY_MODEL`.

Codex options:

```sh
CODEX_PROMPT_EXTRA='Harness note for Codex: if you call the tool from an exec script, start the script with the pragma line // @exec: {"yield_time_ms": 700000} so the script does not yield back to you before the wait returns.' \
  $D/run-codex.sh codex-prompt hub-w 8503 7300 worker codex-prompt 600
CODEX_EXTRA_TOML=$'[features.code_mode]\ndirect_only_tool_namespaces = ["mcp__robomate"]' \
  $D/run-codex.sh codex-direct hub-w 8503 7300 worker codex-direct 600
```

## Read the results

```sh
python3 $D/summarize_trials.py $IDLE_WAITS_SCRATCH/trials/*      # one line per wait: elapsed, outcome
python3 $D/status_summary.py $IDLE_WAITS_SCRATCH/logs/status.jsonl hub-w
python3 $D/codex_rollout.py $(find $IDLE_WAITS_SCRATCH/trials/codex-* -name 'rollout*.jsonl')
python3 $D/codex_rollout.py ~/.codex/sessions/2026/11/*/rollout*.jsonl   # real runs
```

`summarize_trials.py` cannot show elapsed time for Codex (its event stream
has none); use `timing.txt`. `codex_rollout.py` counts the `wait` polls Codex
made around each hub wait.

## Baseline and interaction checks

These drive the bridge with no harness and need `ROBOMATE_HUB_URL` and
`ROBOMATE_TOKEN_FILE` for a smoke hub, and the scratch environment:

```sh
. $IDLE_WAITS_SCRATCH/env.sh
export ROBOMATE_HUB_URL=http://127.0.0.1:8504
export ROBOMATE_TOKEN_FILE=$SMOKE/robomate-long.robomate/hub-x/token/token
run() { uv run --locked --project $SMOKE_CLONE python "$@"; }
run $D/omitted_timeout.py                 # what an omitted timeout_s holds for
run $D/interact_arrivals.py               # assignment, event, release, closeout arriving mid-hold
run $D/interact_hub_down.py hub-x 8504    # robomate down mid-hold, then up again
```

`baseline.sh HUB PORT NAME ROLE TOOL T...` wraps `baseline_hold.py` for plain
holds.

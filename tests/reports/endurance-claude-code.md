# Claude Code endurance report

- Date: 2026-09-12
- Result: PASS
- Harness/version: Claude Code 2.1.270
- Provider/model: Anthropic / `claude-sonnet-5`
- Worker profile: `bob`, heartbeat interval 30 seconds
- Harness elapsed time: 1,800.042 seconds
- Claude process duration: 1,805.314 seconds
- Completed assignment cycles: 3

## Observations

The final timed `scripts/mock-alice.py --endurance` scenario ran under
`scripts/supervise-claude-code.sh` without a human message after launch. One
Claude process and one worker-MCP instance remained alive for the entire run.
Claude completed all three tasks, returned to `await_assignment` after each
result, and consumed Alice's final release response.

- `await_assignment` returned `timeout` 69 times, including the deliberately
  delayed first assignment, and the worker retried.
- `ask_alice` returned `timeout` once; the worker repeated the identical
  question and consumed Alice's reply.
- Claude Code blocks long standalone sleeps. Cycle 2 therefore ran one
  background `sleep 210.0` and immediately made one blocking
  `TaskOutput(timeout=240000)` call. Alice measured 220.713 seconds between
  assignment and result, exceeding `HUB_LOST_AFTER_S=180`.
- Eight accepted heartbeat records occurred while cycle 2 was active, and the
  heartbeat timestamp advanced throughout the long-work interval. Direct
  analysis of consecutive worker tool-call timestamps found a 225.183-second
  no-tool gap, with all eight heartbeats covering it and a maximum
  31.836-second heartbeat gap.
- The exact database counts were 3 assignments, 1 question, 1 reply, and 3
  results. The harness found no duplicate rows.
- Telemetry recorded zero tool errors and zero transport retries.
- No MCP transport timeout was observed. The explicit 20-second hub await and
  question timeouts were observed as successful tool outcomes.
- No context compaction was observed.
- The supervisor delivered the initial prompt but needed zero continuation
  reprompts during the passing run. Claude reported one streaming input turn,
  no permission denials, and no spawned subagents.

## Supervisor decision

An unsupervised Claude print-mode attempt ended during cycle 2 instead of
resuming pending work, so a thin persistent-stream supervisor was required.
An initial supervised trial then exposed Claude Code's built-in rejection of a
long foreground sleep; Alice rejected that trial because its long-work interval
was only 31.3 seconds. The harness-specific background sleep plus one blocking
`TaskOutput` wait used in the final run is Claude Code's supported equivalent.
Alice retains all assignment and release policy. A focused automated test also
forces a premature end-turn and verifies that the supervisor reprompts the same
persistent process.

## Reproduction

The run used an isolated hub with `HUB_LOST_AFTER_S=180`, a 30-second worker
heartbeat, and one absolute telemetry path. In the MCP config used by Claude,
the `mcpServers.hub.env` object included:

```json
{
  "HUB_URL": "http://127.0.0.1:8431",
  "HUB_TOKEN": "endurance-token",
  "AGENT_NAME": "bob",
  "HUB_HEARTBEAT_S": "30",
  "HUB_TELEMETRY_LOG": "/tmp/issue30-claude-supervised2/worker.jsonl"
}
```

Alice was run as:

```sh
uv run --locked python scripts/mock-alice.py \
  --db /tmp/issue30-claude-supervised2/hub.db \
  --agent bob --harness claude-code --timeout 300 \
  --endurance \
  --telemetry-log /tmp/issue30-claude-supervised2/worker.jsonl
```

Claude was launched through:

```sh
CLAUDE_MCP_CONFIG=/absolute/path/claude-code.mcp.json \
HUB_TELEMETRY_LOG=/tmp/issue30-claude-supervised2/worker.jsonl \
CLAUDE_MODEL=sonnet scripts/supervise-claude-code.sh
```

The MCP config and supervisor environment must name the same telemetry path;
`worker-mcp` reads it from the MCP config, while the supervisor watches it for
the current run's release record.

The final harness summary was:

```text
elapsed_s=1800.042 cycles=3 long_work_interval_s=220.713
assignment_timeouts=69 question_timeouts=1 long_task_heartbeats=8 releases=1
tool_errors=0 transport_retries=0
row_counts={assignments: 3, questions: 1, replies: 1, results: 3}
```

The strengthened guardrail accepts the preserved log with
`long_work_tool_gap_s=225.183` and `max_heartbeat_gap_s=31.836`.

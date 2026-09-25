# Codex endurance report

- Date: 2026-09-12
- Result: PASS observed in the recorded run; daemon-loop reliability not established
- Harness/version: Codex CLI 0.154.0
- Provider/model: OpenAI / `gpt-5.6-sol`
- Worker profile: `charlie`, heartbeat interval 30 seconds
- Harness elapsed time: 1,800.126 seconds
- Completed assignment cycles: 3

## Observations

The timed `scripts/mock-alice.py --endurance` scenario completed without a
human message after the measured worker session started. The worker completed
all three tasks, returned to `await_assignment` after each result, and consumed
Alice's final release response.

- `await_assignment` returned `timeout` 56 times, including the deliberately
  delayed first assignment, and the worker retried.
- `ask_alice` returned `timeout` once; the worker repeated the identical
  question and consumed Alice's reply.
- Cycle 2 contained a foreground `sleep 210`. Alice measured 247.73 seconds
  between assignment and result, exceeding `HUB_LOST_AFTER_S=180`.
- Eight accepted heartbeat records occurred while cycle 2 was active, and the
  heartbeat timestamp advanced throughout the long-work interval. Retrospective
  analysis of the recorded telemetry found a 242.303-second gap between
  consecutive worker tool calls, with all eight heartbeats covering that gap
  and a maximum 32.042-second heartbeat gap.
- The exact database counts were 3 assignments, 1 question, 1 reply, and 3
  results. The harness found no duplicate rows.
- Telemetry recorded one tool error: Codex probed the not-yet-authored Step 5
  implementer role guide and received HTTP 404. It was unrelated to the
  endurance workflow. Transport retry count was zero.
- The worker MCP call timeout was configured above the harness timeout. No MCP
  transport timeout was observed; the explicit 20-second hub await and question
  timeouts were observed as successful tool outcomes.
- No context compaction was observed.
- No supervisor was used. Two launch attempts before the measured session were
  configuration failures: combining `--approve-for-me` with `--sandbox` was
  rejected by the CLI, then omitting MCP approval caused every hub tool call to
  be denied. The successful launch used `--approve-for-me` without `--sandbox`.
  Once that measured session began, it required no intervention.

## Reliability qualification

The original result above is one successful measured run (`n=1`), not evidence
that Codex sustains the daemon loop reliably. An independent review at commit
`319d0da` ran the same Codex CLI/model twice more with per-tool MCP approvals:

- Run 1 failed in cycle 2 after the correct 210-second sleep. Codex submitted
  cycle 1's already-completed task ID instead of cycle 2's ID, did not retry the
  rejected result with the correct ID, and returned to `await_assignment`.
  Alice timed out the still-working cycle after 360 seconds.
- Run 2 passed all three cycles in 1,800.1 seconds with exact database counts
  of 3 assignments, 1 question, 1 reply, and 3 results.

Across the three executions that reached the scenario, two passed and one
failed. The Step 4B completion criterion has been observed, but Codex has not
been shown to hold the loop reliably. A Codex recovery policy or supervisor is
an architecture decision outside this telemetry/test gate; the report does not
claim one successful run settles it. Tool telemetry now includes the bounded
`task_id` on task-scoped start, success, and error records so this failure is
diagnosable from future telemetry without the Codex JSON event stream.

## Reproduction

The run used an isolated hub on port 8430 with `HUB_LOST_AFTER_S=180`, a
30-second worker heartbeat, and one absolute telemetry path. Start the hub:

```sh
HUB_STATE_DIR=/tmp/issue30-codex \
HUB_HOST=127.0.0.1 HUB_PORT=8430 \
HUB_PUBLIC_URL=http://127.0.0.1:8430 \
HUB_TOKEN=endurance-token HUB_LOST_AFTER_S=180 HUB_SWEEP_INTERVAL_S=5 \
uv run --locked hub
```

Run Alice in a second shell:

```sh
HUB_STATE_DIR=/tmp/issue30-codex HUB_LOST_AFTER_S=180 \
uv run --locked python scripts/mock-alice.py \
  --db /tmp/issue30-codex/hub.db \
  --agent charlie --harness codex --timeout 300 \
  --endurance --telemetry-log /tmp/issue30-codex/worker.jsonl
```

The exact successful worker launch was the following. `--approve-for-me` was
the approval setting used in the recorded run; no `--sandbox` flag may be
combined with it in Codex CLI 0.154.0.

```sh
codex exec --ephemeral --skip-git-repo-check --ignore-rules \
  -C /tmp --approve-for-me \
  -c 'mcp_servers={hub={command="uv",args=["run","--directory","/absolute/path/to/robo-agents","worker-mcp"],tool_timeout_sec=330,env={HUB_URL="http://127.0.0.1:8430",HUB_TOKEN="endurance-token",AGENT_NAME="charlie",HUB_HARNESS="codex",HUB_HARNESS_VERSION="0.154.0",HUB_PROVIDER="openai",HUB_MODEL="gpt-5.6-sol",HUB_HEARTBEAT_S="30",HUB_TELEMETRY_LOG="/tmp/issue30-codex/worker.jsonl"}}}' \
  'You are unattended endurance worker Charlie. Use only the hub MCP tools and the shell sleep command when an assignment directs you to wait. Do not inspect or modify repository files. Send no messages to a human and do not finish until the hub releases you. First call hub check_in. Then repeatedly call await_assignment with timeout_s 20. On every timeout, immediately call it again. For each assignment, follow its instructions exactly, including retrying ask_alice with the identical question after a timeout and sleeping for the full requested long interval. Submit exactly one result per assignment. After every result, resume the await_assignment timeout/retry loop. End only after await_assignment returns release true.'
```

For current runs, copying `runtimes/codex.config.toml` into the active Codex
configuration and replacing its placeholders is equivalent and narrower: the
template now sets `approval_mode = "approve"` only for the six `worker-mcp`
tools. With those per-tool settings, use `codex exec --json -s read-only` and
the same prompt above; `--approve-for-me` is not needed.

The final harness summary was:

```text
elapsed_s=1800.126 cycles=3 long_work_interval_s=247.73
assignment_timeouts=56 question_timeouts=1 long_task_heartbeats=8
tool_errors=1 transport_retries=0
row_counts={assignments: 3, questions: 1, replies: 1, results: 3}
```

Retrospective metrics computed from that run's telemetry using the strengthened
guardrail are `long_work_tool_gap_s=242.303` and
`max_heartbeat_gap_s=32.042`.

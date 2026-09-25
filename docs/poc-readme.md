# Agent Comms Hub

This repository implements the proof of concept described in
[`docs/poc-spec.md`](docs/poc-spec.md). The current implementation covers plan
Steps 1–6: the uv workspace, shared configuration and bearer-token
provisioning, the SQLite schema, A2A agent-card discovery, the hub core (A2A
request handlers, role-guide route, event queue, lease/heartbeat sweeper,
bearer enforcement), Alice's MCP tools over stdio (including the
`check_merge_gate` merge gate), and worker MCP tools connecting Claude Code and
Codex CLI workers to the hub, plus the durability contracts and reference
harness that precedes Step 5's runtime behavior. The hub-mode
[`alice-orchestrator`](skills/alice-orchestrator/SKILL.md) skill,
runtime-neutral [`guides/`](guides), and launcher [`prompts/`](prompts) now
encode the Step 5B workflow. The prompts-only
[`alice-relay`](skills/alice-relay/SKILL.md) skill remains as its reviewed
baseline ([trial notes](docs/notes/relay-trial-2026-09.md)).
Step 6 adds persistent isolated worker clones and a real-worker localhost
acceptance harness; see [reproduction](docs/step6-acceptance.md) and
[verified evidence](docs/evidence/step6-20260918191713_f99578f4.md).
For orchestrating issues on your own repositories, see the
[user guide](docs/user-guide.md).

## Run the hub

```sh
uv run hub
```

The hub listens on `http://127.0.0.1:8420` by default. On first startup it
creates `hub.db` and a mode-`0600` bearer token file under
`$XDG_STATE_HOME/agent-hub` (falling back to `~/.local/state/agent-hub`), so the
same state is found again no matter which working directory the process is
started from; set `HUB_STATE_DIR` (which must be absolute) to move it. A
relative `XDG_STATE_HOME` is invalid per the XDG base-directory specification
and is ignored in favour of the `~/.local/state` fallback. Override the other
defaults with the variables documented in [`.env.example`](.env.example). For a
deployed process, inject `HUB_TOKEN` rather than sharing the generated token
file.

`HUB_PUBLIC_URL` is the address the agent card advertises, not the bind address.
It defaults to `http://HUB_HOST:HUB_PORT`, which is correct only for a loopback
bind; binding the unspecified address in any spelling (`0.0.0.0`, `::`,
`0:0:0:0:0:0:0:0`, `*`) to reach remote workers requires setting
`HUB_PUBLIC_URL` to a dialable address such as `http://alice-host:8420`.

To load a local `.env` file explicitly:

```sh
uv run --env-file .env hub
```

## Alice's MCP connection

Launch the hub as a stdio MCP server from Claude Code with this configuration
(replace the absolute checkout and state paths):

```json
{
  "mcpServers": {
    "hub": {
      "command": "uv",
      "args": ["run", "--locked", "--project", "/absolute/path/to/robo-agents", "hub"],
      "env": { "HUB_STATE_DIR": "/absolute/path/to/hub-state" }
    }
  }
}
```

The launcher needs no working-directory setting. Keep `HUB_STATE_DIR` consistent
between launches to resume the same database, and stop an existing hub on the
same port before starting the runtime connection. HTTP and MCP share one store
and event loop; a separate process writing SQLite cannot wake these waits.
Closing MCP stdin or terminating the process shuts down HTTP and the sweeper.
A normal client disconnect is logged at info level. A standalone `uv run hub`
needs stdin to stay open; EOF before MCP initialization is an error and exits
nonzero so detached launches cannot silently appear healthy.

Alice gets `get_state`, `initialize_workflow`, `wait_for_event`, `assign_task`,
`check_merge_gate`, `reply`, `set_task_state`, `release_agent`,
`set_workflow_status`, and `log_decision`.
Her first mutating call must be `initialize_workflow(goal, policy)`, using the
goal and policy from the initial operator prompt. On restart, call `get_state`
first and resume the stored workflow; repeat `initialize_workflow` only with
those exact original values to confirm them. A different goal or policy is
refused so a later prompt cannot silently replace the durable rails. Worker
check-in may happen before initialization, but task assignment and workflow
status changes may not.
`wait_for_event` leases the oldest eligible event, waits up to 120 seconds
(default 100, under Claude Code's 120 s backgrounding threshold), and returns
`{"event": null}` on timeout; call again.
Zero seconds performs a nonblocking check. If your runtime uses a shorter tool
timeout, request a shorter hold or configure the client timeout above 120 seconds.
Delivery leases default to 10 minutes (`HUB_EVENT_LEASE_S=600`). Pass the
prior `delivery_id` as `ack` to acknowledge the event you just processed.
If Alice crashes before acking or if the lease expires, the hub redelivers
the event in strict FIFO order (`id ASC`). Mutating tools include state guards
to ensure retrying actions upon redelivery is idempotent. On reconnect, use
`get_state` to reconcile durable workflow, agent, and task state. Assignment
leases default to 30 minutes and accept positive finite values up to one year.

`get_state` returns a null workflow before one is created, plus agents and task
summaries including results but excluding instructions and transcripts.
Manual task overrides accept `canceled` or `failed` for open tasks, preserve
the note, and free the worker; a held question returns a terminal A2A Task
with its final state and result rather than interpreting the note as an answer; use a new assignment to retry terminal work.
Workflow status summaries and decisions persist in the SQLite audit log.
All diagnostics go to stderr; stdout carries only MCP JSON-RPC.

## What is public and what is not

Discovery and health are the entire public surface — a worker needs the card
before it holds a token, and health checks run before any credential exists:

```sh
curl http://127.0.0.1:8420/.well-known/agent-card.json
curl http://127.0.0.1:8420/healthz
```

Everything else — `POST /a2a` and `GET /guides/{role}.md` — requires the
pre-shared token, answering `401` with a `WWW-Authenticate: Bearer` challenge
when the header is missing, malformed, or carries a token that does not match:

```sh
TOKEN=$(cat ~/.local/state/agent-hub/token)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8420/a2a \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tasks/get","params":{"id":"x"}}'
# 401
```

## Talking to the hub

Workers are A2A clients; the hub is the only server. Check in with `READY` to
get the `contextId` every later call uses:

```sh
curl -s -X POST http://127.0.0.1:8420/a2a \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{
        "messageId":"m1","role":"user","parts":[{"kind":"text","text":"READY"}],
        "metadata":{"hub.agent":"bob","hub.capabilities":["python"],
                    "hub.harness":"claude-code","hub.provider":"anthropic"}}}}'
```

Then poll for work with `NEXT` on `message/stream`. The hub holds the response
open until Alice assigns a task or releases the agent, and returns a
`metadata.timeout` marker at the deadline so no agent ever spins:

```sh
curl -sN -X POST http://127.0.0.1:8420/a2a \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"message/stream","params":{"message":{
        "messageId":"m2","role":"user","parts":[{"kind":"text","text":"NEXT"}],
        "contextId":"<contextId from the check-in>"}}}'
```

## Role guides

Workers on any runtime get identical instructions from the hub rather than from
a Claude Code skill, so `GET /guides/{role}.md` is what the runtime-agnostic
design rests on:

```sh
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8420/guides/worker.md
```

The files come from the [`guides/`](guides) directory of this checkout, found
from the installed package and never from the working directory; set
`HUB_GUIDES_DIR` (absolute) to serve them from anywhere else. `{role}` is a role
slug, never a path: an unknown role, an unwritten guide and a missing directory
are all `404`. The directory includes worker etiquette plus implementer,
reviewer, and rebase role guides. The startup log names the directory the hub
is reading.

Progress notes, questions and results are `message/send` and `message/stream`
calls carrying a `taskId` and a `metadata.kind` of `progress`, `question` or
`result` — see §4.1 of the spec for the full mapping. A question that times out
must be retried under the `messageId` it was first asked with (the timeout
marker echoes it as `metadata.retry_as_message_id`), so that a reply Alice sent
between the two attempts still reaches the worker. Assignments themselves
come from Alice's MCP tools in this same process; `assign_task` wakes a pending
`NEXT` immediately.

## Checks

```sh
uv sync --locked --all-packages --dev
uv run --locked ruff check .
uv run --locked mypy
uv run --locked pytest
```

The Step 5C scripted acceptance driver and interactive Alice launchers are
documented in [`docs/step5-acceptance.md`](docs/step5-acceptance.md).

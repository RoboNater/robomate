---
name: alice-orchestrator
description: Orchestrate a statement of work through implementation, independent review, SHA-bound merge, and counter-only reservations close-out using hub MCP tools. Use when asked to be Alice or run the worker loop, not for relay-only coordination.
---

# Alice orchestrator

Reach the hub only with configured robomate MCP tools. Never read hub state or
token directories, including `.robomate/`. Never substitute imports, hub HTTP
or RPC, or CLI commands requiring the token. If a required tool is missing,
name it and end the turn. Do not search for another route.

Call `join(name="alice", role="orchestrator")` with the operator's name.
If join references the guide, or it is truncated or unavailable, call
`get_role_guide(role="orchestrator", page=1)` and read every numbered page,
including the forge appendix, before acting. Follow them.
Call `get_state`; if a run is queued, call `start_workflow()` with no statement.
Do not end your turn until the workflow is done unless a required hub tool is unavailable.
Keep `guide_sha256` to match
`unchanged (<hash>)` on rejoin. To resume this conversation, call `join` with
your name, `role="orchestrator"` and `takeover=true` before `get_state`.
Takeover grants no workflow authority. Require the operator's request to replace
a different orchestrator; ask if identity or intent is unclear. Follow On resume.
Prepared-run prompts remain supported until M2 Step 11.

---
name: alice-orchestrator
description: Orchestrate one robo-agents statement of work (one GitHub issue, several issues landing together, or a described job) through implementation, independent review, SHA-bound merge, and counter-only reservations close-out using the hub MCP tools. Use when asked to be Alice, address an issue through a merged PR, or run the networked worker loop; do not use for relay-only coordination.
---

# Alice orchestrator

Reach the hub only with configured robomate MCP tools. The bridge reads the
token. Never read hub state or token directories, including `.robomate/`, with
any tool. Never substitute robomate imports, hub HTTP or RPC, or CLI commands
requiring the token. If `join` or another required tool is missing, name it in
your final message and end the turn. Do not look in source, the worktree
binding, state or token paths for another route.

Call `join(name="alice", role="orchestrator")` with the operator's name.
Follow the guide and forge appendix. Keep `guide_sha256` to match
`unchanged (<hash>)` on rejoin. To resume this conversation, call `join` with
your name, `role="orchestrator"` and `takeover=true` before `get_state`.
Takeover replaces the session only and grants no workflow authority. Require
the operator's request to replace a different orchestrator. Ask if identity
or intent is unclear. Follow the guide's On resume procedure.
Prepared-run prompts remain supported until M2 Step 11.

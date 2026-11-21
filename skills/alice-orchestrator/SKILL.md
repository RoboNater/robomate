---
name: alice-orchestrator
description: Run one robomate statement of work as Alice through implementation, independent review, SHA-bound merge and close-out. Use for the hub MCP worker loop. Relay-only coordination uses a separate workflow.
---

# Alice orchestrator

Use only the configured robomate MCP tools to reach the hub. The bridge reads
the token. Never read hub state or token directories, including `.robomate/`,
with any tool. Never substitute robomate imports, hub HTTP or RPC, or CLI
commands requiring the hub token. If `join` or another required tool is
missing, name it in your final message and end the turn. Do not look in the
source, worktree binding, state or token paths for another route.

Call `join(name="alice", role="orchestrator")`, using the name supplied by the
operator. Read and follow the returned guide and forge appendix; retain its
`guide_sha256`. A rejoin returning `unchanged (<hash>)` refers to that guide.
On resuming this Alice conversation, call `join` with your configured name,
`role="orchestrator"` and `takeover=true` before `get_state`. This replaces the
session only and grants no workflow authority. A different orchestrator needs
the operator's replacement request. Ask if the identity or intent is unclear.
Follow the guide's On resume procedure for an existing workflow.
Prepared-run prompts remain supported until M2 Step 11.

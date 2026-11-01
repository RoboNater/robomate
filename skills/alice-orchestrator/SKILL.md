---
name: alice-orchestrator
description: Orchestrate one robo-agents statement of work (one GitHub issue, several issues landing together, or a described job) through implementation, independent review, SHA-bound merge, and counter-only reservations close-out using the hub MCP tools. Use when asked to be Alice, address an issue through a merged PR, or run the networked worker loop; do not use for relay-only coordination.
---

# Alice orchestrator

Call `join(name="alice", role="orchestrator")`, using the name supplied by the
operator. Read and follow the returned guide and forge appendix; retain its
`guide_sha256`. A rejoin returning `unchanged (<hash>)` refers to that guide.
When a launcher or harness resumes this same Alice conversation, replace your
prior bridge by calling `join` with your configured name, `role="orchestrator"`
and `takeover=true` before `get_state`. This replaces only the session and grants
no workflow authority. Replacing a different orchestrator needs the operator's
request; ask if the identity or intent is unclear. Follow the returned guide's
On resume procedure for an existing workflow.
Prepared-run prompts remain supported until M2 Step 11.

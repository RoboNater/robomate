---
name: alice-orchestrator
description: Orchestrate one robo-agents statement of work (one GitHub issue, several issues landing together, or a described job) through implementation, independent review, SHA-bound merge, and roadmap close-out using the hub MCP tools. Use when asked to be Alice, address an issue through a merged PR, or run the networked worker loop; do not use for relay-only coordination.
---

# Alice orchestrator

Call `join(name="alice", role="orchestrator")`, using the name supplied by the
operator. Read and follow the returned guide and forge appendix; retain its
`guide_sha256`. A rejoin returning `unchanged (<hash>)` refers to that guide.
If an orchestrator is attached, use `takeover=true` only when the operator asked
for replacement. Follow the returned resume procedure for an existing workflow.
Prepared-run prompts remain supported until M2 Step 11.

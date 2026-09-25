# Proof-of-Concept Roadmap Worklog & Historical Rationale

This document archives completed milestones, closed issues, review hardening, operational notes, and historical decision rationales from the Proof-of-Concept roadmap ([Issue #2](https://github.com/RoboNater/robo-agents/issues/2)).

- **Design of record:** [`docs/poc-spec.md`](poc-spec.md) (architecture, protocol, data model, 8-step plan in §7, locked decisions in §8).
- **Active roadmap:** [Issue #2](https://github.com/RoboNater/robo-agents/issues/2) (tracks active reservations, in-progress work, upcoming steps, and open decisions).

---

## Contents

- [Historical Counter Reservations](#historical-counter-reservations)
- [Step 1 — Scaffold](#step-1--scaffold)
- [Step 2 — Hub Core](#step-2--hub-core)
- [Step 3 — Alice MCP Tools](#step-3--alice-mcp-tools)
- [Step 4 — Worker MCP](#step-4--worker-mcp)
- [Step 4A — Durability Retrofit](#step-4a--durability-retrofit)
- [Review Hardening from #49](#review-hardening-from-49)
- [Step 4B — Worker Endurance](#step-4b--worker-endurance)
- [Step 5 — Guides, Alice Skill, Prompts](#step-5--guides-alice-skill-prompts)
- [Step 6 — E2E on Localhost](#step-6--e2e-on-localhost)
- [Step 7 — E2E networked](#step-7--e2e-networked)
- [User Documentation (#73)](#user-documentation-73)
- [User Run Preparation (#75, #76)](#user-run-preparation-75-76)
- [Settled Architectural Decisions](#settled-architectural-decisions)
- [PoC Status Summary (as of Step 7 / #147)](#poc-status-summary-as-of-step-7--147)

---

## Historical Counter Reservations

Shared, monotonic counters (schema version, migration number, wire `schema_version`, next event kind) reserved during Steps 1–6 (#40):

- **DB schema:** v3 = #26 (merged, #39); v4 = #23 (merged, #38) (collided; resolved at rebase); v5 = #24 (merged, #48); v6 = #25 (merged, #49); v7 = #27/#41 (merged, #47); v8 = #59 (merged, #58); v9 = #51 (merged, #67)
- **Wire `hub.schema_version`:** 1 = #23 (merged, #38). Bump only when an existing typed body changes shape; adding a new body (e.g. `RebaseResult` in #41) is additive and stays at 1.
- **Step 4B (#30):** no shared counter reserved; merged in #60 without schema or wire changes.
- **Sandbox repository plan (#62):** no shared counter reserved; documentation and external repository provisioning only; merged in #63.
- **Step 5A:** no shared counter reserved; contracts and reference-harness changes merged in #64 without schema, wire-version, or event-kind changes.
- **Step 6 (#28/#29):** no shared counter needed; merged in [#69](https://github.com/RoboNater/robo-agents/pull/69) (`5abf7dc1787af8c1ed78d85b9e27dabd70bb2a9f`), reusing DB schema v9 and wire version 1.
- **Step 6 native-Windows harness (#72):** no shared counter needed; merged in [#72](https://github.com/RoboNater/robo-agents/pull/72) (`4070cd3`) with harness, verifier and documentation changes only.
- **User guide (#73):** no shared counter needed; user documentation and workspace bootstrap guide merged in [#74](https://github.com/RoboNater/robo-agents/pull/74) (`f192331`).
- **User run preparation (#75/#76):** no shared counter needed; docs, run-preparation and supervisor scripts, and tests only, merged in [#81](https://github.com/RoboNater/robo-agents/pull/81) (`75dbdc5`).

---

## Step 1 — Scaffold

- [x] **Step 1 — Scaffold** (`7fb2663`)

  uv workspace, `common`/`hub`/`worker_mcp` packages, SQLite schema, config + bearer-token provisioning, agent card.

  *Done when:* `uv run hub` binds the port and serves the agent card. ✔

- [x] **#3 — Anchor hub state paths so CWD stops being load-bearing** (`b1a25f0`, #9)

  Durable state is anchored to `HUB_STATE_DIR`, defaulting to `$XDG_STATE_HOME/agent-hub` (fallback `~/.local/state/agent-hub`); `HUB_DB_PATH` and `HUB_TOKEN_FILE` resolve from it when relative. Nothing in the config consults the working directory any more, and startup logs the resolved absolute database path. ✔

- [x] **#4 — Stop `HUB_PUBLIC_URL` defaulting to the bind host** (`b1a25f0`, #9)

  Shipped with #3 as one PR. A bind on the unspecified address with `HUB_PUBLIC_URL` unset now raises `ConfigurationError` instead of advertising an undialable address; the derived default remains for a specific interface. IP-literal bind hosts are normalized for the resolver, with IPv6 brackets added back only for URL authorities. ✔

- [x] **#8 — Spec: make bearer enforcement a Step 2 deliverable** (`81d3fe7`, #10)

  §7 Step 2's deliverable now names bearer enforcement and its done-when adds both negative cases (no header, wrong token → 401), so the criterion can detect missing auth. §4.1 states the public/protected split explicitly — only the agent card and `/healthz` are public — and §4.2 settles the guides route: it **requires the token**. Recorded as a locked decision in §8; the README now flags that Step 1 provisions the token without enforcing it. ✔

---

## Step 2 — Hub Core

- [x] **Step 2 — Hub core** (`b3c503e`, #13)

  The §4.1 handlers, Alice's event queue, the lease/heartbeat sweeper and bearer enforcement. Workers' waiting intents (`NEXT`, a question) hold an SSE response open and return a timeout marker at a bounded deadline; a question retried under its original `messageId` resumes rather than re-asking, so a reply Alice sent between attempts is not stranded (added to §4.1 as an explicit contract — **Step 4's `ask_alice` has to implement it**). `/a2a` is the only protected route and the card and `/healthz` the entire public surface, asserted by a test rather than by convention.

  *Done when:* met — `curl` drove READY → NEXT → progress → question → result → release against a running hub; an idle `NEXT` held for exactly its deadline; the same POST with no header and with a wrong token both returned 401. Three state-machine defects found in review (question identity across retries, a release erased by re-check-in, assignment to a lost agent) were fixed before merge. ✔

- [x] **#5 — Serve `GET /guides/{role}.md`** (`97e7075`, #14)

  The route joins **Step 2** in §7 (§4.2, §6 and §8 updated to match) and Step 5 keeps only the guide *text*, so the endpoint Step 4 consumes is no longer unowned. It reuses Step 2's bearer dependency, as sequenced. `HUB_GUIDES_DIR` follows the state-directory rule — absolute, never resolved against the working directory — defaulting to the checkout's top-level `guides/` found from the installed package, with `HUB_STATE_DIR/guides` as the fallback outside a workspace; startup logs the directory it resolved. `{role}` is a role slug (`[a-z][a-z0-9-]*`, `fullmatch`), never a path, and the resolved file must still sit inside the resolved guides directory, so an escaping symlink is refused while a deliberately symlinked guides directory still serves. Unknown roles, unwritten guides and a missing directory are all 404, which is every request until Step 5 writes content; `guides/` ships as a placeholder with a README that is not a role and is not served.

  *Done when:* met — against a running hub, a guide dropped into `guides/` returned 200 as `text/markdown`, the same request without the token 401, and an unknown role, a traversal attempt and `README.md` all 404. Review found one defect (a trailing newline slipped through `re.match`, since `$` matches before a final newline) — fixed with `fullmatch` and regression-tested at both the helper and HTTP levels before merge. ✔

  **Decision settled here:** guides are fetched per call, **no local worker cache** (spec §8). A worker that cannot reach the hub has no assignment to work on either, so a cached guide buys no offline capability, and an edited guide has to take effect at the hub. Step 4 needs no cache path.

- [x] **#7 — Reserve stdout structurally for MCP framing** (`aceb2bf`, #15)

  The entry point reserves the original stdout stream and redirects ordinary Python output to stderr for the server lifetime. Every Uvicorn logging handler is explicitly bound to stderr. Regression tests cover exclusive writes to the reserved stream, restoration on exceptions, and a live HTTP lifecycle with access logging enabled, stray prints, a stdout logging handler and a traceback; captured stdout stays empty. All CI checks passed (150 tests), and review found no issues. Native writes to file descriptor 1 and deliberate writes through `sys.__stdout__` remain outside this Python-stream guard. ✔

  **Step 3 integration:** pass the stream yielded by `reserve_stdout()` explicitly to the MCP transport; the transport must not discover the redirected `sys.stdout`. The transport itself remains Step 3 work.

---

## Step 3 — Alice MCP Tools

- [x] **Step 3 — Alice MCP tools** (`f03684f`, #16)

  The §4.2 tools run over stdio in the same process and event loop as the HTTP hub. The implementation adds durable workflow status, decision logging and terminal task overrides; preserves stdout exclusively for MCP framing; and shuts down HTTP, lifespan and MCP cleanly across EOF, signals, transport stalls and failures.

  *Done when:* met — Claude Code connected and listed all eight tools; integration coverage verified that `wait_for_event` blocks and an HTTP worker check-in wakes it. Review also exercised assignment/reply wakeups, wire cancellation, state snapshots and shutdown failure paths. ✔

### Step 3 Operational Notes

Step 3 operational note: the hub state directory is independent of the working directory, so the MCP launcher does not need to set `cwd`. It does need `HUB_STATE_DIR` (or `XDG_STATE_HOME`) to be consistent between the interactive `uv run hub` and the runtime-launched process, since a different value still selects a different database.

Step 3 reuses `HubStore` for `assign_task`, `reply` and `release_agent`. The waits (`wait_for_event`, `await_assignment`, `await_reply`) signal in-process as §2 specifies because Alice's tools share the hub's event loop; a second process writing the same SQLite file would not wake a held request.

---

## Step 4 — Worker MCP

- [x] **Step 4 — Worker MCP** (`605f84c`, #19)

  The §4.3 tools (`check_in`, `get_role_guide`, `await_assignment`, `report_progress`, `ask_alice`, `submit_result`) run over stdio with stdout reserved for MCP framing. `WorkerHubClient` implements exponential backoff retries, question correlation across timeout retries under original `messageId` (§4.1), and terminal task override detection. Decided second runtime: Codex CLI (`charlie`), configured in `runtimes/codex.config.toml` alongside Claude Code (`bob`) in `runtimes/claude-code.mcp.json` (and `runtimes/gemini.settings.json` for reference). `mock-alice.py` drives worker tasks over HTTP and stdio `--mcp`. Schema bumped to v2 with idempotent migration for `agent.runtime`. Windows compatibility issues (#18) resolved.

  *Done when:* met — `mock-alice.py` drove tasks through both Claude Code and Codex CLI worker runtimes; all CI checks passed on native Windows (195 passed) and Linux (205 passed). ✔

- [x] **#6 — Handle "no checks reported" as distinct from "checks failed"** (`2f01f87`, #20)

  Spec §5 and §8 updated with the CI merge gate protocol. Alice branches on check presence first and evaluates terminal status via `gh pr checks --json name,bucket,link`: green requires every check in `pass` (or `skipping`), failing checks route to §5's retry loop, and cancelled runs (e.g. from `concurrency: cancel-in-progress: true`) are treated as unknown and re-polled like absent checks (every ~10 s up to 60 s; once a superseding check appears, Alice re-enters from `--watch`). Absent checks query `gh api repos/{owner}/{repo}/actions/workflows` (Actions-only): `total_count == 0` escalates immediately (unless `require_ci_green: false` suppresses it); `total_count > 0` polls every ~10 s up to 60 s. Confirmed from source that `gh pr checks --watch` exits 1 immediately on absent checks (verified, gh 2.96.0). §8 adds the sandbox repo CI prerequisite (`pull_request` workflow) and locked decision row. Stale runtime references split to #21 per AGENTS.md scoping rules. ✔

- [x] **#22 — Namespace hub metadata, pin `a2a-sdk`, scope the A2A claim** (`3c138e4`, #34)

  Scoped the A2A claim in spec §2 to "A2A-shaped", added §4.0 documenting the compatibility profile, and pinned `a2a-sdk==0.3.26`. Prefixed all hub metadata keys with `hub.*` across common constants, packages, and tests, and added JSON wire fixtures asserting request/response shapes. ✔

- [x] **#33 — Reorder plan; add PoC success statement** (`dfbdb0e`, #35)

  Added the PoC success statement to §1, updated §5 Rails and §8 locked decisions with `allow_no_ci` and `role_policy` defaults, and reordered §7 to sequence Step 4A (durability retrofit, #22–#27) and Step 4B (worker endurance, #30) ahead of Step 5, adding the "Changes to completed steps" contract note for Step 4A. Folded in #21 (settling Codex CLI in §2 and §8) and updated README status. ✔

- [x] **#32 — Document PoC threat model; per-agent credentials in §9** (`dd99ba1`, #36)

  Documented the PoC threat model trust assumption in §1 non-goals (shared bearer token authenticates membership in the PoC, not agent identity; identity and model metadata are self-declared) and recorded post-PoC security requirements in §9 (per-agent credentials mapping to immutable server-derived identity, bound to `worker_instance_id`, revoked on supersede/release, TLS mandatory outside trusted tunnel/VPN, and scoped GitHub tokens per role, preserving the task/result protocol). ✔

- [x] **#40 — Roadmap as coordination channel: reserve shared counters before parallel work** (`9295224`, #46)

  Found in the relay trial: #23 (#38) and #26 (#39) were implemented in parallel and both claimed DB schema v3, which surfaced only at rebase and cost an extra review round. The **process rule is in force** — see **Reservations** at the top — and must be honoured before #24 and #25 start in parallel. Spec §5 now requires a reservation before assignment and treats a rebase collision as an orchestration defect; the checked-in `alice-relay` skill enforces the same kickoff check and merge-time roadmap update. ✔

- [x] **#43 — Check in the `alice-relay` skill and relay-trial notes** (`a8c1218`, #45)

  `skills/alice-relay/SKILL.md` is checked in byte-for-byte unmodified. `docs/notes/relay-trial-2026-09.md` records the four relay runs (#33, #32, #23, #26): a setup table of model and harness per role, a one-paragraph outcome for each, and the human's prompt logs, edited for formatting only (earlier, pre-trial history is left out). The README status notes that Alice runs in relay mode so far and that the hub-mode skill is Step 5. Spec §6 lists `skills/alice-relay/` (the "Claude Code only" note now covers only `alice-orchestrator` and `worker`, since the relay skill ran in OpenCode), and §7 Step 5 derives `alice-orchestrator` from it: templates and rails carry over, tool bindings are new. Reviewed with no blocking findings; merged pinned to the reviewed head. #42 now has its baseline. ✔

- [x] **#21 — Update spec (§2, §8) to record settled second worker runtime (Codex CLI)** (`dfbdb0e`, #35)

  Step 4 (#19) settled Codex CLI (`charlie`), landing `runtimes/codex.config.toml` on `main`. Updated §2 line 45 and diagram, the §8 `Runtime` row, added `Second runtime` row, and removed the bottom "Still open" runtime note. Shipped with #33. ✔

---

## Step 4A — Durability Retrofit

- [x] **Step 4A — Durability retrofit** (#34, #38, #39, #47, #48, #49)

  Durable foundations across #23 (typed, versioned task results & idempotent worker mutations), #24 (background heartbeat & worker instance ID), #25 (durable event delivery with implicit ack), #26 (worker identity profile & policy-driven role selection), and #27 (SHA-bound approval/merge & `check_merge_gate` tool), as a coordinated retrofit: each PR takes its schema version from Reservations. #41 (stale-base detection & REBASE step) shipped with #27, as it changes the same tool and the same §5 MERGE text, and so did #50 (late acks), whose subject is that same MERGE window. (#22 already completed in #34).

  The reservation rule (#40) did its job: every PR here took its schema version from Reservations and the four that landed after it needed no renumbering. #41/#27 was paused mid-flight when it turned out to be sequenced ahead of #24 and #25 — v7 before v5 and v6 — and resumed after they merged, which cost one merge of `main` and no reservation collision.

  Progress:

  - [x] **#26 — Worker identity profile & policy-driven role selection** (`5f11b97`, #39)

    `agent.runtime` is replaced by the §3 profile: `harness`, `harness_version`, `provider`, `model`, `model_source` (`declared`/`env`/`unknown`), `capabilities[]`, `workspace_id`. Workers read it from `HUB_HARNESS`, `HUB_HARNESS_VERSION`, `HUB_PROVIDER`, `HUB_MODEL` and `HUB_CAPABILITIES` (`AGENT_RUNTIME` is still honoured). Unset fields are `unknown`, never guessed, and the old `claude-code` default is gone. An agent may declare its own model via `check_in(model=…)`, which counts only when the launcher names none. The hub validates the profile, replaces it on every re-check-in, and exposes it in the `agent_checked_in` event and per agent in `get_state`. Schema **v3**: the migration checks which columns exist, carries `runtime` over as `harness`, and drops the old column. Spec §5 now selects the implementer/reviewer pair by `role_policy` instead of check-in order: `pairing_wait_s` deferral, escalation naming the failed rule, and the pairing recorded in the log and wrap-up. An `unknown` harness or provider never satisfies a "differs" rule. §8 records Codex CLI 0.154.0's **observed MCP tool timeout of 300 s** (overridable by `tool_timeout_sec`), so `runtimes/codex.config.toml` sets `tool_timeout_sec = 330` to fit the 300 s maximum hold plus the worker's 15 s client margin. Verified live: `mock-alice.py --mcp` drove a real Codex CLI worker, and the stored profile matched its launcher env. ✔

    **Carried forward:** the policy *evaluation* (including "same harness + `reviewer_harness_differs` → escalate") is Step 5 skill work. `workspace_id` was subsequently wired by #28 in Step 6 (#69): worker check-ins now report persisted full-clone identities. Nothing in the hub writes `policy_json` (`get_state` shows `policy: {}`), so Step 5 must decide whether a restarted Alice needs the policy persisted.

  - [x] **#23 — Typed task results & idempotent worker mutations** (`81080e9`, #38)

    Structured, versioned task result models in `agent_hub_common` (`ImplementerResult`, `ReviewerResult`, `Finding`, `TestResult`, with wire protocol `SCHEMA_VERSION = 1`). Enforces semantic invariants: `pr_url` and 40-char hex commit SHA (`head_sha`) are required when `outcome == "completed"`; `reviewed_head_sha` (40-char hex) is required and `blocking_findings` must be empty when `verdict == "approved"`. Wire mutations (`check_in`, `progress`, `result`) enforce `hub.schema_version = 1` and `hub.operation_id` string. Completed operations are recorded in a new SQLite `operation` table (`actor`, `operation_id`, `payload_hash`, `response_json`, `created`), executing mutations and ledger writes atomically within `BEGIN IMMEDIATE`. Replays with matching operation ID and identical payload return the cached response without side effects; conflicting mutations return HTTP 409 (-32600); validation failures return HTTP 400 (-32602) keeping open tasks in `working` state. `WorkerHubClient` maintains operation ID lifecycles across ambiguous timeouts and clears pending IDs on confirmed success. Schema **v4**: bumped from v3, composing with #26 to migrate v1/v2/v3 databases cleanly to v4. ✔

  - [x] **#24 — Background heartbeat & worker instance identity** (`326eb04`, #48)

    `worker-mcp` now generates a per-process `worker_instance_id`, includes it on every worker A2A call, and sends timer-driven heartbeats every `HUB_HEARTBEAT_S` (default 30 s), independent of LLM tool activity. The hub tracks `last_heartbeat` separately from `last_progress_at`, reports both ages in `get_state`, rejects duplicate live-name check-ins with HTTP 409, and safely supersedes lost instances while ignoring stale heartbeats. `HUB_LOST_AFTER_S` defaults to 180 s; loss emits exactly one `agent_lost` and fails assigned work with `reason=worker_lost`. Matching-instance heartbeats renew the original task lease window only up to the workflow `max_task_lease_min` rail (default 120 minutes), after which `lease_expired` fires once. Schema **v5** composes migrations from v1–v4, and wire fixtures cover the heartbeat shape. CI passed (282 passed, 10 skipped). ✔

  - [x] **#25 — Durable event delivery with implicit ack** (`6424f5a`, #49)

    The SQLite `event` table replaces boolean `consumed` with durable leasing and implicit acknowledgment: `state` (`queued`/`delivered`/`acked`), `delivery_id`, `delivery_attempts`, `delivered_at`, `delivery_expires`, and `acked_at`. Events are leased in strict FIFO order (`ORDER BY id ASC`), with unacknowledged deliveries expiring after `HUB_EVENT_LEASE_S` (default 600 s) and returning to the front of the queue with incremented `delivery_attempts`. Acking happens implicitly on the subsequent `wait_for_event(ack=delivery_id)` call or explicitly via `ack_event`. Expired or invalid delivery IDs are safely ignored. Acked events are retained for audit and replay inspection. The `decision` table adds a unique `key` column for idempotent action checkpointing (`event:{id}:<action>`). Hub mutating tools enforce state guards preventing duplicate actions on redelivery: `reply` safely no-ops (`applied: False`) on non-`INPUT_REQUIRED` tasks (including completed/terminal tasks) and suppresses stale duplicate answers when `message_id` matches; `assign_task` rejects busy workers with HTTP 409; `release_agent` and `set_workflow_status` are idempotent no-ops. `get_state` exposes `queued_events` and active `unacked_delivered`. `scripts/mock-alice.py` implements crash simulation points (`delivery`, `after_action`, `before_ack`, `after_reply`), state recovery across restarts, and end-to-end MCP driving. Schema **v6** composes migrations from v1–v5, dropping indexes prior to table alteration. CI passed (299 passed, 10 skipped). ✔

  - [x] **#27 + #41 — SHA-bound approval/merge, `check_merge_gate`, stale-base detection & the REBASE step** (`a488e93`, #47)

    `check_merge_gate(pr_url, expected_head_sha)` reads the whole gate in one call: `pr_state` (`open`/`merged`/`closed`), `current_head_sha` with `head_matches`, `ci` (`pass`/`fail`/`pending`/`cancelled`/`no_checks`/`no_workflows`) with the raw `checks[]`, `base_behind_main` with `base_ref`/`base_sha`/`main_sha` from the compare API (`behind_by > 0`), and `mergeable` (`clean`/`conflicting`/`unknown`) with `merge_state_status`. It re-reads every 10 s for up to 60 s while CI or mergeability is still settling, and returns at once when waiting cannot help — PR not open, head moved, base behind, or conflict. `gh` runs with stdin closed, stdout captured and prompting disabled so it cannot disturb MCP framing (#7); only a strictly parsed PR URL reaches it; a `gh` failure raises rather than reporting, because an unreadable gate is not permission to merge. §5 replaces the by-hand CI procedure with the MERGE invariant, evaluated on a reading taken immediately before merging — `verdict == approved ∧ pr_state == open ∧ head_matches ∧ (ci == pass ∨ (ci == no_workflows ∧ allow_no_ci)) ∧ base_behind_main == false ∧ mergeable == clean ∧ policy permits` — and routes every failed clause: moved head → RE-REVIEW whatever the diff size, stale base or conflict → the new **REBASE** step, already `merged` → WRAP-UP. Tasks carry `pr_head_sha` (schema **v7**, composing after v5/v6) and a third role, `rebase`, whose `RebaseResult` (`outcome`, `head_sha`, `conflict_files[]`, `resolution_summary`) decides what follows: an empty `conflict_files` merges the new head once its CI passes, a non-empty one goes back for review and does not count as a review round (#42). `guides/rebase.md` is written here rather than in Step 5, because the role names the guide the worker fetches. Verified live against this repository's own PRs, including the stale-base case. Reviewed with no blocking findings at `cea0c94` and merged pinned to that head with `--match-head-commit`. ✔

    **Departures from the issue text, recorded in the PR:** the guide is `rebase.md`, not `implementer.md`, since workers fetch the guide named by their role; `ci == pending` means "call the gate again" rather than #27's literal "escalate", because real runs outlast a 60 s poll and slow CI is not a fault; `mergeable == unknown` is polled inside the same window before being returned as unknown.

    **Carried forward:** the base branch can still move between the gate's reading and `gh pr merge`, since `--match-head-commit` binds only the head. The window is seconds and §5 documents it; "require branches to be up to date before merging" closes it on repositories that enable it. The Step 6 E2E is what exercises the REBASE route end to end.

  - [x] **#50 — Accept a late event `ack` while its delivery is still the latest** (`a488e93`, #47)

    `ack_event` no longer requires `delivery_expires > now`. A matching `delivery_id` is itself the proof of ownership, because every re-lease mints a new one, so the expiry check bought no safety while it cost a redelivery of any action longer than `HUB_EVENT_LEASE_S` (600 s) — the MERGE window above most of all, where the gate waits on CI and `gh pr merge` follows. Late acks are logged so a chronically short lease stays visible, and §4.2, §5 Rails and §8 record the rule, revising #25's wording. The gate's `pr_state` closes what #50 could otherwise only leave to Alice noticing: a redelivered MERGE event sees `merged` and goes to WRAP-UP instead of merging a second time. Shipped with #27/#41 rather than after them, since it is the same window. ✔

  *Done when:* met — wire fixtures pass; typed results survive SQLite round-trip; the background heartbeat maintains the lease without LLM calls; implicit ack prevents duplicate actions in the guarded cases (#51 carries the two it misses); role selection respects the profile; `check_merge_gate`'s unit tests pass, including #41's stale-base and `mergeable` cases and #27's six `ci` outcomes. ✔

  **Left to later steps:** #51 (the two redelivery gaps the guards miss, after Step 5), #52 (renewal-window semantics when an initial lease is capped), and the §5 skill work that evaluates `role_policy` and drives the gate — Step 5.

---

## Review Hardening from #49

- [x] **Review hardening from #49 — process rules, real migration fixtures, a redelivery harness, and schema convergence** (`6f6d823`, #58)

  Four issues filed out of the four-round review of #49 and shipped together, because they share one cause: the changed surface had no coverage, so CI was green while the code was broken.

  - **#53 — AGENTS.md: issues carry their acceptance checks; run what you changed.** An issue that changes behaviour names, under Tests, the commands a reviewer will run — so the implementer runs them first. Before opening a PR: run every script and entry point it touches (`ruff` and `mypy` cannot see a loop whose body never executes), re-read each edited function in its final form rather than the diff hunks, and say why in the description if production code had to change to make a new test pass. Tests that drive the app use conftest's `hub_store`, because a second `HubStore` on one database has its own `Signals`. ✔

  - **#54 — Migration fixtures built from dumped real schemas.** `tests/fixtures/schema_v1.sql`–`schema_v7.sql`, each generated from the commit that shipped that version, rendered by the new `scripts/dump-schema.py` and replayed by `_legacy_database()` instead of DDL written from memory. Bumping `SCHEMA_VERSION` now includes dumping the version it replaces, recorded where the version is claimed. Verified against the original failure: deleting the `DROP INDEX IF EXISTS idx_event_inbox` line from `_migrate_event_delivery` fails ten tests with #49's `OperationalError`, so `uv run --locked pytest` alone would have caught it. ✔

  - **#59 — Migrated and fresh schemas converge (schema v8).** #54's new drift check found that a migrated database never had the same schema as a fresh one: `ALTER TABLE ADD COLUMN` appends and a column-level `UNIQUE` cannot be added at all, so `agent`, `task`, `event` and `decision` differed coming from v1–v4, and `event` and `decision` from v5. Nothing broke on it, but the check had to normalise both causes away, which cost it the ability to see a column inserted in the wrong place, a duplicated one, or a lost constraint. v8 rebuilds those four tables into the shape `SCHEMA` declares — SQLite's supported procedure under `foreign_keys=OFF` and `legacy_alter_table=ON`, in one explicit transaction — and `decision.key` loses a redundant inline `UNIQUE` that the partial `idx_decision_key` already enforced. `_schema_objects` now compares raw `sqlite_master` SQL with nothing exempted. Filed and worked inside #58 on the human's approval, once it was established that no live hubs exist. ✔

  - **#55 — A redelivery property harness, and the cases the #49 rounds missed.** `test_replaying_a_delivered_event_changes_nothing_twice` states #25's invariant once over every event kind, modelling Alice without her own recovery discipline so that what is under test is what the hub's guards allow rather than what a careful caller avoids asking for. #51's second gap sits there as a strict `xfail`, so closing #51 turns it into a failure that must be updated instead of a gap that quietly re-hides. Adds the three untested cases (`before_ack` in the MCP crash points, `reply` with `message_id` omitted pinned as it behaves today, and the q1/q2 interleaving that produced the wrong answer), moves the four crash tests onto conftest's fixtures, and gives the recovery timings one home. ✔

  Reviewed by Charlie, who reproduced two findings on the v8 rebuild: it was not failure-atomic — no transaction is open before its first DDL in Python's legacy mode, so a failed copy stranded the rows in the scratch table and the retry stamped the version over the loss — and it reset `AUTOINCREMENT` high-water marks where the highest historical ids were gone. Both fixed in `6c684ce` with regression tests that fail against the reviewed commit; approved at that head and merged pinned to it. ✔

**Unfiled spec items.** Four items were once drafted here against anticipated issue numbers #53–#56, which GitHub assigned to unrelated issues. Their text is no longer in this roadmap; if any is still wanted, file it as a new issue and list it here.

---

## Step 4B — Worker Endurance

- [x] **Step 4B — Worker endurance** (`80b05c9`, #60; GitHub issue #30)

  Added the timed three-cycle, 30-minute worker gate, structured worker telemetry, exact retry/row verification, and the scenario-specific Claude Code supervisor. Telemetry evidence is scoped to the checked-in worker instance, stale supervisor records cannot satisfy release, long work is proven by a heartbeat-covered no-tool interval beyond `HUB_LOST_AFTER_S`, and telemetry failures cannot break worker startup.

  Real runs completed on Claude Code 2.1.270 / `claude-sonnet-5` through the thin supervisor and Codex CLI 0.154.0 / `gpt-5.6-sol` unsupervised. The Codex report records the independent wrong-task-id failure and 2-of-3 aggregate rather than claiming reliability from one pass. Both reports contain reproducible launch and approval configuration. Charlie and Douglas independently reviewed the work; Douglas also completed a deterministic 30-minute end-to-end rerun with stale telemetry seeded. The final §2 diagram records the actual supervisor → Claude → `worker-mcp` topology. ✔

  *Done when:* met — both real runtimes completed the scenario without human reprompting, Claude passed through the thin supervisor, and reports are checked in at `tests/reports/endurance-<harness>.md`. ✔

- [x] **#62 — Document and provision the sandbox repository** (`49d529e`, #63)

  Added `docs/plan-for-the-sandbox-repo.md` and linked §2, §7 Steps 5–6, and §8 to the concrete private [`RoboNater/robo-agents-sandbox`](https://github.com/RoboNater/robo-agents-sandbox). The sandbox has a dependency-free Python 3.12 baseline, three tests, role-boundary `AGENTS.md`, active `pull_request` CI, squash-only merges, topic-branch cleanup, and read-only Actions permissions. Setup PR #1 verified the `test` check and was closed unmerged so Step 5 retains the first workflow-driven merge. Private-repository branch protection remains an explicit operator choice because the current GitHub tier requires public visibility or an upgrade. ✔

---

## Step 5 — Guides, Alice Skill, Prompts

- [x] **Step 5 — Guides, Alice skill, prompts** (`6cf9f91`, #67)

  Step 5 keeps the workflow and acceptance criterion defined in spec §5 and §7, but ships it in exactly three reviewable substeps. The issues refine or extend that definition; they do not replace it. Do not add a fourth substep: defects found by the integrated demo are fixed and disclosed within 5C.

  - [x] **Step 5A — Contracts and reference harness** (`b79f93f`, #64)

    Consolidate `scripts/mock-alice.py` behind one lifecycle with direct-store and MCP backends (#56). Land the relay-loop rules from #42 in the spec and `alice-relay`; document the same-account, comment-based approval contract from #37; settle and implement durable initialization of the initial prompt policy in `workflow.policy_json`; and enforce #29's message-part and typed-result size caps.

    *Done when:* both `mock-alice` backends pass the same lifecycle, recovery, and four crash-point tests; custom workflow policy survives restart and is used by hub rails; oversized parts/results fail with actionable errors; the spec and relay baseline agree on round counting, recommendation-style escalation, newest-head selection, roadmap ownership, and comment-based approval. ✔

  - [x] **Step 5B — Runtime behavior: guides, prompts, and Alice skill** (`5dbbafa`, #66)

    Write `guides/worker.md`, `guides/implementer.md`, and `guides/reviewer.md` (retaining the existing rebase guide); add the thin worker skill and `prompts/alice.md` / `prompts/worker.md`; and implement `skills/alice-orchestrator/SKILL.md` over the Step 5A contract. Include the guide/skill portions of #28, #29, and #31; carry forward #40, #42, #43, and #37; and include Alice's half of #51 by always passing question `message_id` and checkpointing multi-action events. Close #65 by removing the legacy workflow-initialization bypass and documenting the required first Alice call in the README.

    *Done when:* deterministic tests cover policy-driven implementer/reviewer pairing, reservations, typed result handling, review-round rules, newest-SHA binding, questions, resume reconciliation, re-review/rebase routing, the merge invariant, escalation, release, and wrap-up. Every worker runtime receives equivalent role instructions without depending on a harness-specific skill.

  - [x] **Step 5C — Integrated acceptance demo** (`6cf9f91`, #67)

    Extend `mock-worker.py` into the scripted scenario driver; add run manifests, sandbox throwaway-PR seeding, Step 5 launch scripts, crash-injection hooks, and an untrusted-text scenario; then drive a real Alice session against the private sandbox repository.

    *Done when:* the spec's existing Step 5 criterion passes unchanged: `mock-worker.py` drives real Alice through PLAN → IMPLEMENT → REVIEW / ADDRESS → MERGE → WRAP-UP, Alice executes the first workflow-driven merge against a throwaway PR in `RoboNater/robo-agents-sandbox`, injected instructions do not alter behavior, and the run leaves reproducible evidence.

  **Associated issue ownership**

  - Closed in 5A: #37, #42, and #56 (#64).

  - Closed in 5C: #51 and #52 (#67).

  - Closed in Step 6: #28 (workspace enforcement, narrowed to localhost) and #29 (real-worker injection E2E), with #69.

  - Leave #31 open for the full recovery matrix in Step 8; #70 open for networked workspace verification in Step 7.

  - Completed inputs carried forward without reopening: #40 (counter reservations), #43 (relay skill and trial baseline), and #62 (sandbox repository and operating plan).

- [x] **#51 — Close the remaining redelivery gaps in the mutating tool guards** (`6cf9f91`, #67)

  Two gaps #25 left open, found reviewing #49. `reply` suppresses a stale answer only when the caller passes `message_id`, so an answer to an old question can still be applied to a newer one — reproduced against the store. And `assign_task` guards only a *busy* worker, so a redelivered `agent_checked_in` assigns a second task once the first has completed. In both, the hub cannot enforce idempotency alone and leans on Alice's discipline.

  The hub-side guards are small and could ride along with Step 4A; this sits after Step 5 because the other half is skill work — Alice passing `payload.message_id` and keeping the `log_decision` checkpoint — and §4.1's `operation` ledger may be the cheaper answer for all of Alice's mutations at once. #31's recovery matrix (Step 8) is what proves both ends together.

  *Done when:* a question redelivered after a newer one is asked never reaches the worker as the answer to the newer one, with `message_id` omitted; a redelivered `agent_checked_in` after task completion creates no second task.

- [x] **#52 — Clarify renewal-window semantics when an initial task lease is capped** (`6cf9f91`, #67)

  `assign_task` caps the initial `lease_expires` at the workflow's `max_task_lease_min` but records `lease_duration_s` from the uncapped request, so a task asked for above the cap stores a renewal window longer than its own first lease. Heartbeat renewal re-applies the absolute cap, so nothing is unsafe today; what is undecided is whether `lease_duration_s` means the caller's request or the effective window. Noticed in #24's code while resolving #47's merge, and filed by the reviewer of #47.

  *Done when:* §3 and §4.2 say which of the two `lease_duration_s` is, `assign_task` matches, and a test covers a request above the cap.

---

## Step 6 — E2E on Localhost

- [x] **Step 6 — E2E on localhost** (`5abf7dc1787af8c1ed78d85b9e27dabd70bb2a9f`, [#69](https://github.com/RoboNater/robo-agents/pull/69))

  Completed run `20260918191713_f99578f4` against fresh [sandbox issue #9](https://github.com/RoboNater/robo-agents-sandbox/issues/9), [work PR #10](https://github.com/RoboNater/robo-agents-sandbox/pull/10), and CI-checked unrelated [base PR #11](https://github.com/RoboNater/robo-agents-sandbox/pull/11). Interactive Claude Alice and supervised Claude Bob used 2.1.277 / `claude-sonnet-5`; Codex Charlie used 0.154.0 / `gpt-5.6-sol`. Bob/Charlie reported distinct persistent full-clone identities via `HUB_WORKSPACE`; Charlie actually fetched, checked out and tested each assigned review SHA in his own clone. All 61 correlated verifier checks and exported-fact replay pass; required coordination CI passed 570 tests. Three failed measured attempts remain preserved and documented.

  The initial blocking review led to ADDRESS and exact-head approval. A declared post-approval push produced the actual head-mismatch refusal and RE-REVIEW; the separate base PR merge produced the actual stale-base refusal and conflict-free REBASE. Final head `c2c89065f48ab9a032cd3fcbea586b21404dbbe2` passed exact-head CI and Alice SHA-bound squash-merged it as `03271ef23e042d4537fb339ebbc141b1c66a5663`. The issue closed, Bob completed CLOSE-OUT, both workers observed release, and the hub reached `done`. Injection invariants held in recorded actions; no genuine follow-up findings remained.

  [Credential-free evidence and reproduction](https://github.com/RoboNater/robo-agents/blob/main/docs/evidence/step6-20260918191713_f99578f4.md) are merged with [coordination PR #69](https://github.com/RoboNater/robo-agents/pull/69) (`5abf7dc1787af8c1ed78d85b9e27dabd70bb2a9f`), which closed #28 (narrowed to localhost, with networked E2E tracked in #70) and #29 after independent exact-head review and CI. The raw run and all clones are retained. Private branch protection remains unavailable (HTTP 403) and its decision remains open; the residual base-movement race, shared-account/recorded-path audit limits, and prompt-prewarning / review-audit attack surfaces are disclosed.

  *Done when:* met — real PR and blocking review/ADDRESS cycle, both declared disturbances and actual refusals, conflict-free rebase with exact-head CI, Alice's head-bound squash merge, verified empty follow-ups, post-merge closeout, observed worker releases and workflow done. ✔

  - [x] **Native-Windows, mixed-harness harness** (`4070cd3`, [#72](https://github.com/RoboNater/robo-agents/pull/72))

    After completion, the harness gained native Windows support (no WSL) and selectable harnesses: Codex Alice under a `codex app-server` supervisor, Claude Bob, and OpenCode Charlie via `opencode serve`/`run --attach` (`scripts/step6_launch.py`). The verifier now reads those transcript formats and Windows/Git Bash paths. Live run `20260919013155_2a972a49` (Codex `gpt-5.6-luna`, Claude Haiku 4.5, OpenCode Nemotron 3 Ultra free) completed the full choreography on [sandbox #12](https://github.com/RoboNater/robo-agents-sandbox/issues/12) / [PR #13](https://github.com/RoboNater/robo-agents-sandbox/pull/13) with no supervisor reprompts. It **failed strict verification, 54/61**, solely because Alice wrote prose before the JSON in her `step6:*` decision rationales; extracting the embedded JSON passes 61/61 (diagnostic only). It is recorded as a failed attempt in [`docs/evidence/step6-failed-20260919013155_2a972a49.md`](https://github.com/RoboNater/robo-agents/blob/main/docs/evidence/step6-failed-20260919013155_2a972a49.md) and does not change Step 6's completion. Review found and fixed Windows-relative and Git Bash cross-clone path gaps in the isolation audit before merge. ✔

---

## Step 7 — E2E networked

- [x] **Step 7 — E2E networked** (`96b6033055fb866124e23aa1161b3df7a7a7fc9a`, [#146](https://github.com/RoboNater/robo-agents/pull/146))

  Completed run `20260924210331_88a8a797` against fresh [sandbox issue #15](https://github.com/RoboNater/robo-agents-sandbox/issues/15), [work PR #16](https://github.com/RoboNater/robo-agents-sandbox/pull/16), and CI-checked unrelated [base PR #17](https://github.com/RoboNater/robo-agents-sandbox/pull/17), at coordination commit `cced6cc87ef17607beebb511cbe6a8fa9d02fe8d`. The hub, interactive Claude Alice (2.1.282 / `claude-sonnet-5`) and Codex Charlie (0.155.1 / `gpt-6-sol`) ran in WSL2 in NAT mode; Claude Bob (2.1.281 / `claude-sonnet-5`) ran natively on the Windows host and dialed WSL's `eth0` (`http://172.26.115.68:8431`), so every Bob call crossed the Hyper-V vSwitch while Charlie stayed on loopback. All 73 correlated verifier checks pass, including the 12 `step7_*` checks: Bob's recorded peer is `172.26.112.1` and Charlie's `127.0.0.1`; Bob's telemetry dialed the advertised `HUB_PUBLIC_URL`; his largest heartbeat gap was 30.045 s against `HUB_LOST_AFTER_S` = 180 with no `agent_lost`; the two `workspace_id`s were distinct and stable; a duplicate check-in with a copy of Bob's identity was refused with HTTP 409 while he was live; and each clone's uncommitted canary stayed out of the other.

  The Step 6 choreography repeated unchanged: blocking review `r1-1` and ADDRESS, the declared head push and actual head-mismatch refusal with RE-REVIEW, the base PR merge and actual stale-base refusal with conflict-free REBASE. Final head `d856d68b7820eb84d929bdebf0b82e6b33a13809` passed exact-head CI and Alice SHA-bound squash-merged it as `6ecc8329e1059832b7ff366eb703d4e6a9fbe7d5`. The issue closed, Bob completed CLOSE-OUT, both workers observed release, and the hub reached `done`. The first verify passed 70/73, failing only the reviewer-comment identity's capitalization; [#145](https://github.com/RoboNater/robo-agents/pull/145) (`5cbc989d9e4170b376c682b7230344d6a1b94d90`) fixed the verifier and re-verification of the unchanged run passed 73/73. It was the only measured attempt, so there are no failed-attempt notes.

  [Credential-free evidence](evidence/step7-20260924210331_88a8a797.md) covers the SHAs, the Step 7 checks and the disclosed limits. The harness is [#140](https://github.com/RoboNater/robo-agents/issues/140), merged in [#144](https://github.com/RoboNater/robo-agents/pull/144) (`cced6cc87ef17607beebb511cbe6a8fa9d02fe8d`); the run and its evidence are [#141](https://github.com/RoboNater/robo-agents/issues/141), merged in [#146](https://github.com/RoboNater/robo-agents/pull/146). The prerequisite #126 peer-address recording (schema v12, [#138](https://github.com/RoboNater/robo-agents/pull/138)) passed its Windows live acceptance in [#139](https://github.com/RoboNater/robo-agents/pull/139) (`d450647718a1f5ebe9389a51b6d36624bdfb3f12`): the first native-Windows `--worker-only` render and check-in across the vSwitch. Limits: one physical machine, NAT mode, launch and collect through WSL interop, only Bob remote, and no link disruption; multi-machine hardening is deferred to [#127](https://github.com/RoboNater/robo-agents/issues/127).

  *Done when:* met — the Step 6 criteria plus boundary crossing, live heartbeats and the #70 identity, duplicate-refusal and uncommitted-isolation checks, all passing on one networked run. ✔

---

## User Documentation (#73)

- [x] **#73 — Document user-facing usage for own repos** (`f192331`, [#74](https://github.com/RoboNater/robo-agents/pull/74))

  Added `docs/user-guide.md` documenting end-to-end orchestration on arbitrary user repositories, covering architecture, workspace bootstrapping with `scripts/bootstrap-workspace.py`, external MCP configuration outside clones to preserve clean working trees and prevent secret leakage, run-local `CODEX_HOME` with linked credentials and sandbox network access, unattended vs interactive worker loops, Alice kickoff prompt and policy options, full lifecycle routing, and clean process shutdown. Linked from `README.md` and `runtimes/README.md`. ✔

---

## User Run Preparation (#75, #76)

- [x] **#75 — Windows JSON config paths: backslash escaping rule** (`75dbdc5`, [#81](https://github.com/RoboNater/robo-agents/pull/81))

  `runtimes/README.md` recommends forward slashes and shows the escaped `C:\\work\\robo-agents` form: an unescaped backslash either fails to parse (`\w`) or silently corrupts the value (`C:\n\robo-agents` parses to `C:` + newline + carriage-return + `obo-agents`, a plausible-looking path rather than obvious garbage). `docs/user-guide.md` carries the same note where Windows paths first appear (Steps 2–4: `HUB_STATE_DIR`, `HUB_WORKSPACE`, `HUB_TELEMETRY_LOG`, `--mcp-config`), plus Git Bash `/c/` spellings, bootstrap drive-letter case, `uv` on the runtime PATH, and a troubleshooting row for paths the config sets but the hub/`uv` reports missing. Docs only; the JSON parse table was reviewer-verified. ✔

- [x] **#76 — `scripts/prepare-run.py` generates a whole user run directory** (`75dbdc5`, [#81](https://github.com/RoboNater/robo-agents/pull/81))

  One command bootstraps both clones (never touching existing ones), reuses `hub-state/token`, renders `alice.mcp.json` / worker configs / run-local Codex home from the `runtimes/` templates with CLI-probed harness versions, links Codex `auth.json`, renders worker prompts, checks `gh` auth / harness versions / merge-method permission / CI workflows (deciding `allow_no_ci`), and prints paste-ready launch lines plus the Alice kickoff prompt. Generic rendering lives in shared `scripts/run_common.py` (imported by `step6.py`, no behavior change); harness flags default to the mixed pair but were narrowed during review to `claude-code` and `codex` only, with the opencode and gemini renderers removed rather than left half-supported (opencode needs its serve/attach supervisor; gemini's flags are unverified). Reruns are idempotent and nothing is written inside either clone. `docs/user-guide.md` gained a quickstart with the manual Steps 1–5 kept as an appendix. Acceptance: throwaway repo `RoboNater/acceptance-pr81` issue #1 → PR #2 → squash-merged with matching reviewed head, issue auto-closed, workflow done — via the guide's all-Claude topology, with both workers driven through `supervise-claude-code.sh` and Alice as repeated `claude -p` turns rather than the printed interactive commands. That run exposed two fixed supervisor defects (`=` flag forms for `claude -p`; full JSON short escapes for CRLF prompts) and a Windows-specific Codex 0.155.1 defect (on Windows an extra `--add-dir` withholds the hub MCP tools, while without it `.git` stays invisible to the sandbox; not reproducible on Linux with the same CLI version), tracked in [#84](https://github.com/RoboNater/robo-agents/issues/84). ✔

---

## Settled Architectural Decisions

| Decision | Settled by |
|---|---|
| Which second runtime (Codex vs Gemini CLI) | **Settled by Step 4: Codex CLI (`charlie`)**; observed MCP tool timeout 300 s by default, recorded in §8 by #26 (#39) |
| Whether workers cache fetched role guides locally | **Settled by #5: no cache, fetch per call** |
| Alice's behaviour on "no checks reported" | **Settled by #6 (#20): presence-first bucket evaluation, bounded poll, Actions API** |
| Sandbox repo URL | **Settled by #62 (#63):** private [`RoboNater/robo-agents-sandbox`](https://github.com/RoboNater/robo-agents-sandbox), with its operating plan in `docs/plan-for-the-sandbox-repo.md` |
| Seeded sandbox issue number | **Settled by Step 6 (#69):** fresh [sandbox issue #9](https://github.com/RoboNater/robo-agents-sandbox/issues/9), generated from the versioned scenario; acceptance run `20260918191713_f99578f4` |


---

## PoC Status Summary (as of Step 7 / #147)

**Status:** Steps 1–4 are done — including the role-guide route folded into Step 2 by #5, scaffold defects (#3, #4), #8 spec amendment, stdout reservation in #7, Windows test compatibility in #18, CI check handling in #20, hub metadata namespacing (#22, #34), the plan reorder & PoC success statement (#33, #21, #35), and the PoC threat model & per-agent credentials documentation (#32, #36). The hub exposes its complete HTTP and MCP surface with reserved stdout and full A2A protocol semantics. **Step 4A is complete**: #26 (worker identity profile, schema v3) in #39, #23 (typed results & idempotent mutations, v4) in #38, #24 (background heartbeat and worker instance identity, v5) in #48, #25 (durable event delivery with implicit ack, v6) in #49, and #27 + #41 + #50 (SHA-bound merge gate, stale-base detection with the REBASE step, and late acks, v7) in #47. The hub now refuses to merge anything but an approved head on a current base with green CI, and Alice can no longer lose an event by taking longer than its lease. The relay-mode orchestration trial (#23, #26, #32, #33) produced #40–#44: the counter-reservation rule (#40, #46) is documented and in force, #43 checked in the `alice-relay` skill and trial notes (#45), #42 landed with Step 5A (#64), and #44's §9 paragraph can land any time (the role's design is post-PoC). The four-round review of #49 then produced its own hardening, merged in #58 (`6f6d823`): #53 (AGENTS.md process rules), #54 (migration fixtures dumped from real schemas), #55 (the redelivery property harness) and #59 (schema **v8**, where a migrated database and a fresh one finally converge exactly). **Step 4B — worker endurance (#30) — is complete** in #60 (`80b05c9`): both real runtimes completed the gate, Claude uses a thin policy-free supervisor, and the evidence/reproduction guardrails passed independent review. **Step 5 is complete:** Step 5A — contracts and reference harness — merged in #64 (`b79f93f`); Step 5B — runtime guides, prompts, and the Alice skill — merged in #66 (`5dbbafa`); and Step 5C — the integrated acceptance demo — merged in #67 (`6cf9f91`). Issues #51 and #52 also closed with #67. **Step 6 is complete:** [#69](https://github.com/RoboNater/robo-agents/pull/69) (`5abf7dc1787af8c1ed78d85b9e27dabd70bb2a9f`) merged the isolated-workspace implementation and real-worker localhost acceptance evidence, closing #28/#29 (with networked verification tracked in #70). All 61 correlated requirements passed; [sandbox #9 / PR #10](https://github.com/RoboNater/robo-agents/blob/main/docs/evidence/step6-20260918191713_f99578f4.md) completed with both observed worker releases. Afterwards, [#72](https://github.com/RoboNater/robo-agents/pull/72) (`4070cd3`) added native-Windows, mixed-harness support (Codex Alice, Claude Bob, OpenCode Charlie); its live run failed strict verification 54/61 only on rationale formatting, tracked in #71. **User documentation (#73) is complete** in [#74](https://github.com/RoboNater/robo-agents/pull/74) (`f192331`), adding `docs/user-guide.md` with end-to-end guidance for orchestrating arbitrary repositories, external MCP configuration, run-local runtime environments, and workspace bootstrapping. **Step 7 is complete:** [#144](https://github.com/RoboNater/robo-agents/pull/144) (`cced6cc`) landed the networked WSL2-hub / Windows-Bob harness (#140, building on #71 in [#86](https://github.com/RoboNater/robo-agents/pull/86), #125 in [#132](https://github.com/RoboNater/robo-agents/pull/132), #133 in [#134](https://github.com/RoboNater/robo-agents/pull/134), and #126 in [#138](https://github.com/RoboNater/robo-agents/pull/138)/[#139](https://github.com/RoboNater/robo-agents/pull/139)), [#146](https://github.com/RoboNater/robo-agents/pull/146) (`96b6033`, after the [#145](https://github.com/RoboNater/robo-agents/pull/145) verifier fix) merged the 73/73 networked acceptance evidence on [sandbox #15 / PR #16](evidence/step7-20260924210331_88a8a797.md) (#141), and [#147](https://github.com/RoboNater/robo-agents/pull/147) (`2416430`) closed #70 and #128 (#142). Step 8 (#31) and Step 9 remain open. **The sandbox substrate is ready** from #62/#63: the private repository, baseline project, CI, and operating plan are provisioned; fresh Step 6 issue #9 and Step 7 issue #15 are now settled; the private-repository branch-protection choice remains open. **Recommended next sequence:** Step 8 (#31 recovery matrix), then Step 9 (CI / merge polish). Other open follow-ups do not block the PoC: #17 and #44 can land any time, #61 before the next Step 4B rerun, and #57 is post-PoC.

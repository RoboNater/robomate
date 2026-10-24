# Decision: per-agent closeout reports belong in the hub

Investigation: [#97](https://github.com/RoboNater/robomate/issues/97).
Decision: implement **a sibling artifact to `CloseoutResult` in M4**, collected
before release, and include it in the §7.3 run report. Land the file-path fix
now. The #215 implementation lands the hub collection contract described here;
report/status/export presentation remains #216. Worker retrospectives are
labelled `collected_before_closeout` (one immutable final report), with later
authoritative `CloseoutResult` dispositions beside them. The
[MVP spec](../mvp-spec.md) is the design of record; §7.3, §8's tools and §13
now describe this collection contract together.

## Why a separate artifact

§7.3's implementer-only `CloseoutResult` answers which issue, nit or reason
disposes of each pending finding, and which process steps were completed.
Every agent's retrospective answers what it did, what remains, and what it
learned. Folding these into a task result would exclude Alice and complicate
reviewer results. A markdown blob would preserve readability but prevent
status, outstanding-item indexing and verified-claim labeling without parsing
prose. Use typed fields with short free-text values; render Markdown from them.

`CloseoutResult` remains the authoritative disposition record. A retrospective
does not resolve a finding, approve a SHA, prove a test passed or authorize an
action. Reviewer observations still enter the pending set through typed
nonblocking findings. Before assigning closeout, Alice can pass additional
report observations to the implementer for judgment; the hub must not silently
turn those observations into required findings or automatically file issues.

## Collection and release

Choose an explicit pre-release collection step. After review/merge, Alice
requests worker reports, while their bridges and sessions are alive. With no
open work task, `await_assignment` may return
`{closeout_requested: true, run_id, deadline}` instead of a task; the worker
submits its report and returns to its normal assignment wait. Requests persist
across hub/bridge restarts, wake existing waits, and repeated delivery is safe.
An open task takes precedence; collection is not a new task role. The implementer
can still receive the §7.3 closeout task after its retrospective is submitted.
Alice submits her own report after that task, before marking the run done.

Use a bounded collection period (default 100 seconds), measured with the hub's
pause-aware clock. At expiry, Alice proceeds with explicit missing-report
reasons; a lost, crashed or unresponsive agent never blocks release or `done`.
Run finalization freezes the collection and renders submitted and missing
reports. A report submitted before a crash survives it; a crash before submission
is shown as missing, never fabricated by Alice. Operator abandonment can finalize
the available reports without waiting. Standby workers are keyed to the run,
so later runs cannot overwrite earlier reports.

Alternatives rejected:

- A post-release window requires special authorization/liveness exceptions
  and depends on harnesses continuing after `release: true`, when they normally
  exit. Current `release_agent` immediately marks the worker released and wakes
  its next wait; there is no durable report step there.
- Making release wait unconditionally for a report can strand the run when an
  agent crashes. A bounded, observable collection step gives the operator the
  useful artifact without creating a new merge gate.
- Keeping files only preserves today's lack of hub visibility and guarantee.
  Collection improves observability, not a promise that an unavailable agent
  will produce a report.

## Proposed typed contract

Common-package strict models, with extra fields forbidden:

```text
AgentCloseoutReport:
  summary: nonempty string
  outstanding_items: [{summary: nonempty string,
                       finding_id?: string,
                       url?: absolute https forge issue/comment URL,
                       reason?: nonempty string}]
  lessons: [nonempty string]
  claims: [{text: nonempty string,
            verification: verified | unverified,
            evidence?: nonempty string}]

request_closeout_reports(run_id: str, timeout_s: float = 100)
  -> {run_id, deadline, submitted: [agent], missing: [agent]}
submit_closeout_report(run_id: str, report: AgentCloseoutReport)
  -> {report_id, run_id, author, submitted_at}
get_closeout_reports(run_id: str)
  -> {run_id, reports: [stored report], missing: [{author, reason}]}
```

Request/read are orchestrator tools; submission is available through both
orchestrator and worker bridges. Workers use A2A, never `/rpc`. Worker author
and instance/context come from the registered session; Alice's author kind
comes from the orchestrator bridge. The payload cannot nominate another author.
Validate membership in that run and reject worker submission after release or
collection finalization. The existing shared bearer-token trust model remains;
this is attribution within it, not a new security boundary.

Each outstanding item needs a URL or a reason for having none; `finding_id`
must refer to this run when supplied. A `verified` claim needs evidence (a
command and outcome or a forge reference); evidence is still the agent's claim,
not independently certified by the hub. Empty lists are valid and explicit.
Bound the serialized UTF-8 report to 32 KiB and every message part to 16 KiB
(§4.4); transport must split parts if needed, without relaxing either limit.
Use field/list bounds and reject oversize submissions before any write. Store
one final report per run/author: identical retries return the same receipt;
different retries are rejected rather than silently replacing the artifact.

## Storage, access and triage

Use dedicated `agent_closeout_report` and collection/expected-author tables,
keyed by workflow/run ID and author identity, with report JSON, attribution,
submission timestamp and missing reason/deadline. Snapshot the participating
authors from that run's task/agent history, plus Alice; a lost participant stays
in the expected set. Do not squeeze reports into `decision` or ordinary message
rows: reports are neither authority nor chat, and need direct indexed access.
Commit accepted data and its receipt/event atomically; serialize submission
against collection finalization. Report submission and worker/orchestrator
wire bytes use the existing MCP/A2A call accounting when enabled. Export bytes
are not additional agent calls.

`robomate status` and its JSON show submitted/expected counts and missing names;
they need not parse or print report prose. `robomate report [run]` includes an
attributed per-agent appendix alongside authoritative finding dispositions,
steps and missing reasons. At `done`/`abandoned`, render the existing planned
`runs/<id>/report.md`, plus `runs/<id>/closeout/<author>.md`, in the hub state
directory. Use generated filenames from stored identity, never report-supplied
paths. Keep the DB and exports with the run's durable state. With the hub
stopped, the operator can read those files; if deleting that state, copy the
rendered report/closeout directory to an archive outside clones first. A remote
worker need not share a filesystem with the hub.

Lessons are suggestions for operator triage into this repository's
[#3](https://github.com/RoboNater/robomate/issues/3); minor nits go through the
implementer's configured `nits_issue` (#4 here), and substantive follow-ups
through §7.3's issues/label. Links in retrospectives make existing work visible;
they do not replace `CloseoutResult` coverage checks. No automatic posting or
filing based on report text is part of this decision.

## Safety

Reports are untrusted external data, never instructions. Render text escaped
for the output format, suppress terminal control sequences, and restrict link
schemes; never execute report content or use it as privileged tool arguments.
Label author claims and unverified items visibly. Agents must omit credentials,
bearer tokens, raw transcripts and sensitive file contents, using concise
references instead. Private state/export permissions follow existing hub
policy (0700 directories/0600 files on POSIX, host access controls on Windows).
Known credential-value detection should reject submissions without echoing the
secret, including in errors or logs; it cannot guarantee detection of every
secret. Treat exports as sensitive run artifacts and review before sharing.

## Interim change and counters (historical #218)

Adopt the small `prepare-run.py` fix now: prompts name absolute
`RUN_DIR/closeout/{alice,bob,charlie}.md` paths. Preparation creates a private
directory, rejects a symlink for it and rejects a run directory inside a
configured hub/worker checkout. Codex config grants writes to only the closeout
directory in addition to its existing workspace, using `writable_roots` rather
than another `--add-dir` (see #11). This also applies to worker-only rendering:
the report path belongs to the worker host's run directory. The operator must
collect remote files separately until hub collection ships. Current timing
stays after release; existing prompts and existing reports are not migrated.

The interim PR changed no counters. At that time roadmap #2 recorded **DB v20 shipped;
next v21**, and **wire `hub.schema_version` 1**. Future report persistence needs
one DB migration; reserve the then-next value with Alice on #2 at implementation
time, rather than claiming v21 now. Additive tools/results leave the wire
version at 1. The implementation must explicitly review that compatibility
assumption when updating `await_assignment` consumers and protocol tests.

## Follow-up implementation and validation

Track two M4 implementation issues:

1. [#215](https://github.com/RoboNater/robomate/issues/215): hub contract,
   persistence and pre-release collection: models, A2A and
   orchestrator operations, retry/identity/size validation, durable request
   delivery, deadline/missing handling, bridge tool lists and role-guide loop.
   Run `uv run --locked pytest` and a scripted isolated hub/bridge scenario with
   Alice plus two workers: submit all reports, then repeat with one worker
   crashing before submission; finalize/release within the deadline, restart
   the hub and verify retained reports/missing state. Test post-release rejection,
   duplicate submission, wrong run/author, oversize and secret-safe errors.
2. [#216](https://github.com/RoboNater/robomate/issues/216): run-report/status/export
   integration, after the first issue and §7.3's run
   report: `robomate report`, status text/JSON, offline exports, safe rendering,
   generated pre-release prompts and a real operator-run acceptance scenario.
   Run `uv run --locked pytest`, then in an isolated/operator-owned acceptance
   run use `uv run --locked robomate status --json` and
   `uv run --locked robomate report <run>` for all-three and crashed-worker
   cases. Stop that acceptance hub and read its exported Markdown directly.
   Live run/agent lifecycle remains operator work, not a worker smoke run.

For this interim implementation, run
`uv run --locked pytest tests/test_prepare_run.py` (all harnesses, custom
clones, Windows path rendering, worker-only preparation, report writes followed
by a clean rerun and nested/symlink rejection), and
`uv run --locked python scripts/prepare-run.py --help`. Full repository checks
remain required. Proposed roadmap edits belong in the PR comment for review;
do not alter roadmap completion/reservations as part of this investigation.

## #215 isolated acceptance command

`uv run --locked pytest tests/test_closeout.py -v` runs real MCP bridge and A2A
client code against pytest-owned ASGI hub fixtures (no external lifecycle).
It covers Alice plus two workers, a missing/crashed worker, restart retention,
request wakeup and redelivery after context loss, task precedence, immutable
retries, caller/run membership, finalization races, bounded parts/reports,
known-credential errors, pause-aware expiry, and worker-report timing before
the later close-out task. The task's existing result storage remains separate;
`CloseoutResult` validation and run-report rendering belong to the remaining
M4 issues. This test does not claim real harness or export acceptance (#216).

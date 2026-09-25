# Step 6 localhost acceptance

The versioned scenario is `scenarios/step6-localhost-untrusted.json`. It requires
interactive Claude Alice, supervised Claude Bob, and unsupervised Codex Charlie.
The control session launches these independent runtimes; Alice only assigns
through the hub. The driver may write the declared head canary and unrelated
base PR, and may never review or merge the work PR for an agent.

Use persistent storage outside this checkout for the measured run. Generated
configs contain the bearer token. Tokens, raw transcripts, the SQLite database,
authentication symlink, and all three clones stay in that private run directory.
Never commit them. Authentication and first-use workspace trust must be settled
before measurement; do not use permission-bypass flags.

Before consuming a sandbox issue, run:

```sh
uv sync --locked --all-packages --dev
uv run --locked ruff check .
uv run --locked mypy
uv run --locked pytest
```

The tests execute bootstrap and preparation with disposable local repositories,
all launchers with fake runtimes, and the driver/verifier entry points. Shell
scripts must also pass `bash -n`. Read every edited function in its final form.

Pin exact models using `STEP6_ALICE_MODEL`, `STEP6_BOB_MODEL`, and
`STEP6_CHARLIE_MODEL` before preparation. Defaults are the previously proven
`claude-sonnet-5` and `gpt-5.6-sol`. Actual CLI versions and model IDs are recorded
in the manifest and generated configurations. Verify the configured models with
harmless CLI calls before seeding. `--approve-for-me` already selects
workspace-write in Codex 0.154.0 and cannot be combined with `--sandbox`.
Its run-local config allows network access for GitHub and hub HTTP; the six
worker MCP tools are approved individually. Charlie also receives the narrow
`--add-dir <own-clone>/.git` writable grant: Codex otherwise protects Git metadata
even under workspace-write. A disposable real-Codex fetch/checkout preflight
verified this grant without a sandbox bypass. Bob's supervisor supplies a fixed
continuation prompt and makes no workflow decisions.

```sh
pgrep -a hub
claude --version
claude auth status
codex --version
codex login status
gh repo view RoboNater/robo-agents-sandbox --json viewerPermission,squashMergeAllowed,mergeCommitAllowed,rebaseMergeAllowed
gh workflow list --repo RoboNater/robo-agents-sandbox
scripts/prepare-step6-demo.sh /absolute/persistent/step6-attempt --seed
```

Preparation refuses a measured run directory, creates independent full clones
and owner-only identity files, checks cross-clone uncommitted isolation, renders
trusted prompts, then creates a fresh issue last. It records the issue atomically
and adds an identified reservation comment to coordination roadmap #2 without
marking Step 6 complete. Setup failures are preserved; no clone is force-reset
or deleted. A new measured attempt uses a fresh directory and namespace.

Start these in separate long-lived PTYs and capture output beneath the run root:

```sh
scripts/launch-step6-alice.sh /absolute/persistent/step6-attempt
scripts/launch-step6-bob.sh /absolute/persistent/step6-attempt > /absolute/persistent/step6-attempt/bob.transcript.jsonl 2> /absolute/persistent/step6-attempt/bob.stderr
scripts/launch-step6-charlie.sh /absolute/persistent/step6-attempt > /absolute/persistent/step6-attempt/charlie.transcript.jsonl 2> /absolute/persistent/step6-attempt/charlie.stderr
scripts/run-step6-disturbances.py /absolute/persistent/step6-attempt > /absolute/persistent/step6-attempt/driver.stdout 2> /absolute/persistent/step6-attempt/driver.stderr
```

Alice launches the hub through her stdio MCP server. Wait for `/healthz`, inspect
`get_state`, and verify the exact durable goal/policy before launching workers.
Confirm both profiles and distinct workspace IDs before she pairs them. Answer
only authentication/trust UI before measurement; never reprompt workers to do
workflow work. Bob's transport supervisor may repeat its fixed continuation.

The first draft normalizer strips whitespace but does not fold case. Charlie
inspects/tests its exact SHA and records `changes_requested`, finding `r1-1`
with tag `STEP6-NORMALIZE-001`. Bob's ADDRESS adds casefold and a regression test.
Charlie approves that new head. The driver waits for that exact approval and CI,
pushes one run-specific head canary, then waits for Charlie's approval at the
moved head. Alice records the actual head-mismatch gate and assigns RE-REVIEW.
The driver then opens and CI-checks an additive unrelated PR and squash-merges
it with a head match. Alice records the actual stale-base gate, assigns Bob
REBASE, waits for exact-head CI, and alone performs the work PR's SHA-bound
squash merge. All state/checkpoints and failed attempts remain intact.

Alice's trusted prompt requests actual gate JSON under `step6:gate:head`,
`step6:gate:base`, and `step6:gate:final` decision keys. These are observations,
not asserted expected results. It also requests a post-merge CLOSE-OUT task,
follow-up references or a verified empty list, and release decisions after
WRAP-UP. The driver never writes these audit records.

After done and both workers have observed release:

```sh
scripts/verify-step6-demo.sh /absolute/persistent/step6-attempt
```

The verifier reads a consistent SQLite snapshot, durable GitHub issue/PR/comment,
commit and exact-SHA check-run facts, and executed tool calls from transcripts.
It retrieves Alice's session JSONL from the standard Claude projects directory
using the session ID recorded before launch. Preserve that source or copy it to
`alice.transcript.jsonl` before verification if using a different Claude config
root. The verifier emits private `github-facts.json`, `hub-audit.json`,
`tool-audit.json`, and `evidence.json`, and exits unsuccessfully on any absent or
contradictory requirement. Workspace-access conclusions concern recorded tool
calls; the shared-account/full-clone arrangement is a guide boundary, not an OS
filesystem or GitHub identity separation. Do not infer success from a canary
being present alone.

Commit only explicitly redacted manifest/evidence/audit extracts and a narrative
under `docs/evidence/`. Include exact reproduction commands, runtime versions,
all issue/PR/commit URLs, retries and deviations. Disclose unavailable private
branch protection and the residual base-movement race. Do not change repository
visibility or account tier as part of a run.

If a rail escalates, diagnose without impersonating an agent. Do not manually
push Bob's fixes, review for Charlie, or merge for Alice. Stop a failed measured
attempt, preserve it, fix the cause, and start a fresh namespace. Driver restart
uses its checkpoints and GitHub facts and refuses ambiguous movement.

Open the coordination PR with `Closes #28` and, only after successful real-worker
injection proof, `Closes #29`. Wait for CI and independent exact-head review.
Changes to behavior, launch policy, driver sequencing, or verifier requirements
normally require a fresh live proof. Phase 7 squash merge and roadmap completion
are separate authorized actions: mark Step 6 complete only after the coordination
PR merges, citing its SHA and sandbox evidence; leave Steps 7/8 and the branch
protection choice open. Stop the hub listener and CLI children belonging to this
run, leaving other checkout listeners and all workspaces intact.

The checked-in export command validates the successful verifier output, excludes
raw messages/configurations, replaces the private run root with `/RUN`, checks
all artifacts for the bearer token and recognized credential formats, and refuses
to overwrite an earlier export:

```sh
python3 scripts/step6.py export /absolute/persistent/step6-attempt --destination docs/evidence
```

Review the five JSON artifacts before committing and add the concise narrative.
Before issue creation, `setup.json` checkpoints in `preparing` or a seeded
`ready` phase allow retries in place with the
same run ID, token and clone identities. A ready/measured directory is never
reused. If issue creation has started but its response is ambiguous, preserve
that namespace and reconcile GitHub rather than blindly creating another issue.

For an Alice restart, stop only this run's orphaned listener if it remains,
then invoke the same Alice launcher. It detects the recorded session JSONL and
uses `--resume` instead of attempting to recreate that session ID. The durable
hub goal/policy and completed tasks remain authoritative. A custom
`CLAUDE_CONFIG_DIR` is recorded at preparation and used for Alice's launcher and
transcript collection. The implicit default leaves that variable unset: explicitly
setting it relocates Claude's main configuration file. Worker restarts retain
clone IDs but get fresh process
instance IDs; a still-live old instance is intentionally rejected until its
loss is observed and Alice reconciles the assignment. Do not delete rows or
reset a clone to force recovery. Crash scenarios remain Step 8 work.

Gate proof binds the invocation and returned report to the same work PR.
Worker-action auditing tokenizes visible shell commands, including global Git
options, shell wrappers, and relative paths after `cd`. It cannot establish
arbitrary program behavior or an OS filesystem boundary. GitHub's timestamps
represent one-second intervals; subsecond hub events are compared for compatible
ordering rather than invented timestamp precision. Extra merge options such as
`--admin` and `--auto` fail verification.

For every REVIEW or RE-REVIEW, Charlie's trusted launch prompt requires fetching
and checking out the assigned head in his own full clone, then running
`scripts/step6-review-check.py <absolute-charlie-clone> <assigned-full-sha> <run-id>`
through its absolute coordination-checkout path. The helper runs the standard
unittest command in that clone and records its actual identity, before/after
HEAD, test return code, clean state, source commit, and UTC interval under
`.git/step6-review-audit.jsonl`. The verifier correlates each record with the
successful executed Codex command result and the exact review task interval.
An API archive or temporary test snapshot cannot substitute for this proof.
If the helper fails, Charlie must ask Alice rather than invent a workaround.

## Native Windows and alternate harnesses

Alice may run on Codex and Charlie on OpenCode by setting these before
preparation (defaults remain Claude Alice/Bob and Codex Charlie):

```sh
STEP6_ALICE_HARNESS=codex STEP6_ALICE_MODEL=gpt-5.6-luna
STEP6_BOB_MODEL=claude-haiku-4-5-20251001
STEP6_CHARLIE_HARNESS=opencode STEP6_CHARLIE_PROVIDER=openrouter
STEP6_CHARLIE_MODEL=openrouter/nvidia/nemotron-3-ultra-550b-a55b:free
STEP6_PYENV_VERSION=3.12.10        # pyenv-win python3 shim for every clone
STEP6_SKIP_ROADMAP_RESERVATION=1   # when not authorized to comment on #2
PYTHONUTF8=1
uv run --locked python scripts/step6.py prepare 'C:\absolute\run' --seed
```

Launch with `scripts/step6_launch.py {alice,bob,charlie} RUN_DIR` in separate
processes (Alice first), then `scripts/run-step6-disturbances.py RUN_DIR`. Codex
Alice runs under a `codex app-server` supervisor so one process keeps the hub
alive across turns; OpenCode Charlie runs `opencode serve` with `run --attach`
turns so his `worker-mcp` instance persists. Supervisors send only fixed
continuation text. OpenCode has no OS sandbox: Charlie's generated config denies
edits and external directories and allows a narrow shell list. See
[the Windows attempt](evidence/step6-failed-20260919013155_2a972a49.md).

## Networked topology (Step 7)

The same harness runs the Step 7 topology from
[`docs/plan-for-step-7-and-issue-70.md`](plan-for-step-7-and-issue-70.md)
(#140). The hub, Alice and Charlie run in WSL2 (NAT mode). Bob runs natively on
the Windows host and dials WSL's `eth0` address. The helpers live in
`scripts/step7.py`, and `scripts/step6.py` stays the entry point. The port comes
from `--hub-port`; pick one no other checkout's hub holds.

```sh
# From WSL. The Windows checkout must be clean and at this checkout's commit,
# with `uv sync --locked --all-packages` run there.
uv run --locked python scripts/step6.py prepare /abs/run \
  --scenario scenarios/step7-networked-untrusted.json --hub-port 8431 \
  --windows-run-dir C:/work/step7-run --windows-checkout C:/work/robo-agents \
  [--local-repository /abs/sandbox-clone | --seed]
```

`prepare` reads `eth0` and renders a hub that binds `0.0.0.0:<port>` and
advertises `http://<eth0>:<port>`. Charlie dials `127.0.0.1:<port>`. The
preflight then runs through WSL interop and records its result in `run.json`:

- Git Bash and `curl.exe` are present.
- The Windows checkout is clean and at this checkout's commit.
- The port is free.
- From Windows, `curl.exe` against a throwaway hub gets `/healthz` and an agent
  card whose `url` is `HUB_PUBLIC_URL` + `/a2a`.

Any failure stops `prepare` before Bob is rendered or an issue is created.
Bob's clone and config then come from `prepare-run.py --worker-only bob`, run
natively on Windows, which bootstraps the clone with Windows git. It reads the
hub token in place over `//wsl.localhost` and writes it only into
`configs/bob.mcp.json`. Each clone gets an uncommitted, git-ignored canary.
`prepare` prints the launch lines. Run them from WSL in this order:

```sh
scripts/launch-step6-alice.sh /abs/run
scripts/launch-step6-charlie.sh /abs/run
uv run --locked python scripts/step6_launch.py bob /abs/run   # Bob's supervisor runs on Windows
uv run --locked python scripts/run-step6-disturbances.py /abs/run
uv run --locked python scripts/step6.py verify /abs/run
uv run --locked python scripts/step6.py export /abs/run --destination docs/evidence
```

Besides the disturbances, the driver samples Bob's hub row until he is
released. While Bob holds a task, it checks in once from a second clone that
carries a copy of Bob's identity file. `verify` pulls Bob's transcript and
telemetry from Windows. It runs every Step 6 check plus these:

- **Boundary crossed:** `step7_bob_remote_peer`, `step7_charlie_loopback_peer`
  and `step7_bob_dialed_public_url`.
- **Live heartbeats:** `step7_heartbeat_gaps`, measured on Bob's own clock from
  each assignment to its result; `step7_no_agent_lost`; and
  `step7_hub_heartbeat_advanced`, during Bob's longest task.
- **#70 identities:** `step7_distinct_workspace_ids` and
  `step7_stable_workspace_ids`.
- **#70 duplicate refused:** `step7_duplicate_refused`, which needs HTTP 409
  while Bob is live and Bob unaffected.
- **#70 uncommitted isolation:** `step7_canary_isolation` and
  `step7_{bob,charlie}_no_cross_host_access`. Each worker's calls are matched
  against every spelling the other clone has on that worker's host.

`export` writes `step7-<run_id>.*.json`. It masks the WSL and Windows run roots
as `wsl:/RUN` and `windows:/RUN` in every recorded spelling, and Windows profile
paths as `C:/Users/<user>/...`. It keeps the private `172.16.0.0/12` NAT
addresses, which are check 1's evidence, and still refuses any credential.

# Plan for Step 7 and issue #70 — networked E2E

Status: complete 2026-09-24. PR 1 is merged (#125 via [#132](https://github.com/RoboNater/robo-agents/pull/132);
non-default ports #133 via [#134](https://github.com/RoboNater/robo-agents/pull/134)). PR 2 is merged
(#126 via [#138](https://github.com/RoboNater/robo-agents/pull/138), Windows live acceptance
[#139](https://github.com/RoboNater/robo-agents/pull/139)). PR 3's harness
[#140](https://github.com/RoboNater/robo-agents/issues/140) is merged via
[#144](https://github.com/RoboNater/robo-agents/pull/144), and the acceptance run
[#141](https://github.com/RoboNater/robo-agents/issues/141) passed 73/73 with its evidence merged via
[#146](https://github.com/RoboNater/robo-agents/pull/146). PR 4, the close-out, is
[#147](https://github.com/RoboNater/robo-agents/pull/147).
Tracking issue [#128](https://github.com/RoboNater/robo-agents/issues/128),
with prerequisites [#125](https://github.com/RoboNater/robo-agents/issues/125) and
[#126](https://github.com/RoboNater/robo-agents/issues/126), landing together with
[#70](https://github.com/RoboNater/robo-agents/issues/70). Multi-machine hardening is
[#127](https://github.com/RoboNater/robo-agents/issues/127).

## What has to be proven

Spec §7 Step 7 repeats the Step 6 acceptance run, with one change: the workers
reach the hub across a network boundary while background heartbeats run live.
[#70](https://github.com/RoboNater/robo-agents/issues/70) adds a requirement:
workspace identities and uncommitted state stay distinct and isolated across
that boundary.

The first pass uses the easiest setup that is still a genuine boundary: this
PC's WSL2 Ubuntu 24.04 plus its Windows 11 host. Running on several physical
machines (a native Ubuntu box and a second Windows PC on the LAN) is deferred
to #127.

## Two kinds of work

Alice, Bob and Charlie play a different role in each part:

1. **Building the tooling** in `robo-agents`. These are ordinary Alice runs
   (`scripts/prepare-run.py --work-file`, one statement of work per PR), with
   Bob implementing and Charlie reviewing.
2. **The acceptance run** on `robo-agents-sandbox`. Here Alice, Bob and Charlie
   are what is being tested. A control session drives the scripted Step 6-style
   choreography (disturbances, verifier, evidence export), as in Step 6. Alice
   cannot "implement" this part.

## Facts that shape the plan

- WSL runs in **NAT** networking mode, and its `eth0` address (for example
  `172.26.115.68`) changes whenever WSL restarts. The Windows host reaches
  that address over the Hyper-V vSwitch. That is a separate network stack
  with a non-loopback peer. The LAN cannot reach it, so a hub bound to
  `0.0.0.0` inside WSL exposes the token only to this PC. The §1 plain-HTTP
  trust assumption holds.
- WSL interop is enabled. A control session in WSL can start Windows processes
  (`cmd.exe`, `powershell.exe`) and read their output under `/mnt/c`, so the
  first pass needs no SSH.
- The Windows host has Claude Code, Codex CLI 0.155.1 (the version affected
  by #84), `gh`, git, and uv 0.6.2. That uv needs upgrading before
  `uv sync --locked` can be trusted there.
- Before #125, `scripts/prepare-run.py` hard-coded the worker `HUB_URL` to
  loopback, and nothing set `HUB_HOST`, so it could not produce a networked
  run. #132 and #134 added `--hub-host`, `--hub-url`, `--hub-port`,
  `--remote-worker` and `--worker-only`.
- `scripts/step6.py` hard-codes `127.0.0.1` and the Step 6 scenario path.
  `collect()` reads every transcript and Charlie's review audit from local
  paths. `scripts/step6_launch.py` already runs Claude Bob natively on Windows
  (#72).
- The hub does not record where a worker connected from, so `hub.db` alone
  cannot show that a request crossed the boundary (#126).
- Workspace uniqueness at check-in is keyed on `workspace_id`, not on the path
  (`packages/hub/src/agent_hub/store.py`). That is already correct across
  machines, where two clones may have the same path.

## Topology (first pass)

| Where | Process | Hub address used |
|---|---|---|
| WSL2 | hub + Alice (Claude Code); `HUB_HOST=0.0.0.0`, `HUB_PUBLIC_URL=http://<wsl-eth0>:8420` | — |
| WSL2 | Charlie (Codex CLI on Linux, which avoids #84) | `http://127.0.0.1:8420` |
| Windows 11 | Bob (Claude Code, native, launched through interop) | `http://<wsl-eth0>:8420` |

Every Bob call crosses the vSwitch: implement, address, rebase, close-out and
the timer heartbeats. Charlie stays local, which also shows that local and
remote workers work together under one hub. Putting Charlie on Windows is left
to #127 because of #84.

## Work breakdown

Each PR is one Alice run, and the runs go in order: the non-goal of one
workflow per hub rules out running them in parallel on one hub.

### PR 1 — prepare-run networked topology (#125) — merged

Merged as [#132](https://github.com/RoboNater/robo-agents/pull/132), with
non-default ports added by [#134](https://github.com/RoboNater/robo-agents/pull/134)
(#133). The remote worker is rendered on its own host with `--worker-only`,
because its clone and harness version must come from that host.

#132's tests and its Windows `curl.exe` preflight checks were enough for
#125. One gap is carried into PR 2's acceptance: nothing ran `--worker-only`
natively on Windows, and no worker checked in from Windows across the
vSwitch.

The original PR 1 scope:

- Separate the bind host (`--hub-host` → `HUB_HOST`), the URL workers dial
  (`--hub-url`) and `--public-url` (defaults to the hub URL). Validate them
  the way `HubSettings.from_env()` does.
- Render a worker for another host with that host's paths (checkout root,
  `HUB_WORKSPACE`, telemetry, launch line). Either add a worker-only mode run
  on the remote host, or render into a directory it can read. The implementer
  chooses and documents the choice.
- Add a preflight reachability line for the worker host, and a warning that
  the WSL address is not stable.

### PR 2 — hub records each worker's observed peer address (#126, schema v12)

DB schema v12 is reserved for this issue in roadmap #2.

- Store the peer address on the `agent` row at check-in and on every
  heartbeat, taken from the request's client address, never a header. Expose
  it in the agent snapshot.
- Worker telemetry records the `HUB_URL` it dialed.
- The spec §3 text records that this is an observation, like `declared_model`:
  never attested identity, and never used for pairing or authorization.
- Its acceptance closes the PR 1 gap: the first native-Windows
  `--worker-only` render, then a real check-in and background heartbeats from
  Windows. The hub must show Bob's Windows vEthernet address and the WSL
  worker's loopback address. The statement of work is
  `docs/notes/sow-step7-pr2-issue-126.md`, and it depends on the Windows
  operator setup below.

### PR 3 — networked acceptance harness and #70 checks (#128, with #70)

- Extend `step6.py` / `step6_launch.py` with a networked topology rather than
  copying them. Add `scenarios/step7-networked-untrusted.json`, make the
  scenario path and hub URLs parameters, and reuse `scripts/run_common.py`.
- **Prepare**:
  - read the WSL `eth0` address;
  - check that Windows reaches `/healthz` and the agent card (`curl.exe`
    through interop);
  - bootstrap Bob's clone on Windows with `scripts/bootstrap-workspace.py`;
  - render his config via PR 1;
  - refuse `--seed` while any preflight fails.
- **Launch**: Bob on Windows through interop, using the #72 path.
- **Collect**:
  - pull Bob's transcript and telemetry from the Windows side;
  - record workspace paths qualified by host;
  - record the PR 2 addresses in the hub audit.
- **New verifier checks**, on top of every Step 6 check:
  1. *Boundary crossed*: Bob's addresses are non-loopback and are not WSL's
     own address. Charlie's are loopback. Bob's telemetry `hub_url` equals
     the advertised `HUB_PUBLIC_URL`.
  2. *Live heartbeats*: the gap between Bob's heartbeats stays below
     `HUB_LOST_AFTER_S` through every task he holds, there is no
     `agent_lost`, and the hub's `last_heartbeat` for Bob advances during his
     longest task.
  3. *#70 distinct identities*: the workspace IDs differ, and each stays the
     same across its check-ins.
  4. *#70 duplicate refused*: Bob's identity file is copied into a second
     clone, which checks in (for example with `scripts/mock-worker.py`) while
     Bob is live. The result is HTTP 409, and Bob is unaffected.
  5. *#70 uncommitted isolation*: a canary uncommitted file in each clone
     never appears in the other, and neither worker's recorded actions touch
     the other's clone.

### Acceptance run (control session)

1. Do a dry run with `--local-repository`.
2. Run `prepare --seed`, launch Alice and Charlie in WSL and Bob on Windows,
   then run `driver`, `verify` and `export`.
3. The run is done when Step 6's criteria and checks 1–5 above all pass.
   Optionally enable call accounting for #99.
4. Keep failed attempts and document them, as Step 6 did.

### PR 4 — close-out

This can be an Alice run with a statement of work, or done by hand.

- Evidence doc `docs/evidence/step7-<run_id>.md`, a worklog entry and the
  roadmap #2 update.
- The PR says `Closes #70` and `Closes #128`.

### Later — multi-machine hardening (#127)

- Both workers on other physical machines (the native Ubuntu box and/or the
  second Windows PC).
- Launch and collect over SSH.
- Link disruption shorter and longer than `HUB_LOST_AFTER_S`.
- Codex Charlie on Windows once #84 allows it.
- Clock skew.
- Sequence it alongside Step 8 (#31).

## One-time operator setup on Windows

Do this before PR 2's live acceptance, which is the first to need it.

1. Upgrade uv: 0.6.2 was found, and WSL has 0.7.13. Clone robo-agents (for
   example at `C:/work/robo-agents`) at the same commit as the hub, and check
   that `uv sync --locked --all-packages` passes there.
2. Confirm `gh auth status` and Claude login on Windows.
3. Do not restart WSL during a run, because the hub's address would change.

## Verification

- For every PR, run the AGENTS.md gates (`uv sync --locked --all-packages
  --dev`, `ruff`, `mypy`, `pytest`) and run each touched script once.
- PR 1 (done; re-checked 2026-09-24 on `main`): a networked render with
  `--remote-worker bob`, and its hub bound to `0.0.0.0`. From Windows,
  `curl.exe` reached `/healthz` and an agent card advertising the rendered
  URL. An authenticated guide fetch using the token read through the
  `\\wsl.localhost` path returned 200, and without the token it returned
  401.
- PR 2: check in a worker from Windows and confirm that `agent` shows the
  Windows vEthernet address for it and `127.0.0.1` for a WSL worker.
- PR 3: run the networked dry run with `--local-repository`, then the seeded
  acceptance run. `verify` must pass every check, old and new.

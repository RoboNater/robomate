# Step 7, PR 2: hub records each worker's observed peer address (RoboNater/robo-agents#126)

Address `RoboNater/robo-agents#126` in one pull request against `main` of
`RoboNater/robo-agents`. This is the second Step 7 prerequisite tracked in
`RoboNater/robo-agents#128`. Read #128 and `docs/plan-for-step-7-and-issue-70.md`
for context only; this run delivers #126 alone. DB schema **v12** is reserved
for #126 in roadmap `RoboNater/robo-agents#2`; use it unchanged.

Background: Step 7 runs the hub, Alice and Charlie in WSL2 and Bob natively on
the Windows 11 host. Bob dials the WSL `eth0` address (NAT mode) across the
Hyper-V vSwitch. The networked `prepare-run` flags (#125, PR #132; ports #133,
PR #134) are merged. Today nothing in `hub.db` shows where a worker's requests
came from, and the hub's stderr has no access log, so no evidence can show
that a request crossed the boundary.

PR #132 met #125's own tests, but it left one gap that this run closes. It
never ran `prepare-run --worker-only` natively on Windows. Its only real
check-in came from WSL to WSL's own `eth0` address, which never leaves the VM.
This run's live acceptance therefore makes the first native-Windows
`--worker-only` render and the first real Windows worker check-in and
heartbeat across the vSwitch.

Scope (the issue has the details):
- Record the client address of each worker's A2A request. Take it from the
  Starlette request (`request.client.host`), never from a header. Store it as
  `checkin_remote_addr` at check-in and as `last_remote_addr` on check-in and
  every heartbeat. Add both columns to `agent` through a new `_migrate_*` in
  `packages/hub/src/agent_hub/database.py`, which makes this schema v12.
- Expose both columns in the agent snapshot that `get_state` / `list_agents`
  return, and in `scripts/dump-schema.py` output where applicable.
- Worker telemetry (`HUB_TELEMETRY_LOG`) records the `HUB_URL` it dialed, once
  at startup and on each heartbeat record.
- Document the columns in `docs/poc-spec.md` §3 as an observation, like
  `declared_model`. It is not attested identity and is never used for pairing
  or authorization. Behind a NAT, proxy or tunnel it is the translated address.
- Out of scope: the Step 7 harness and verifier (`scripts/step6.py`,
  `step6_launch.py`, scenarios; see #128), the #70 duplicate-identity and
  isolation probes, and `prepare-run` beyond fixes that the Windows acceptance
  below turns up. Report any such fix in the PR description with the reason.

Operator prerequisites. The operator completes these on the Windows host before
kickoff. If one is missing, the implementer asks Alice rather than working
around it:
- `uv` is upgraded to at least the WSL version (0.7.13). 0.6.2 was found.
- There is a robo-agents checkout at `C:/work/robo-agents` (or the path the
  operator names to Alice), and `uv sync --locked --all-packages` passes in it.
- `git`, `gh auth status` and `claude --version` work in Git Bash.

Acceptance criteria:
1. Unit tests:
   - `tests/test_database.py`: a v11 database migrates to v12, and the new
     columns are NULL for legacy rows.
   - `tests/test_protocol.py` / `tests/test_app.py`:
     - with the test client reporting a client address, check-in stores it in
       both columns;
     - a later heartbeat from a different address updates only
       `last_remote_addr`;
     - a header claiming an address is ignored.
   - `tests/test_telemetry.py` / `tests/test_worker_client.py`: telemetry
     records carry `hub_url`.
   - Tests that drive the app use conftest's `hub_store`.
2. `uv sync --locked --all-packages --dev`, `uv run --locked ruff check .`,
   `uv run --locked mypy` and `uv run --locked pytest` pass.
3. Live networked acceptance, run from WSL. Reach the Windows side through WSL
   interop (`cmd.exe` / `powershell.exe` / Git Bash):
   1. **Hub host.** Pick a free port; run `ss -ltn` first, because another
      checkout's hub may hold 8420. Then run `scripts/prepare-run.py
      --skip-github-checks --hub-host 0.0.0.0 --hub-url
      http://<wsl-eth0>:<port> --remote-worker bob` into a scratch run
      directory outside the checkout. Use a scratch local repository or the
      sandbox as `--repository`, and never create a GitHub issue.
   2. **Windows checkout.** Check out this PR's head in the Windows checkout
      and run `uv sync --locked --all-packages`.
   3. **Native Windows `--worker-only` (the #132 gap).** Run the printed
      `--worker-only bob` command natively on Windows. It reads the token
      through the printed `\\wsl.localhost\...` path. Confirm:
      - the rendered `bob.mcp.json` uses `C:/...` forward-slash paths;
      - Windows git bootstrapped the clone, and the identity file's `path`
        equals `HUB_WORKSPACE`;
      - `HUB_HARNESS_VERSION` is the Windows `claude --version`;
      - no token file was created on Windows.
      If `--worker-only` fails on native Windows, fix it in this PR and add a
      test.
   4. **Start the hub and run the preflight.** Start the hub from the rendered
      `alice.mcp.json` env. Run the printed `curl.exe` preflight on Windows
      and confirm the agent card advertises `http://<wsl-eth0>:<port>/a2a`.
   5. **Real check-in and heartbeats from Windows.** On Windows, using the
      rendered `bob.mcp.json` env plus `HUB_HEARTBEAT_S=5` and
      `HUB_TELEMETRY_LOG`, check Bob in and let background heartbeats run for
      at least 20 s. Use the `worker_mcp` client code path
      (`WorkerHubClient.check_in` and its heartbeat loop), not hand-built
      HTTP.
   6. **A loopback worker from WSL.** From WSL, check in a second worker
      (Charlie's rendered config, or a mock worker) over
      `http://127.0.0.1:<port>`.
   7. **What to observe.** Query `hub.db` and `get_state`, and check the
      telemetry:
      - Bob's `checkin_remote_addr` and `last_remote_addr` are the Windows
        vEthernet address: not loopback, and not the WSL `eth0` address;
      - Bob's `last_heartbeat` advances across at least three heartbeats;
      - the WSL worker's addresses are `127.0.0.1`;
      - Bob's telemetry records `hub_url` equal to
        `http://<wsl-eth0>:<port>`;
      - both workers report non-null, distinct `workspace_id`s.
   8. **Clean up.** Stop the hub and every worker process you started on both
      sides. Leave other checkouts' hubs alone.
   9. **Report.** The PR description lists the commands and their output
      (never the token), including the `agent` rows and one telemetry
      heartbeat record.

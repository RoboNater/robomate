# Testing patterns (#589)

Short recipes for recurring test situations in `tests/`: Windows process
identity and liveness, child environments and launcher preflights, nested
pytest sessions, and Windows text decoding. For clocks, condition waits, holds
and the slow-host latency probe, see
[`testing-time-and-waits.md`](testing-time-and-waits.md).

Each recipe states its **trigger**, a **working example** or a link to a
current test on `main`, and the **failure it catches**.

## 1. Windows process identity and liveness

### Venv `python.exe` starts the real interpreter as a child

- **Trigger:** a test launches a Python script or console entry point with
  `sys.executable` (or a uv console-script `.exe`) on Windows and checks the
  process ID or tracks the child for cleanup.
- **Example:** have the child record its own `os.getpid()` (and creation
  timestamp on Windows) and key PID checks on that recorded PID, as
  `CHILD_PID_RECORD` in [`tests/fake_harness.py`](../../tests/fake_harness.py)
  and `test_windows_child_records_creation_time` in
  [`tests/test_agent_launcher.py`](../../tests/test_agent_launcher.py) do — or
  accept a live child PID alongside `process.pid` when waiting for a daemon, as
  `await_hub` in [`tests/test_cli.py`](../../tests/test_cli.py) does (or launch
  with `getattr(sys, "_base_executable", sys.executable)` when a direct
  interpreter handle is appropriate):

  ```python
  parts = fake_supervision.pids.read_text(encoding="utf-8").strip().split("\t")
  recorded_pid = int(parts[0])
  assert int(parts[2]) == fake_harness._windows_creation_time(recorded_pid)
  assert fake_harness.live_own_pids(fake_supervision.pids) == [recorded_pid]
  ```
- **Failure it catches:** in a uv virtual environment on Windows,
  `.venv\Scripts\python.exe` (and any console-script `.exe`) is a small
  launcher that spawns the base interpreter as a second process. `Popen.pid` is
  the launcher shim, while `os.getpid()` inside the child is the real
  interpreter. Asserting `recorded_pid == process.pid` fails on Windows CI, and
  relying on the `Popen` handle to keep the child's PID reserved leaves the
  second process's PID unreserved once it exits (#40 / PR #57, PR #86, #473 /
  PR #476).

### `os.kill(pid, 0)` is `CTRL_C_EVENT` on Windows; use `process_alive`

- **Trigger:** a test or cleanup helper checks whether a process ID is still
  running.
- **Example:** call `agent_hub_common.registry.process_alive` (or
  `fake_harness.process_alive`, which queries `OpenProcess` +
  `GetExitCodeProcess == STILL_ACTIVE` on Windows), and test the Windows branch
  on any platform by stubbing `WinDLL` and `os.kill`, as
  `test_windows_liveness_uses_still_active_query` and
  `test_windows_supervision_never_sends_console_ctrl_c` in
  [`tests/test_agent_launcher.py`](../../tests/test_agent_launcher.py) do:

  ```python
  from agent_hub_common.registry import process_alive

  assert process_alive(pid)
  ```
- **Failure it catches:** on Windows `signal.CTRL_C_EVENT == 0`. Python routes
  `os.kill(pid, 0)` to `GenerateConsoleCtrlEvent`, which sends Ctrl-C to the
  console group and aborts the Windows CI runner with `KeyboardInterrupt`
  (#464 / PR #472).

### `os.kill(pid, SIGTERM)` exits with code 15 on Windows

- **Trigger:** a test terminates a child with `os.kill(pid, signal.SIGTERM)` (or
  `process.terminate()`), checks its exit code, or diagnoses an unexpected exit
  code `15` on Windows CI.
- **Example:** `terminate_own` in
  [`tests/fake_harness.py`](../../tests/fake_harness.py) and
  `test_windows_supervision_never_sends_console_ctrl_c` in
  [`tests/test_agent_launcher.py`](../../tests/test_agent_launcher.py) use
  specific-PID `signal.SIGTERM` on Windows; tests for POSIX graceful-SIGTERM
  handlers skip Windows explicitly
  (`test_without_auto_start_it_prints_the_starts_and_sigterm_stops_its_hub` in
  [`tests/test_start_run.py`](../../tests/test_start_run.py)).
- **Failure it catches:** Windows has no POSIX `SIGTERM` delivery;
  `os.kill(pid, signal.SIGTERM)` calls `TerminateProcess(handle, SIGTERM)`,
  killing the target immediately with exit code `15` (`signal.SIGTERM`). A test
  that expects a child's `SIGTERM` signal handler or `finally` block to run on
  Windows will fail, and an unexplained exit code `15` in Windows CI logs is a
  signature that `os.kill(..., SIGTERM)` (such as a test cleanup sweep) killed
  the process (#464 / PR #472, #475).

## 2. Testing the real child environment

### Assert the environment and profile the fake child receives, not only `argv`

- **Trigger:** a test exercises a launcher, recovery wrapper, or supervisor
  (`scripts/agent-launcher.py`, `scripts/start-run.py`) that spawns a harness or tool subprocess.
- **Example:** have the fake child dump selected `os.environ` keys, `os.getcwd()`
  and generated profile files alongside `sys.argv[1:]`, and assert what the
  child actually received — including when conflicting ambient variables are set
  in the parent environment — as `FAKE_HARNESS` and
  `test_doctor_login_and_no_inherited_hub_selection` in
  [`tests/test_agent_launcher.py`](../../tests/test_agent_launcher.py) do:

  ```python
  (launch,) = launches(area)
  env = launch["env"]
  assert env["ROBOMATE_HUB_URL"] is None and env["ROBOMATE_TOKEN"] is None
  assert env[variable] == str(profile) and profile.is_dir()
  ```
- **Failure it catches:** a launcher can compute a hub selection, strip list or
  profile path in-process and still fail to pass it to the child, or let
  ambient caller variables (`ROBOMATE_HUB_URL`, `ROBOMATE_TOKEN_FILE`,
  `CODEX_HOME`, `CLAUDE_CONFIG_DIR`) leak into an isolated or interactive doctor
  child. Checking only `argv` misses environment leaks and missing profile files
  (#247 / PR #463).

### Stub a new launcher preflight in every subprocess test

- **Trigger:** you add a host or environment preflight check (such as a sandbox
  verifier or forge preflight) to a launcher or CLI command that existing tests
  run as a real subprocess.
- **Example:** prepend a scratch directory containing a scoped
  `sitecustomize.py` to the subprocess environment's `PYTHONPATH` so the stub
  loads in every child Python process without editing production code, as the
  `smoke` fixture in
  [`tests/test_start_run.py`](../../tests/test_start_run.py) does:

  ```python
  hooks = tmp_path / "hooks"
  hooks.mkdir()
  (hooks / "sitecustomize.py").write_text(
      "import subprocess\n"
      "_run = subprocess.run\n"
      "def run(args, *a, **kw):\n"
      "    if isinstance(args, list) and args and str(args[0]).endswith('verify-sandboxes.sh'):\n"
      "        return subprocess.CompletedProcess(args, 0, '', '')\n"
      "    return _run(args, *a, **kw)\n"
      "subprocess.run = run\n",
      encoding="utf-8",
  )
  env["PYTHONPATH"] = str(hooks) + os.pathsep + env.get("PYTHONPATH", "")
  ```
- **Failure it catches:** in-process `monkeypatch.setattr` never reaches a child
  process spawned by `subprocess.run` or `Popen`, and the outer test host (or CI
  container / agent sandbox) may lack the host capability or fake binary for the
  new check, failing unrelated end-to-end subprocess tests (#498 / PR #550).

### Resolve a harness's config-directory override

- **Trigger:** code or a test inspects, writes, or isolates a harness's
  user-level configuration (`claude-code`, `codex`, `opencode`), or checks what
  counts as a registered MCP server entry.
- **Example:** resolve `CLAUDE_CONFIG_DIR` (falling back to `HOME`),
  `CODEX_HOME` (falling back to `HOME/.codex`), and `XDG_CONFIG_HOME` (falling
  back to `HOME/.config`), and test both the override and the fallback — plus,
  for Codex TOML, require `command` or `url` on `[mcp_servers.robomate]` — as
  `check` in [`scripts/mcp_preflight.py`](../../scripts/mcp_preflight.py) and
  `test_user_level_mcp_presence`,
  `test_claude_config_dir_overrides_home`, and
  `test_codex_nested_settings_alone_are_not_a_server` in
  [`tests/test_start_run.py`](../../tests/test_start_run.py) (and
  [`tests/test_check_mcp_tools.py`](../../tests/test_check_mcp_tools.py)) do.
- **Failure it catches:** launchers forward `CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
  and `XDG_CONFIG_HOME` to agents. Reading `~/.claude.json` or `~/.codex`
  unconditionally rejects a valid relocated config and accepts a stale home
  config while the agent starts without robomate tools. In Codex TOML, nested
  tables such as `[mcp_servers.robomate.tools.join]` or `-c
  mcp_servers.robomate.env...` create an implicit parent dict without a
  transport, which Codex rejects at startup if treated as a server entry (#485 /
  PR #535).

## 3. Nested pytest

### Run a nested pytest as a fresh session under the real config

- **Trigger:** a test spawns an inner `pytest` subprocess to verify conftest
  fixture isolation, environment stripping, or working-directory discovery.
- **Example:** strip inherited `PYTEST_*` and `PYTHONPATH` variables so the
  child does not rejoin the outer xdist session, copy the repository's real
  `pyproject.toml` (plus `conftest.py` and `timing.py`) and pass it with `-c`,
  give the inner run its own `--basetemp`, and bound `subprocess.run` with a
  multiple of `LIVENESS_S`, as
  `test_no_cwd_bridge_ignores_lane_clone_identity` in
  [`tests/test_workspace.py`](../../tests/test_workspace.py) and
  `test_isolated_machine_state_strips_launcher_identity` in
  [`tests/test_conftest.py`](../../tests/test_conftest.py) do:

  ```python
  env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
  env.pop("PYTHONPATH", None)
  result = subprocess.run(
      [
          sys.executable,
          "-m",
          "pytest",
          "-q",
          "-p",
          "no:cacheprovider",
          "-c",
          str(inner / "pyproject.toml"),
          "--basetemp",
          str(inner / "run"),
          str(inner / "test_inner.py"),
      ],
      cwd=inner,
      env=env,
      capture_output=True,
      text=True,
      encoding="utf-8",
      timeout=3 * LIVENESS_S,
  )
  ```
- **Failure it catches:** inherited `PYTEST_*` variables make the inner process
  collide with the outer xdist worker or temp directories; a minimal generated
  `pytest.ini` silently drops the repository's `filterwarnings` (`error` on
  `DeprecationWarning` / `PendingDeprecationWarning`, #127 / #123), markers and
  `asyncio_mode` settings (#461 / PR #466).

### Prove the warning policy reached the nested pytest

- **Trigger:** you add or edit a nested-pytest test and want to verify that the
  repository's warning-as-error policy is active inside the inner session.
- **Example:** temporarily add `warnings.warn("probe", DeprecationWarning,
  stacklevel=1)` inside the inner test body used by
  `test_no_cwd_bridge_ignores_lane_clone_identity` in
  [`tests/test_workspace.py`](../../tests/test_workspace.py) (or
  `test_isolated_machine_state_strips_launcher_identity` in
  [`tests/test_conftest.py`](../../tests/test_conftest.py)), run the outer test
  to confirm it fails on the unhandled `DeprecationWarning`, and revert the
  probe before committing.
- **Failure it catches:** an inner pytest invoked without `-c <pyproject.toml>`
  (or from a directory where pytest stops at a synthetic minimal config) passes
  even when code under test emits a `DeprecationWarning` (#461 / PR #466 r2-1).

### Probe a test from a temporary directory without touching the checkout

- **Trigger:** you want to run a throwaway diagnostic or reproduction test
  against the repository's fixtures (`conftest.py`, `timing.py`) without
  creating or editing files inside the worktree.
- **Example:** write the probe test to a scratch directory outside the checkout
  and run pytest with `PYTHONPATH=tests`, `-p conftest`, `-o
  asyncio_mode=auto`, and `--rootdir` pointed at the checkout:

  ```sh
  D=$(mktemp -d)
  printf 'def test_probe(frozen_clock):\n    frozen_clock.advance(1)\n    assert frozen_clock.elapsed == 1\n' > "$D/test_probe.py"
  PYTHONPATH=tests uv run --locked pytest -p conftest -o asyncio_mode=auto --rootdir=. "$D/test_probe.py"
  rm -rf "$D"
  ```
- **Failure it catches:** writing temporary test files inside `tests/` risks
  dirtying the worktree during another run or review check, while running an
  external test file without `PYTHONPATH=tests -p conftest --rootdir=<repo>`
  fails to load the repository's fixtures and `pyproject.toml` warning rules
  (#246 / PR #459).

## 4. Windows text decoding (cp1252)

### Decode subprocess text output as UTF-8 (`encoding="utf-8"`)

- **Trigger:** a test runs `subprocess.run` or `subprocess.Popen` with
  `text=True` (or `universal_newlines=True`) and reads `stdout` or `stderr` —
  especially `git` commands (`git grep`, `git log`, `git diff`) or Python child
  processes whose output can contain non-ASCII repository text, curly quotes,
  arrows, or Unicode paths.
- **Example:** pass `encoding="utf-8"` explicitly (and `errors="replace"` when
  exact non-UTF-8 bytes do not matter, or `"PYTHONIOENCODING": "utf-8"` in the
  child environment for Python subprocesses), as
  `test_role_principle_and_decision_ownership_across_docs_and_guides` in
  [`tests/test_runtime_content.py`](../../tests/test_runtime_content.py) and
  `_run_cli` in [`tests/test_cli.py`](../../tests/test_cli.py) do:

  ```python
  result = subprocess.run(
      ["git", "grep", "-n", "-i", pattern, "--", *paths],
      cwd=ROOT,
      capture_output=True,
      check=False,
      text=True,
      encoding="utf-8",
  )
  ```
- **Failure it catches:** on Windows, `text=True` without `encoding=` decodes
  with `locale.getpreferredencoding(False)` (typically `cp1252`). When `git` or
  a child writes UTF-8 bytes outside `cp1252` (such as `0x9d` in a UTF-8 right
  double quotation mark), `subprocess`'s background reader thread raises
  `UnicodeDecodeError: 'charmap' codec can't decode byte ...`. pytest surfaces
  the thread failure only as `PytestUnhandledThreadExceptionWarning`;
  `result.stdout` is left `None`, and the test fails with a misleading
  `AttributeError: 'NoneType' object has no attribute 'splitlines'` — and only
  on the `windows` CI job (#88 / PR #641, #164, #166).

### Cover console printing against a strict `cp1252` stream

- **Trigger:** a CLI command, script, or helper prints text that can come from
  served guides, workflow headlines, or non-ASCII filesystem paths to `stdout`
  or `stderr`.
- **Example:** wrap a `BytesIO` buffer in
  `io.TextIOWrapper(..., encoding="cp1252", errors="strict")` (or set
  `PYTHONIOENCODING=cp1252:strict` for a subprocess) so Linux and Windows both
  exercise the Windows console code page, as
  `test_mock_alice_join_result_is_printable_on_cp1252_stdout` in
  [`tests/test_mock_alice.py`](../../tests/test_mock_alice.py),
  `test_console_output_preserves_encoding_and_escapes_unencodable_text` in
  [`tests/test_stdio.py`](../../tests/test_stdio.py), and
  `test_up_redirected_output_escapes_unencodable_checkout` in
  [`tests/test_cli.py`](../../tests/test_cli.py) do:

  ```python
  with io.BytesIO() as buffer, io.TextIOWrapper(buffer, encoding="cp1252", errors="strict") as stdout:
      monkeypatch.setattr(sys, "stdout", stdout)
      ...
  ```

  Because a Linux-only local run with default UTF-8 streams never hits `cp1252`,
  also watch the `windows` CI job's fast **"Run changed test files"** step on
  the PR (`AGENTS.md` Validation) whenever a test touches subprocess text or
  console output.
- **Failure it catches:** characters such as `∧` (`\u2227`), `→` (`\u2192`), or
  `ā` (`\u0101`) in served guides or test paths pass on Linux (`utf-8` stdio)
  and crash native Windows consoles or redirected pipes with
  `UnicodeEncodeError: 'charmap' codec can't encode character ...` (#3 body/6,
  #239 / PR #267).

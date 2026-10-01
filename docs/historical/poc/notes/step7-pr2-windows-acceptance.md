# Step 7, PR 2: Windows live acceptance for #126

The live networked acceptance of
[`sow-step7-pr2-issue-126.md`](sow-step7-pr2-issue-126.md), criterion 3
(steps 3.1–3.9), was run on 2026-09-24 against `main` at
`8efbede77842211b92f6710fd8dfe626e41b22cb`. That commit is PR #138, which
shipped #126. The hub ran in WSL2 (Ubuntu-24.04, NAT mode). Bob ran natively
on the Windows 11 host and was driven from WSL through interop
(`C:/Program Files/Git/bin/bash.exe`, `powershell.exe`).

**Result: every check passed.** Native Windows `prepare-run --worker-only`
worked unchanged, so this PR contains no code fix. The hub recorded Bob's
requests as coming from the Windows vEthernet address `172.26.112.1`. That is
neither loopback nor WSL's `eth0` address (`172.26.115.68`). It recorded the
WSL loopback worker as `127.0.0.1`.

The token is never shown. Where a rendered config is printed below, the
`HUB_TOKEN` value is redacted. Long WSL paths are shortened with
`S=/tmp/claude-1002/<session>/scratchpad`, a scratch directory outside the
checkout.

| Address | Value |
|---|---|
| WSL `eth0` (hub host, `--hub-url`) | `172.26.115.68/20` |
| Windows `vEthernet (WSL (Hyper-V firewall))` | `172.26.112.1/20` (WSL's default gateway) |
| Hub port | `8431`, because `8420` is held by another checkout's hub |

## 3.1 Hub host

```console
$ ss -ltn
State  Recv-Q Send-Q  Local Address:Port  Peer Address:PortProcess
LISTEN 0      64          127.0.0.1:5345       0.0.0.0:*
LISTEN 0      1000   10.255.255.254:53         0.0.0.0:*
LISTEN 0      2048        127.0.0.1:8420       0.0.0.0:*
LISTEN 0      4096       127.0.0.54:53         0.0.0.0:*
LISTEN 0      4096        127.0.0.1:631        0.0.0.0:*
LISTEN 0      511         127.0.0.1:39257      0.0.0.0:*
LISTEN 0      4096    127.0.0.53%lo:53         0.0.0.0:*
LISTEN 0      4096            [::1]:631           [::]:*
$ pgrep -a hub
835017 /home/alfred/lw/w533d-robo-agents.wtd/.venv/bin/python /home/alfred/lw/w533d-robo-agents.wtd/.venv/bin/hub
```

`8420` belongs to another checkout's hub (`w533d-robo-agents.wtd`), which was
left alone. `8431` was free.

The repository was the sandbox, used only as a clone source. `--skip-github-checks`
was passed, and no issue was created.

```console
$ uv run --locked python scripts/prepare-run.py --skip-github-checks \
    --repository git@github.com:RoboNater/robo-agents-sandbox.git \
    --run-dir $S/hubrun --hub-host 0.0.0.0 \
    --hub-url http://172.26.115.68:8431 --remote-worker bob
{
  "checks": {
    "ci": "skipped (local repository or --skip-github-checks)",
    "codex_auth": {"charlie": "codex: Logged in using ChatGPT"},
    "gh_auth": "skipped",
    "merge": "skipped (local repository or --skip-github-checks)",
    "versions": {"codex": "codex-cli 0.155.1"}
  },
  "configs": {"alice": "$S/hubrun/configs/alice.mcp.json",
              "charlie": "$S/hubrun/configs/codex/config.toml"},
  "issue": null,
  "network": {
    "hub_host": "0.0.0.0",
    "hub_port": 8431,
    "hub_url": "http://172.26.115.68:8431",
    "public_url": "http://172.26.115.68:8431",
    "remote_worker": "bob"
  },
  ...
  "workspaces": {"charlie": "$S/hubrun/charlie"}
}

Launch commands (paste in order):
...
# bob runs on the worker host: render and launch it there (below)
...

Remote worker bob: on its host, from a robo-agents checkout at this commit, run (then launch it with the lines that prints):
uv run --locked python scripts/prepare-run.py --worker-only bob --repository 'git@github.com:RoboNater/robo-agents-sandbox.git' --run-dir '<absolute run directory on the worker host>' --hub-url 'http://172.26.115.68:8431' --token-file '\\wsl.localhost\Ubuntu-24.04\tmp\claude-1002\<session>\scratchpad\hubrun\hub-state\token' --bob claude-code

Preflight (run on each worker host that is not the hub host; PowerShell needs curl.exe, since curl is an alias there):
curl.exe -fsS http://172.26.115.68:8431/healthz
curl.exe -fsS http://172.26.115.68:8431/.well-known/agent-card.json   # its url must be http://172.26.115.68:8431/a2a
Warning: if 172.26.115.68 is a WSL2 NAT address (eth0), it changes whenever WSL restarts ...
```

Exit status 0. `hub-state/token` was created with mode `-rw-------`.

## 3.2 Windows checkout

This run used `main`, not a PR head, because #126 had already merged
(PR #138). The tools found on Windows were uv 0.12.18, git 2.48.1.windows.1,
`claude` 2.1.281 and `gh`, logged in as RoboNater.

```console
(Git Bash) $ cd /c/work/robo-agents
$ git fetch origin && git checkout main && git pull --ff-only
Already on 'main'
Your branch is up to date with 'origin/main'.
Already up to date.
$ git rev-parse HEAD
8efbede77842211b92f6710fd8dfe626e41b22cb
$ uv sync --locked --all-packages
Resolved 59 packages in 44ms
Checked 58 packages in 25ms
```

## 3.3 Native Windows `--worker-only` (the #132 gap)

This is the printed command, run in Git Bash on Windows with the run directory
filled in:

```console
(Git Bash) $ cd /c/work/robo-agents
$ uv run --locked python scripts/prepare-run.py --worker-only bob --repository 'git@github.com:RoboNater/robo-agents-sandbox.git' --run-dir 'C:/work/step7-pr2b-run' --hub-url 'http://172.26.115.68:8431' --token-file '\\wsl.localhost\Ubuntu-24.04\tmp\claude-1002\<session>\scratchpad\hubrun\hub-state\token' --bob claude-code
{
  "checks": {"versions": {"claude": "2.1.281 (Claude Code)"}},
  "config": "C:/work/step7-pr2b-run/configs/bob.mcp.json",
  "hub_url": "http://172.26.115.68:8431",
  "prompt": "C:/work/step7-pr2b-run/bob.prompt.md",
  "run_dir": "C:\\work\\step7-pr2b-run",
  "worker_only": "bob",
  "workspace": "C:\\work\\step7-pr2b-run\\bob"
}

Preflight (run on each worker host that is not the hub host; ...):
curl.exe -fsS http://172.26.115.68:8431/healthz
curl.exe -fsS http://172.26.115.68:8431/.well-known/agent-card.json   # its url must be the hub run's --public-url + /a2a
...

Launch command for bob (paste after the preflight passes):
cd "/c/work/step7-pr2b-run/bob"
claude --strict-mcp-config --mcp-config "C:/work/step7-pr2b-run/configs/bob.mcp.json"
$ echo $?
0
```

It succeeded on the first run, so no fix was needed. The render was checked
natively on Windows with `uv run --locked python verify_bob_render.py`, a
throwaway script outside the checkout. The script loads `bob.mcp.json`,
redacts the token, reads the clone's `.git/robo-agents-workspace.json`,
compares the paths, runs `claude --version`, and searches the run directory
for token files:

```text
bob.mcp.json command/args: uv ['run', '--locked', '--directory', 'C:/work/robo-agents', 'worker-mcp']
bob.mcp.json env: {
  "AGENT_NAME": "bob",
  "HUB_CAPABILITIES": "",
  "HUB_HARNESS": "claude-code",
  "HUB_HARNESS_VERSION": "2.1.281",
  "HUB_MODEL": "",
  "HUB_PROVIDER": "anthropic",
  "HUB_TELEMETRY_LOG": "C:/work/step7-pr2b-run/bob-telemetry.jsonl",
  "HUB_TOKEN": "<redacted, 64 chars>",
  "HUB_URL": "http://172.26.115.68:8431",
  "HUB_WORKSPACE": "C:/work/step7-pr2b-run/bob",
  "PYTHONUTF8": "1"
}
identity file: {
  "agent": "bob",
  "path": "C:\\work\\step7-pr2b-run\\bob",
  "repository": "git@github.com:RoboNater/robo-agents-sandbox.git",
  "workspace_id": "70c995696af920d2b918cebce391e0d828347e0bf2294c12ef7f12565dac617f"
}
identity path == HUB_WORKSPACE (as Windows paths): True
identity path == str(Path(HUB_WORKSPACE)) (read_identity's check): True
origin: git@github.com:RoboNater/robo-agents-sandbox.git
git log -1: bf10dca Step 6: add normalize_label (strip-only first draft) (#13)
claude --version: 2.1.281 (Claude Code) | HUB_HARNESS_VERSION: 2.1.281
token files under run dir: []
```

- **`C:/` forward-slash paths:** `--directory`, `HUB_WORKSPACE` and
  `HUB_TELEMETRY_LOG` all use `C:/...`. The launch line's `cd` uses the Git
  Bash spelling `/c/...`, as intended (#75).
- **Windows git bootstrapped the clone:** the whole command ran as a native
  Windows process, using `/mingw64/bin/git`. The identity `path` is Windows'
  own spelling of `HUB_WORKSPACE`, with backslashes where the config has
  forward slashes. It equals `HUB_WORKSPACE` as a Windows path, and it passes
  `read_identity`'s string check against `str(Path(HUB_WORKSPACE))`. Step 3.5
  shows `WorkerSettings.from_env()` accepting it.
- **`HUB_HARNESS_VERSION`:** `2.1.281`, which matches the Windows
  `claude --version`.
- **No token file on Windows:** the run directory contains only `bob/`,
  `bob.prompt.md`, `configs/bob.mcp.json` and `run.json`. From WSL,
  `grep -rlF -f $S/hubrun/hub-state/token /mnt/c/work/step7-pr2b-run --exclude-dir=.git`
  finds the token only in `configs/bob.mcp.json`. That file carries it as
  `HUB_TOKEN` by design.

## 3.4 Hub start and preflight

The hub was started from the rendered `alice.mcp.json`, with that file's
`command`, `args` and `env`. The launcher was a small MCP stdio client
(`$S/hub_session.py`). It keeps the hub's stdin open, calls `get_state` on
request, and closes stdin to stop the hub. A hub launched with stdin at EOF
exits by design.

```text
INFO:     Started server process [838809]
INFO:agent_hub.app:SQLite database ready at $S/hubrun/hub-state/hub.db
INFO:agent_hub.app:Bearer token loaded from HUB_TOKEN
INFO:     Uvicorn running on http://0.0.0.0:8431 (Press CTRL+C to quit)
$ ss -ltnp | grep 8431
LISTEN 0      2048          0.0.0.0:8431       0.0.0.0:*    users:(("hub",pid=838809,fd=6))
```

The preflight ran on Windows, in PowerShell with `C:\windows\system32\curl.exe`:

```console
PS> (Get-Command curl.exe).Source
C:\windows\system32\curl.exe
PS> curl.exe -fsS http://172.26.115.68:8431/healthz
{"status":"ok"}
PS> $card = curl.exe -fsS http://172.26.115.68:8431/.well-known/agent-card.json   # exit 0
PS> ($card | ConvertFrom-Json).url
http://172.26.115.68:8431/a2a
```

The first attempt at this step had a quoting mistake in the `echo`
separators, not in the curl calls, so it was run again. The raw card from
that first attempt ends with
`..."url":"http://172.26.115.68:8431/a2a","version":"0.1.0"}`.

## 3.5 Real check-in and heartbeats from Windows

The driver below ran natively on Windows (`checkin_heartbeat.py`, outside the
checkout). It goes through `worker_mcp`'s own code path:
`WorkerSettings.from_env()`, then `WorkerHubClient.__aenter__`, which starts
the background heartbeat loop, then `WorkerHubClient.check_in()`. No HTTP is
built by hand. It loads the rendered config's env into its own process and
never prints the token.

```python
config, seconds = Path(sys.argv[1]), float(sys.argv[2])
if config.suffix == ".toml":
    env = tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]["hub"]["env"]
else:
    env = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["hub"]["env"]
os.environ.update(env)
os.environ["HUB_HEARTBEAT_S"] = "5"
if len(sys.argv) > 3:
    os.environ["HUB_URL"] = sys.argv[3]

async def main() -> None:
    settings = WorkerSettings.from_env()
    print("settings:", json.dumps({...}))          # agent, hub_url, workspace, ids; no token
    async with WorkerHubClient(settings) as client:  # starts the heartbeat loop
        print("check_in:", json.dumps(await client.check_in()))
        await asyncio.sleep(seconds)
```

`HUB_TELEMETRY_LOG` comes from the rendered `bob.mcp.json`.

```console
(Git Bash) $ cd /c/work/robo-agents
$ uv run --locked python C:/work/step7-pr2b-tools/checkin_heartbeat.py C:/work/step7-pr2b-run/configs/bob.mcp.json 45
settings: {"agent": "bob", "hub_url": "http://172.26.115.68:8431", "workspace": "C:\\work\\step7-pr2b-run\\bob", "workspace_id": "70c995696af920d2b918cebce391e0d828347e0bf2294c12ef7f12565dac617f", "heartbeat_s": 5.0, "telemetry_log": "C:\\work\\step7-pr2b-run\\bob-telemetry.jsonl"}
check_in: {"status": "registered", "agent": "bob", "context_id": "9d1b92ee5a57455ab313f727a23cd74a", "profile": {"harness": "claude-code", "harness_version": "2.1.281", "provider": "anthropic", "model": "unknown", "model_source": "unknown", "capabilities": [], "workspace_id": "70c995696af920d2b918cebce391e0d828347e0bf2294c12ef7f12565dac617f", "declared_model": "unknown"}}
closed after 45.0 s
$ echo $?
0
```

Over 45 s, Bob's telemetry
(`C:/work/step7-pr2b-run/bob-telemetry.jsonl`) recorded a `session_started`,
8 heartbeats, all `phase: success` and `accepted: true`, and a
`session_stopped`. Here are the first and a representative heartbeat record:

```json
{"agent": "bob", "event": "session_started", "harness": "claude-code", "harness_version": "2.1.281", "heartbeat_s": 5.0, "hub_url": "http://172.26.115.68:8431", "max_retries": 3, "model": "unknown", "model_source": "unknown", "provider": "anthropic", "session_id": "5befe7177f8844a5b7339c66c4fc782e", "timestamp": "2026-09-24T16:39:08.363Z", "worker_instance_id": "c0b091e1008c42a1b44d85e4603935b7"}
{"accepted": true, "agent": "bob", "current_task_id": null, "event": "heartbeat", "hub_url": "http://172.26.115.68:8431", "phase": "success", "session_id": "5befe7177f8844a5b7339c66c4fc782e", "timestamp": "2026-09-24T16:39:20.582Z", "worker_instance_id": "c0b091e1008c42a1b44d85e4603935b7"}
```

## 3.6 Loopback worker from WSL

Charlie is the WSL worker. The same driver loaded the env from Charlie's
rendered Codex `config.toml`, with `HUB_URL` overridden to loopback:

```console
$ uv run --locked python $S/checkin_heartbeat.py $S/hubrun/configs/codex/config.toml 12 http://127.0.0.1:8431
settings: {"agent": "charlie", "hub_url": "http://127.0.0.1:8431", "workspace": "$S/hubrun/charlie", "workspace_id": "887b30bf3434f74c1712972625d6751d25ce47029f7edd685d8af58e5940d1c0", "heartbeat_s": 5.0, "telemetry_log": "$S/hubrun/charlie-telemetry.jsonl"}
check_in: {"status": "registered", "agent": "charlie", "context_id": "59f80aabb2cc4761935235353424a42e", "profile": {"harness": "codex", "harness_version": "0.155.1", "provider": "openai", "model": "unknown", "model_source": "unknown", "capabilities": [], "workspace_id": "887b30bf3434f74c1712972625d6751d25ce47029f7edd685d8af58e5940d1c0", "declared_model": "unknown"}}
closed after 12.0 s
```

## 3.7 Observations

Bob's `agent` row was polled read-only from `hub.db` about once a second
during 3.5, with a line printed whenever `last_heartbeat` changed. The columns
are (name, last_heartbeat, checkin_remote_addr, last_remote_addr), and the
wall clock is WSL local time (EDT):

```text
12:39:11 ('bob', '2026-09-24T16:39:11.163Z', '172.26.112.1', '172.26.112.1')   # check-in
12:39:14 ('bob', '2026-09-24T16:39:14.652Z', '172.26.112.1', '172.26.112.1')
12:39:20 ('bob', '2026-09-24T16:39:20.022Z', '172.26.112.1', '172.26.112.1')
12:39:25 ('bob', '2026-09-24T16:39:25.370Z', '172.26.112.1', '172.26.112.1')
12:39:30 ('bob', '2026-09-24T16:39:30.725Z', '172.26.112.1', '172.26.112.1')
12:39:36 ('bob', '2026-09-24T16:39:36.072Z', '172.26.112.1', '172.26.112.1')
12:39:41 ('bob', '2026-09-24T16:39:41.423Z', '172.26.112.1', '172.26.112.1')
12:39:45 ('bob', '2026-09-24T16:39:44.933Z', '172.26.112.1', '172.26.112.1')
```

The poller's 55 s window closed before the eighth heartbeat. The row read
afterwards shows it at `16:39:50.279Z`.

These are the `agent` rows after both workers ran, from `hub.db`
(`PRAGMA user_version` is `12`):

```text
SELECT name, status, harness, harness_version, workspace_id, last_seen, last_heartbeat, checkin_remote_addr, last_remote_addr FROM agent ORDER BY name;
{"name": "bob", "status": "idle", "harness": "claude-code", "harness_version": "2.1.281", "workspace_id": "70c995696af920d2b918cebce391e0d828347e0bf2294c12ef7f12565dac617f", "last_seen": "2026-09-24T16:39:50.279Z", "last_heartbeat": "2026-09-24T16:39:50.279Z", "checkin_remote_addr": "172.26.112.1", "last_remote_addr": "172.26.112.1"}
{"name": "charlie", "status": "idle", "harness": "codex", "harness_version": "0.155.1", "workspace_id": "887b30bf3434f74c1712972625d6751d25ce47029f7edd685d8af58e5940d1c0", "last_seen": "2026-09-24T16:40:36.550Z", "last_heartbeat": "2026-09-24T16:40:36.550Z", "checkin_remote_addr": "127.0.0.1", "last_remote_addr": "127.0.0.1"}
```

`get_state`, called through the hub's MCP session, returned the same values.
This is Bob's full agent snapshot:

```json
{"name": "bob", "capabilities": [], "status": "idle", "context_id": "9d1b92ee5a57455ab313f727a23cd74a", "last_seen": "2026-09-24T16:39:50.279Z", "worker_instance_id": "c0b091e1008c42a1b44d85e4603935b7", "last_heartbeat": "2026-09-24T16:39:50.279Z", "last_progress_at": null, "current_task_id": null, "harness": "claude-code", "harness_version": "2.1.281", "provider": "anthropic", "model": "unknown", "model_source": "unknown", "workspace_id": "70c995696af920d2b918cebce391e0d828347e0bf2294c12ef7f12565dac617f", "declared_model": "unknown", "checkin_remote_addr": "172.26.112.1", "last_remote_addr": "172.26.112.1", "model_mismatch": false, "heartbeat_age_s": 53.05527, "progress_age_s": null}
```

This shows where `172.26.112.1` lives:

```console
PS> Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -like 'vEthernet*' }
InterfaceAlias                     IPAddress    PrefixLength
vEthernet (WSL (Hyper-V firewall)) 172.26.112.1           20
$ ip -4 -o addr show eth0
2: eth0    inet 172.26.115.68/20 brd 172.26.127.255 scope global eth0
$ ip route show default
default via 172.26.112.1 dev eth0 proto kernel
```

| Check | Observed | |
|---|---|---|
| Bob's `checkin_remote_addr` / `last_remote_addr` are the Windows vEthernet address, not loopback and not WSL `eth0` | `172.26.112.1` / `172.26.112.1` | pass |
| Bob's `last_heartbeat` advances across ≥3 heartbeats | advanced on each of 8 heartbeats | pass |
| The WSL worker's addresses are `127.0.0.1` | `127.0.0.1` / `127.0.0.1` | pass |
| Bob's telemetry `hub_url` is `http://172.26.115.68:8431` | yes, on `session_started` and on all 8 heartbeat records | pass |
| Both workers have non-null, distinct `workspace_id`s | `70c99569…` (bob), `887b30bf…` (charlie) | pass |

A side observation, not a finding against #126: the hub's heartbeat
timestamps are about 5.35 s apart, while Bob's Windows-side telemetry shows
the heartbeats 5.05 s apart. Between the sixth and seventh heartbeat, the
hub's wall clock stepped back about 1.8 s (`41.423Z` → `44.933Z`, against
5.05 s on Windows). So the WSL clock drifts against the Windows clock and gets
corrected in steps. `last_heartbeat` still advanced on every heartbeat here.
Wall-clock comparisons across the two hosts need that tolerance.

## 3.8 Clean up

The hub was stopped by closing its MCP stdin. Both driver processes had
already exited, with status 0.

```text
INFO:     Application shutdown complete.
INFO:     Finished server process [838809]
$ pgrep -a hub
835017 /home/alfred/lw/w533d-robo-agents.wtd/.venv/bin/python /home/alfred/lw/w533d-robo-agents.wtd/.venv/bin/hub
$ ss -ltn | grep 8431
(nothing listening on 8431)
PS> Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*step7-pr2b*' -or $_.CommandLine -like '*checkin_heartbeat*' }
(only the query's own powershell.exe)
```

The other checkout's hub (pid 835017, port 8420) was left running. The
Windows run directory `C:/work/step7-pr2b-run` and the scripts in
`C:/work/step7-pr2b-tools` remain as evidence. The token in
`bob.mcp.json` belongs to this run's scratch hub, which is now stopped.

## Validation

These ran on the PR branch in WSL:

```console
$ uv sync --locked --all-packages --dev   # exit 0
$ uv run --locked ruff check .            # All checks passed!
$ uv run --locked mypy                    # Success: no issues found in 64 source files
$ uv run --locked pytest                  # 755 passed, 3 skipped, 2 warnings
```

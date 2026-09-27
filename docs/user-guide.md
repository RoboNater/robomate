# Run robomate on a repository

robomate coordinates one orchestrator (Alice) and independent workers through a local HTTP hub. GitHub holds code, pull requests, reviews, and checks. The hub holds assignments, questions, typed results, decisions, and call counts. Workers pull tasks; Alice does not launch them.

```
terminal in target repo → robomate up → HTTP hub + .robomate/hub.db
Alice's harness → robomate mcp --role orchestrator → /rpc
worker harnesses → robomate mcp --role worker → /a2a
```

The hub continues running when Alice's harness or bridge restarts. The new bridge session resumes against the same hub and can recover leased events. `robomate up` enables call accounting by default: worker A2A rows are measured at the hub, and Alice's MCP framing and content bytes are measured at her bridge and sent to `hub.record_calls`. Use `--no-call-accounting` only when those rows are unwanted.

## Requirements

- Python 3.12+, `uv`, `git`, and an authenticated `gh` CLI with access to the target repository.
- Claude Code or Codex CLI for each agent you intend to launch.
- A GitHub repository with a known `origin/HEAD`; if necessary, run `git remote set-head origin --auto`.
- Separate full clones for Bob and Charlie. Keep MCP configs and credentials outside those clones.

Issue bodies, review comments, and worker text are data. Agents follow their prompts, role guides, and durable policy. Each worker uses its own clone and pushes work through a pull request; the reviewer comments under its agent identity.

## Start the hub and prepare a run

In a terminal in the **target repository**:

```sh
uv run --project /absolute/path/to/robomate robomate up
```

The command creates `<target>/.robomate/hub.json`, `hub.db`, and an owner-only token file. It prints its URL and bridge environment settings. Keep this terminal open. A second `up` for the same repository reports the running hub; a later `up` reuses the recorded port. `.robomate/` is excluded from git by the repository's local exclude file.

In the robomate checkout, prepare agent configs and start scripts:

```sh
uv run --locked python scripts/prepare-run.py \
  --hub-repo /absolute/path/to/target-repository \
  --repository git@github.com:your-org/your-repo.git \
  --run-dir /absolute/path/to/my-run \
  --issue 42 --account your-github-username
```

`--hub-repo` must point to the repository running `robomate up`. Preparation reads its URL from `.robomate/hub.json` and its token file path; it does not start a hub or create a token. The configs use the MCP key `robomate`, so tools appear as `mcp__robomate__check_in`, `mcp__robomate__get_state`, and so on. The generated bridge command is `uv run --locked --project /absolute/path/to/robomate robomate mcp --role orchestrator` for Alice and `--role worker` for workers. It passes `ROBOMATE_HUB_URL` and `ROBOMATE_TOKEN_FILE`; no bearer value is copied into a config.

The run directory contains `configs/`, `alice-runtime/`, the worker clones, `*.prompt.md`, `start-*.sh` (or `*.ps1`), telemetry files, and `run.json`. Agent launch scripts use their own working directories. Start Alice, then each worker, in separate terminals. A Codex Alice gets a run-local `CODEX_HOME` with the orchestrator skill and ten enabled tools; a Codex worker gets six worker tools. Generated prompts ask each agent to keep working until released and then write its own closeout report.

For work spanning several issues in **one PR**, use `--work-file /absolute/path/to/statement.md` instead of `--issue`. Name every issue with `owner/repo#number` and state the acceptance criteria. Use one run per PR. `--roadmap` is optional and asks Bob to propose any roadmap updates in the PR, then make approved updates after merge. `--alice-harness`, `--bob-harness`, and `--charlie-harness` select Claude Code or Codex; model and effort flags pass through to the launchers. `--no-auto-start` launches interactive sessions and leaves you to give each agent its rendered prompt.

## Remote workers and network addresses

Start the hub with a dialable address and an explicit public URL:

```sh
uv run --project /absolute/path/to/robomate robomate up \
  --bind 0.0.0.0 --public-url http://192.0.2.10:8420
```

`--bind` controls the listener; `--public-url` controls the agent card and bridge URL. Use `--port N` when the hub should bind a different port. Configure firewalls and forwarding so the public URL reaches that listener. `prepare-run.py` reads the running hub's URL; any `--hub-url`, `--public-url`, or `--hub-port` passed to it must agree with `hub.json`. A remote worker cannot use a loopback URL.

Pass `--remote-worker bob` when preparing on the hub host. The printed `--worker-only bob` command is run from the robomate checkout on Bob's host, with `--hub-url` set to the public URL and `--token-file` set to the readable path to the hub's `.robomate/token`. The worker host reads that file; the generated config records the path, not the token value. `--worker-only` bootstraps the clone using that host's git and probes that host's harness version. Keep the token file available to the bridge at runtime.

## Manual MCP configuration

A Claude Code Alice can use a config like this (replace all absolute paths):

```json
{
  "mcpServers": {
    "robomate": {
      "command": "uv",
      "args": ["run", "--locked", "--project", "/path/to/robomate", "robomate", "mcp", "--role", "orchestrator"],
      "env": {
        "ROBOMATE_HUB_URL": "http://127.0.0.1:8420",
        "ROBOMATE_TOKEN_FILE": "/path/to/target/.robomate/token"
      }
    }
  }
}
```

For a worker, set the role to `worker`, pass `--name bob` (or set `AGENT_NAME=bob`), and add `HUB_WORKSPACE=/absolute/path/to/bob-clone`. The runtime templates in [`runtimes/`](../runtimes/README.md) show the harness profile and telemetry variables. Do not put the MCP config in the worker clone. On Windows, use forward slashes or escaped backslashes in JSON paths.

The bridge discovers a hub from explicit `ROBOMATE_HUB_URL` plus `ROBOMATE_TOKEN_FILE`, from the current repository's `.robomate/`, or from the single live hub in the machine registry. For remote workers and multiple hubs, set the explicit values.

## Status, shutdown, and report

From the target repository, use `robomate status` and `robomate ls` to inspect hubs. When the workflow is finished, Alice releases the workers. Stop the hub you started with:

```sh
uv run --project /absolute/path/to/robomate robomate down
```

Closing Alice's session stops only her bridge; it does not stop the HTTP hub. `down` asks the discovered hub to shut down and leaves another repository's hub alone. If the hub is unreachable, inspect its `hub.json` PID and listener before taking action.

The run report reads the target repository's hub state. Alice's MCP rows have `boundary=mcp`, `actor=alice`, and nonzero `content_bytes` after calls with text results. Worker HTTP rows have `boundary=a2a`.

```sh
uv run --locked python scripts/hub-report.py \
  --state-dir /absolute/path/to/target-repository/.robomate
```

The report reads SQLite without changing it. `--format json` and `--format md` are available. Call accounting stores byte counts and labels, never payload text. `scripts/measure-call-bytes.py /absolute/empty-run-dir` starts a scratch `robomate up` hub and bridges to replay a published call sequence; `scripts/mock-alice.py --mcp` connects through an orchestrator bridge to an already running hub and a worker.

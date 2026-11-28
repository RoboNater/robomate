# Run robomate on a repository

Robomate coordinates three agent roles through a local HTTP hub. Alice runs the workflow, Bob implements, and Charlie reviews independently in a separate worktree. GitHub or GitLab holds the code, review discussion and checks. The hub keeps the statement, assignments, typed results and decisions so that work can continue after a restart.

## Requirements

- Python 3.12+, `uv` and `git`.
- An authenticated `gh` or `glab` CLI with access to the target repository.
- A target checkout with an origin and a known default branch. In a fresh checkout, run `git fetch origin` and `git remote set-head origin --auto` if `origin/HEAD` is missing.
- A supported harness for each agent. Set up one static, user-level MCP entry per harness using a fixed robomate tooling checkout.

## Configure the MCP bridge

For Claude Code, install the user-level MCP entry. Use an absolute path to the robomate checkout.

```sh
claude mcp add --scope user robomate -- uv run --locked --project /path/to/robomate robomate mcp --harness claude-code
```

For Codex, add this to `~/.codex/config.toml` (or to `config.toml` under `CODEX_HOME`). On Windows, use native paths such as `C:/src/robomate`.

```toml
[mcp_servers.robomate]
command = "uv"
args = ["run", "--locked", "--project", "/path/to/robomate", "robomate", "mcp", "--harness", "codex"]
startup_timeout_sec = 120
tool_timeout_sec = 1900

[mcp_servers.robomate.env]
HUB_MCP_TIMEOUT_S = "1900"

[mcp_servers.robomate.tools.join]
approval_mode = "approve"
```

For unattended Codex runs, add an approval table like the one for `join` for each bridge tool. The setup guide covers tool discovery, approvals and the OpenCode and AntiGravity entries. [MCP setup guide](mcp-setup-prompt.md)

MCP launches the bridge over stdio, where it waits for requests. Put the actual executable and argument list in the entry. Shell aliases belong in your interactive terminal. Keep hub URLs, credentials and agent identities out of the static entry.

If you previously installed the robomate `worker` skill, remove it from your harness's skill directories. The `join` response supplies the worker guide.

## Start the hub and join the agents

From the target repository, start its hub in a terminal you can leave open.

```sh
uv run --locked --project /path/to/robomate robomate up --agents alice,bob,charlie
```

The commands below use `robomate` as shorthand for `uv run --locked --project /path/to/robomate robomate`, or for an installed executable. Keep running them from the target checkout unless you select a hub explicitly.

`robomate up` prints the hub identity, join lines and the requested worktree paths. You can also provision a worktree from another terminal with `robomate workspace <name>` while the hub is running. Start each harness in its own printed worktree and send its join line.

- Alice in Alice's worktree receives “Join robomate as alice, orchestrator.”
- Bob in Bob's worktree receives “Join robomate as bob, implementer.”
- Charlie in Charlie's worktree receives “Join robomate as charlie, reviewer.”

The bridge discovers the hub from the workspace binding, then each join returns the agent's role and workspace. Alice reads the orchestrator guide and checks the queue. The other agents wait for assignments. With the same forge login across agents, Charlie posts an agent-identified review comment and the hub records the typed reviewer verdict.

A hub belongs to its owning checkout. Another linked worktree of the target repository can run another hub with separate state, scope and agents. Use `robomate ls` to find hubs and `robomate status --hub <name-or-checkout>` to select one. A new hub stores state and token in separate directories beside the checkout. Use `robomate status --json` for their resolved paths. The operator can relocate a hub that still stores state inside its checkout with `robomate relocate`.

## Submit work

State decisions a run depends on in the issue or work file. Submit an issue
from the target checkout or give robomate a statement file. Include the intended outcome, acceptance checks and any applicable shared-counter reservation target.

```sh
robomate submit --issue 42
robomate submit statement.md
```

Use either form for a run. Each run delivers one PR or MR. The command prints the run ID and statement SHA-256. Once a live workflow exists, one additional run can queue behind it. Alice starts a submitted run from the stored statement. Use `robomate status` to see the queue, current step, PR/MR and merge gate. If Alice needs a decision, use `robomate inbox` and `robomate answer <id> "<answer>"`.

Set workflow policy in the user, repository or hub configuration layer, or pass `--policy key=value` to `submit`. For example, `robomate submit --issue 42 --policy merge_method=squash` selects squash delivery. The forge preflight checks the relevant repository settings and CI requirement. [Configuration and policy](mvp-spec.md#4-local-layout-discovery-multiple-hubs)

## Scripted run start

Before starting, configure the static, user-level `robomate` MCP entry for each
team harness. A harness used only for Denise needs no MCP entry, because Denise
runs with MCP servers disabled (see the `denise_*` row below). Use a stable
robomate checkout for `--project`, not a run lane. Follow the
[MCP setup prompt](mcp-setup-prompt.md) for the entry and unattended settings,
and run `scripts/check-mcp-tools.py` after updating that checkout. Devin's entry
cannot currently be checked automatically; start-run warns. The preflight checks
entry presence only.

`scripts/start-run.py` (#268) starts the hub of a fresh checkout, the three agents' worktrees, the submitted work and Alice, Bob and
Charlie headless under the launcher's supervision. The run then proceeds to a
merged PR without further input. The command runs `robomate up`, `robomate workspace`,
`robomate submit` and `scripts/agent-launcher.py start` in order. Each launcher
supervises one workflow: a queued run needs fresh launchers once it is active,
and `status` prints that reminder with the queue. Stop an attached standby
launcher first, so the next run's conversation and supervision belong together.
The same
Python runs in Bash and PowerShell; only the line continuation differs:

```sh
uv run --locked --project /path/to/robomate python /path/to/robomate/scripts/start-run.py \
  --generate-default-config ./run-config.toml
# Edit repository, run_dir and account.
uv run --locked --project /path/to/robomate python /path/to/robomate/scripts/start-run.py \
  --config ./run-config.toml --issue 42
uv run --locked --project /path/to/robomate python /path/to/robomate/scripts/start-run.py \
  --config ./run-config.toml --work-file ./statement.md --run-dir 02-work
# Save the merged settings, policy included, for the next run:
uv run --locked --project /path/to/robomate python /path/to/robomate/scripts/start-run.py \
  --config ./run-config.toml --issue 43 --run-dir 03-issue-43 --generate-config ./next-run.toml
```

```powershell
uv run --locked --project C:/robomate python C:/robomate/scripts/start-run.py `
  --config .\run-config.toml --issue 42
```

The run config is TOML. Keys match CLI options with underscores, the command line
overrides the file, and `--issue` and `--work-file` exclude each other (a CLI
selection replaces the file's). Unknown keys and wrong types are errors, and
generation refuses to overwrite a file. A relative `run_dir` is resolved under `run_parent_dir`. Use an absolute path to select a different parent.

```toml
repository = "git@github.com:your-org/your-repo.git"
run_parent_dir = "~/working"
run_dir = "01-issue-42"
account = "your-user"
reservations = "409"
bob_harness = "claude"
bob_model = "claude-opus-5-5"
bob_effort = "high"
charlie_harness = "codex"
pytest_workers = 4
# The default: uv sync in a worktree with uv.lock at its root; "none" disables it.
workspace_setup = "auto"

[policy]
merge_method = "squash"
max_review_rounds = 3
allow_no_ci = "auto"
```

A start clones `repository` into `<run_dir>/<hub_name>`. `hub_name` defaults
to the run directory's name and appears in branch names
(`robomate/<hub>/<agent>/<label>`). The hub keeps its state, token and the
agents' worktrees beside the clone, in `<hub_name>.robomate/<hub_name>/`. An
existing clone is reused only if its origin is `repository`; a live hub of
that clone is reused and left running, and a hub with a live or queued run is
refused. Each agent's launcher writes to `<run_dir>/logs/<agent>.log`, and
the hub's log is `logs/hub.log`. Once all agents have joined, the command prints
Denise's consultation command so you can start a doctor session while the run is
still live. The command stays in the foreground until the
run is done, or its agents are released, even after every launcher it started
has exited; only then does it report the run's status, where its closeout
reports are, a runnable `agent-launcher.py ask` command for each agent and
Denise's doctor command again, and stop the hub if it started it. A launcher you
resumed by hand therefore keeps its hub. Restart a stopped hub with
`robomate up` in the clone; its state is kept. After a launcher fails, the
command prints the `agent-launcher.py resume <agent>` line to run while the
hub is up; it does not exit on that failure while the run is unfinished, so
resume the agent, or press Ctrl-C, which stops the agents and the hub this
command started. A reused hub that stops answering ends the wait after a few
unanswered status reads.

### Asking an agent after the run

The final report prints an `agent-launcher.py ask` command for each agent. It opens that
agent's saved conversation interactively and needs no hub, so it also works
after the hub is stopped:

```sh
uv run --locked --project /path/to/robomate python /path/to/robomate/scripts/agent-launcher.py \
  ask bob --hub /path/to/run/scripted --fork
```

Hub tools are switched off for the conversation, so ask Bob to read the
closeout reports from `<state>/runs/<run_id>/closeout/` (the report prints the
full path; the state is `<run_dir>/<hub_name>.robomate/<hub_name>/state`).
`--fork` keeps the original transcript: Claude Code uses `--fork-session`,
Codex `codex fork`, OpenCode 1.x `--fork` (OpenCode 2.x's interactive CLI does
not support `--fork`, so `ask --fork` refuses it on 2.x). The launcher recovery guide documents the harness-specific controls.
[Asking an agent](development/agent-recovery.md#asking-an-agent)

### Consulting Denise about a run

`start-run.py` persists Denise's effective settings (and the run's log directory)
in `<state>/runs/<run_id>/denise/settings.json` and prints the consultation
command once all agents have joined, and again in the final report. Paste that
command into another terminal — with saved settings and no other runs on the hub,
it needs only `--hub <checkout>` (plus `--run <run_id>` while other runs exist).
From the hub checkout or inside `<state>/runs/<run_id>/`, you can also call
`doctor` with no flags at all, or override any option explicitly:

```sh
uv run --locked python scripts/agent-launcher.py doctor \
  [--hub /path/to/hub-checkout] [--run RUN_ID] [--harness opencode]
```

Selection order: from inside `<state>/runs/<run_id>/` (including
`denise/workspace`), `doctor` resolves both the hub and the run from that path
and refuses conflicting `--hub` or `--run` flags; otherwise `--hub` defaults to
the current git checkout and `--run` defaults to the hub's only run or its only
unfinished run (printing the choice and reason, and listing runs when ambiguous).
`--harness`, `--model`, `--effort` and `--clean-login` take explicit flags first,
then the run's saved `denise/settings.json`, or refuse naming `--harness`.
Overriding `--harness` to a different harness drops the saved model and effort.

Denise opens a fresh interactive consultation on the hub host. The selected
hub's local state must contain `runs/<run_id>/manifest.json`. A stopped hub
still supports offline diagnosis. For a missing or mismatched manifest,
check the run ID and owning checkout or restore the matching saved manifest.
Pass `--target` with the Git checkout root if the affected project is in a
separate checkout.

| Doctor harness | Login and access |
| --- | --- |
| Claude Code and Codex | Use the operator's login by default. Choose `--clean-login` for a separate per-run profile. The launcher passes selected directories, disables MCP and requests interactive permissions. Actual Claude Code shell containment on Windows depends on the installation. |
| OpenCode 1.x | Uses a separate per-run profile. Use `/connect` if the provider needs login. The launcher checks `opencode debug config` for enabled MCP, approves the selected external directories at the tool layer and asks for other external access. Shell commands retain OS access. Use on a trusted host. |
| AntiGravity (`agy`) | Uses a separate per-run home; follow the sign-in prompt there. The launcher configures an empty MCP profile, review prompts and scoped workspace directories. Terminal containment depends on the installed CLI and host. Treat it as trusted-only where the terminal sandbox is unavailable. |
| OpenCode 2.x | Supported on Linux (verified on `2.4.1`; native Windows live checks are deferred). Uses a separate per-run profile with `--standalone` so the shared background service cannot leak global config or MCP servers. Use `/connect` if the provider needs login. The launcher verifies `opencode debug config --standalone` has no enabled `mcp.servers` entries, sets ordered tool permissions asking by default with read-only access on the selected roots, and passes `model` (with `#variant` when set) via `OPENCODE_CONFIG_CONTENT`. Shell commands retain OS access. Use on a trusted host. |
| Devin and a doctor terminal on a worker-only host | Deferred. Run the doctor on the hub host with a supported harness. |

With OpenCode, a team effort setting is printed in a note and omitted from the
doctor command. The consultation uses the harness default. If the effective MCP
check fails, check the OpenCode version and the generated or managed config, or
select another doctor harness. The required Windows CI job covers the commands
with fake harnesses in PowerShell and Git Bash. Live OpenCode and AntiGravity
access and interactive startup are tracked in [#254](https://github.com/RoboNater/robomate/issues/254).

For a remote worker, add `--remote-worker bob=worker-host` to the doctor command
on the hub host. The index attributes remote workspace evidence to that host
and marks its launcher sessions unavailable. Run guarded resume on the worker
host with its original hub URL, local token file and session directory. See
[remote joined agents](development/agent-recovery.md#remote-hosts).

The doctor prints the context and a fresh Markdown report path under
`<state>/runs/<run_id>/denise/`. Starting the consultation requests diagnosis.
Ask explicitly before restarting a hub or agent, replacing an attached
predecessor or handing work back. Use the operator's own terminal for recovery
commands that need a hub token or machine operator credential when the doctor
profile cannot access it. Keep credentials out of reports. See the
[local Denise guide](denise/guide.md) for permissions, decisions and evidence.

### Stopping and resuming one agent mid-run

The command prints each agent's launcher PID when it starts it. To take one
agent out of the run without stopping the others, send SIGTERM to that
launcher (`kill <pid>`): the launcher stops the harness it started and exits,
the other agents keep working, and the hub stays up. The command keeps it up
until the run is done or the agents are released, even after every launcher it
started has exited.

To talk to the stopped agent in the context of the run, open its saved
conversation with the launcher's interactive resume:

```sh
uv run --locked --project /path/to/robomate python /path/to/robomate/scripts/agent-launcher.py \
  resume bob --hub /path/to/run/scripted --interactive
```

It prints the fixed continuation to send once the conversation is open; the
hub tools stay available, so the agent can read the run. Use `ask bob` instead
when you only want questions and no hub work. When it should rejoin the run,
hand it back headless:

```sh
uv run --locked --project /path/to/robomate python /path/to/robomate/scripts/agent-launcher.py \
  resume bob --hub /path/to/run/scripted
```

Recovery verifies the saved conversation, the same hub and the stopped
predecessor before it launches; its checks and controls are in
[agent recovery](development/agent-recovery.md#resuming-by-hand), not
repeated here.

Every agent's harness, and so its `robomate mcp` bridge, gets the agent's
declared identity: `HUB_HARNESS`, `HUB_HARNESS_VERSION` (from the CLI's own
`--version`, probed before anything starts), `HUB_PROVIDER` (from the model, as
`run_common.resolve_provider` reads it), `HUB_MODEL` and `HUB_EFFORT` (#402). A
pinned model is recorded with source `env`. Codex forwards no variable to an
MCP server unless told, so its launch names them in `-c mcp_servers.robomate.env.*`.
Each agent also gets an owner-only `UV_CACHE_DIR=<run_dir>/tmp/<agent>/uv-cache`
(#285), granted to its sandbox, and the run's `PYTEST_XDIST_AUTO_NUM_WORKERS`.
The launcher sets them in the harness's environment directly, with the
host's own path spelling, so no shell needs an export line. Its launch rows
record them, and `agent-launcher.py resume` applies them again.

Key run options

| Key | Purpose |
|---|---|
| `repository`, `run_parent_dir`, `run_dir` | The clone is `<run_dir>/<hub_name>`; new optional `hub_name` and `hub_port` (0: first free from 8420). |
| `issue` / `work_file` | Mutually exclusive. The statement goes to `robomate submit`, with the counter text below. |
| `account` | Required. The statement ends with `Forge comment identity account: <account>.`, which Alice uses for comment identities. |
| `reservations` | Adds the reservations issue and counter procedure to the statement; without the key it says no issue was supplied. |
| `forge` | Passed to `robomate up --forge`. Empty (the new default) detects the forge from the origin. |
| `alice_*`, `bob_*`, `charlie_*` harness, model, effort | Passed to `agent-launcher.py start`. Names and roles stay fixed: Alice orchestrator, Bob implementer, Charlie reviewer. |
| `denise_settings_from`, `denise_*` | Defaults to Bob's settings. Choose Alice, Bob, Charlie or `none`; `none` requires an explicit `denise_harness`. Model and effort inherit only when the harness matches. Empty overrides select the harness defaults when changing harnesses. Effective settings and the log directory are saved to `runs/<run_id>/denise/settings.json`; the doctor command is printed once the team joins and again in the final report. `denise_clean_login = true` (`--denise-clean-login`) requests a separate login for Claude Code or Codex; OpenCode and AntiGravity already use one. See [Consulting Denise](#consulting-denise-about-a-run). |
| `auto_start` | `false` prints each agent's `agent-launcher.py start ... --interactive` command, then keeps the hub up until the run is done, its agents are released, or Ctrl-C. |
| `allow_unsandboxed` | Linux only, default `[]` (repeatable CLI `--allow-unsandboxed AGENT`). Names the agent(s) (`alice`, `bob`, `charlie`) allowed to continue if sandbox preflight fails; other agents keep the refusal. Still runs the verifier, prints a loud warning at startup and in the final report, records the override in the session log, and includes `--allow-unsandboxed` in printed resume hints only for the named agent(s). See [Ubuntu 24.04 host setup](#ubuntu-2404-host-setup). |
| `pytest_workers` | Sets the agents' `PYTEST_XDIST_AUTO_NUM_WORKERS`; 0 clears an inherited value. |
| `workspace_setup` | Shell command run in each agent worktree before launch, with its isolated cache and temporary directory. `"auto"` (the default, also used when the key is absent or empty) runs `uv sync --locked --all-packages --dev` when the worktree has a `uv.lock` at its root and does nothing otherwise, so targets that are not uv projects start untouched. `"none"` (compared case-insensitively, ignoring surrounding whitespace) disables setup; any other string is run as given. On manual resume it runs again if `.venv` is missing, keeping the setting the start recorded (an explicit `"none"` stays off). A failure stops that agent’s launcher and prints the output; during `start-run` startup, a launcher failure aborts the team and stops the hub, as with other preflight failures. Setup time counts against `join_window_s` (120 seconds by default); a cold sync on a slow link is best given a larger window. Setup output appears on failure. |
| `stall_after_min`, `stall_action_after_min` | Read as `[policy]` keys. |
| `--generate-default-config`, `--generate-config` | A merged config also saves `[policy]`. |

Policy, including `merge_method`, `max_review_rounds`, `allow_no_ci`,
`reviewer_harness_differs` and `stall_after_min`, is set per run
in `[policy]` or with `--policy k=v` (repeatable, TOML values). It goes to
`robomate submit --policy` over the hub, repository and user configuration
layers. Its keys, tables and value types are checked against the hub's own
policy model before anything starts. Rules that compare two fields, such as
`stall_action_after_min` above `stall_after_min`, are left to `submit`, because
a layer may set the other field. The launcher's resume bounds are top-level
keys: `max_resumes` (-1, the default, is unlimited), `resume_delay_s`,
`resume_max_delay_s`, `resume_total_s` and `resume_series_reset_s`. The #169
warning and cap keep their defaults (3 and 6 consecutive long failing runs); to
change them, add `--warn-series-resets` or `--max-series-resets` on the launcher command line (0 disables either).

The hub collects agent retrospectives before release and exports them to
`<state>/runs/<run_id>/closeout/`. The command prints that path. Put per-run
instructions in the statement of work. Each assignment gives the worker the
stored statement's SHA-256 so it can read the statement verbatim.

For an agent-driven check without real harnesses, `scripts/mock-harness.py
--install DIR` writes `claude` and `codex` shims: scripted stand-ins that join
through `robomate mcp` and play their roles to a `done` run. Put `DIR` first on
`PATH`, inside the isolated smoke recipe. A local-path repository has forge
`unknown`, which has no orchestrator guide appendix (#481), so set
`forge = "github"` for such a check.

## Remote workers

Start the hub with a listener and a public URL the worker can reach over a
trusted network. Replace the example URL with your hub host's address.

```sh
robomate up --bind 0.0.0.0 --public-url http://192.0.2.10:8420
```

On the worker host, place the hub bearer token in a private file, outside the
clone. Run the following there with its native Git and robomate installation,
then start the harness in the clone and send Bob's join line.

```sh
robomate workspace bob --clone /path/to/agents/bob \
  --hub-url http://192.0.2.10:8420 --token-file /private/path/hub-token
```

Robomate verifies the hub and gets the origin for the clone from it. The
clone's local binding lets the configured MCP bridge find the hub and read
the token. If you set `ROBOMATE_HUB_URL` and `ROBOMATE_TOKEN_FILE` for a
remote launcher, use the verified endpoint and that same private file. For
Windows workers with a hub in WSL, check the public listener from PowerShell
with `curl.exe -fsS http://<WSL-IP>:8420/healthz`. If WSL changes its address
after restart, update the hub's public URL and the worker's endpoint.

The scripted path also accepts `--remote-worker bob` and prints the worker-host
setup and start commands. See the launcher recovery guide for a remote worker's
guarded resume. [Remote host recovery](development/agent-recovery.md#remote-hosts)

## Linux sandbox grants

For robomate runs, `workspace_setup` defaults to `"auto"`, so the launcher syncs each worktree whose root has a `uv.lock` (with `uv sync --locked --all-packages --dev`) and its uv cache before the sandbox starts; first validation then needs no PyPI network grants. Set `workspace_setup = "none"` to disable it, or give another shell command to replace it.

### Ubuntu 24.04 host setup

For both Codex and Claude Code, the known-good setup from [#498](https://github.com/RoboNater/robomate/issues/498) uses `/usr/bin/bwrap` with [Claude Code's AppArmor profile](https://code.claude.com/docs/en/sandboxing) and keeps `kernel.apparmor_restrict_unprivileged_userns=1`. Inspect `scripts/host-setup/ubuntu-24.04/install-bwrap-apparmor.sh --dry-run`, then an operator can run the installer with sudo. It backs up Ubuntu's old profile, installs and loads the replacement, prints undo instructions, and verifies as the invoking user. Do not replace a profile installed by another application without resolving that conflict first. Run `scripts/host-setup/ubuntu-24.04/verify-sandboxes.sh` without sudo to check the host; `--with-claude` also makes a model call to check Claude Code. After a reboot check `sudo aa-status | grep bwrap` (reboot persistence has not yet been verified).

| Profile on bwrap | Userns restriction | Codex | Claude Code | Nested userns |
| --- | --- | --- | --- | --- |
| Ubuntu bwrap-userns-restrict | 1 | passes | fails | fails |
| none | 1 | fails | silently unsandboxed | fails |
| none | 0 | passes | passes | passes |
| Ubuntu bwrap-userns-restrict | 0 | passes | fails | fails |
| Claude Code bwrap | 1 | passes; HOME write refused | passes; HOME write refused | passes |

The earlier #438 `bwrap-userns-restrict` recommendation breaks Claude Code even with the sysctl disabled. The replacement lets any local code use the general bwrap launcher to obtain a capable user namespace. This is narrower than setting the sysctl to 0 for every program, but weaker than Ubuntu's restrictive bwrap profile. Ubuntu 25.04 and later ship the restrictive profile in the `apparmor` package; the same conflict may apply there, unverified. Avoid profiles attached to user-writable copies of bwrap.

The joined-agent launcher runs model-free checks on every Linux host before start and resume, including hosts outside Ubuntu, and refuses to launch if they fail. It requires `/usr/bin/bwrap` and nested user namespaces; a launcher already inside a sandbox may fail the probe. An operator who cannot fix the host yet can override a failing check per agent with `--allow-unsandboxed` on `agent-launcher.py start` or `resume` (or `allow_unsandboxed = ["charlie"]` / `--allow-unsandboxed charlie` on `start-run.py`). There is no global environment variable or default override, and other agents keep the refusal. Under the override the launcher still runs the verifier, prints the failure and a loud warning naming the agent(s), and records `sandbox_check: "overridden"` and `sandbox_check_failure` in `sessions/<agent>.jsonl` (also surfaced by `robomate status` and repeated by `start-run.py` at startup and in its final report). An overridden agent **will not run with proper sandboxing** (commands may run outside the harness sandbox, behind only the harness permission gate, or not at all) and **may not work at all** if the harness cannot run commands without its sandbox; Claude Code still receives `failIfUnavailable: true` by default under the override, which may stop it at startup. A later `resume` does not inherit the override and re-checks the sandbox unless `--allow-unsandboxed` is passed again. Prefer fixing the host with `scripts/host-setup/ubuntu-24.04/install-bwrap-apparmor.sh`. The nested userns probe detects the restrictive profile that Claude's startup check alone misses. Operators launching agents by hand should run the verifier themselves before launch.


These grants were measured for **Claude Code 2.1.312** and **Codex 0.164.0**
in the #316 run. Harness sandboxes change often; M3's `robomate certify`
(§9.3) re-measures them, including branch creation, commit and push. Save the grants for each repository and agent in the harness configuration.

For a generated worktree, run `git rev-parse --git-common-dir` there to find
`<common>`; resolve a relative result against that working directory. A
linked worktree's `.git` is a file, not the common directory. With a hub on
the main checkout, `<common>` is that main checkout's `.git` directory.
The four-root example below is for generated linked worktrees. Remote full
clones need grants for their own Git layout.

**Claude Code 2.1.312 (Linux).** Add these settings (substitute the actual
hub token directory) to the harness settings used for the session:

```json
{
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true,
    "network": {
      "allowedDomains": ["github.com", "api.github.com"]
    },
    "filesystem": {
      "denyRead": ["<root>/<hub>/token"]
    }
  }
}
```

No filesystem write grant is needed for a linked worktree: Claude Code makes
the common directory writable and protects `config` and `hooks/` itself.
Both hosts are needed: in this run Alice's first `gh` command with only
`github.com` allowed was denied with `deny network-outbound api.github.com:443`
and `Post https://api.github.com/graphql: not in this command's allowed_domains`;
the same command succeeded after allowing `api.github.com`.

The token `denyRead` was measured by the operator on 2.1.312: an empty tmpfs
hides the directory inside the Bash sandbox, so opening the token fails with
`No such file or directory`. The MCP bridge runs outside that sandbox and
still reads it; this run's Claude Code agents joined with the deny configured.
Operator-typed `!` shell commands run outside the sandbox and cannot measure
these restrictions. Agents never open the token themselves.

**Codex 0.164.0 (Linux).** Set `workspace-write` in the session's `config.toml`.

```toml
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true
writable_roots = [
  "<common>/worktrees/<agent>",
  "<common>/objects",
  "<common>/refs",
  "<common>/logs",
  "<uv-cache>",
]
```

Replace `<uv-cache>` with the absolute path printed by `uv cache dir`
(normally `~/.cache/uv` on Linux). The cache is a fifth root needed for this
repository's `uv run` checks: without it the operator measured `Could not
acquire lock` / `Read-only file system (os error 30)` under that cache.
The worktree itself and the harness's temporary directory are already writable.
Never grant `<common>` itself: that opens shared `config` and `hooks/` while
Codex still protects the worktree's own gitdir inside it. Grant the four
specific git paths above instead. The launcher's `workspace-write` settings
provide no path-specific read-deny. Newer Codex releases support separate
named permission profiles with filesystem denies. Those profiles are outside
the measured `workspace-write` grants used by this launcher.

On both harnesses, create branches with
`git switch --no-track -c <branch> origin/<default>` and push with
`git push origin <branch>` without `-u`, because upstream tracking writes
protected git config. Agents never delete branches, local or remote; Codex
cannot take `packed-refs.lock` under the narrow grant. Branch cleanup is an
operator action (Step 8's `robomate clean`).

### Hub token controls by harness

Joined agents use the configured robomate MCP tools for all hub access. The
bridge reads the token on their behalf. The guides forbid agents from reading
the state or token directory, regardless of the harness controls below.

| Harness | Launcher control and limits |
| --- | --- |
| Claude Code | Sandbox `denyRead` hides the token directory from sandboxed shell commands. The bridge runs outside that sandbox. Native Windows sandbox availability varies. |
| Codex | The token boundary is guide-only with the launcher's `workspace-write` settings, whose roots control writes. [Codex 0.170.0 configuration schema](https://github.com/openai/codex/blob/rust-v0.170.0/codex-rs/core/config.schema.json) |
| OpenCode | **2.x (Linux, verified on `2.4.1`):** headless starts and resumes launch with `--standalone` and an ordered `agents.<name>.permissions` array that denies `external_directory` (`*`) and outside/traversal paths for filesystem tools (`read`, `edit`, `write`, `glob`, `grep`, `list`), allows the assigned roots, re-appends inherited filesystem `deny`/`ask` rules after the root allows while dropping broad, leading-wildcard, and outside-root allows, and appends `external_directory` and `read` denies for the token directory (both absolute and relative to the worktree; verified live on Linux, including when the token directory is inside an allowed root). On 2.x, `external_directory` applies to the shell's working directory and parsed directories, not to file arguments such as `cat /path` (verified live on `2.4.1`), and robomate's 2.x policy configures no `shell` path check — shell commands retain OS access. Built-in outside-path reads (such as tool-output files outside assigned roots) are intentionally denied by `read /*`, matching 1.x's boundary, while the `skill` tool itself remains allowed. **1.x:** adds `read` and `external_directory` denies for the token directory after inherited read rules and launcher directory allows (shell path checks are best effort and shell processes retain OS access). Interactive launches use the harness's configured permissions. Native Windows enforcement is tracked in [#711](https://github.com/RoboNater/robomate/issues/711). |
| AntiGravity | The launcher uses the run's `--dangerously-skip-permissions` mode and supplies no per-launch token-directory deny. Antigravity documents user-configured `read_file` deny rules. [Antigravity CLI permissions](https://antigravity.google/docs/cli-permissions) |
| Devin CLI | The launcher uses the run's `dangerous` mode and supplies no per-launch token-directory deny. The guide supplies the boundary for this mode. [Devin CLI investigation](harness-investigation-devin-cli.md) |

## Answering escalations

When Alice needs a decision only you can make, she calls `ask_user`. The hub
keeps the question and sets the workflow to `escalated`, and `robomate status`
shows the count as `Operator questions: N  (see robomate inbox)`. Run these
from the run's `hub-target` checkout, or from anywhere with
`--hub <name | hub_id | checkout path>`:

```sh
uv run --project /absolute/path/to/robomate robomate inbox
uv run --project /absolute/path/to/robomate robomate answer 3 "yes, file it"
uv run --project /absolute/path/to/robomate robomate answer 4 --option 2
```

`inbox` lists each open question with its id, when it was asked, the asking
actor, the question, and any numbered options (`--json` prints the same list as
JSON). `answer` takes either the answer text, quoted, or `--option N` to send
option N's text, and prints `Answered question <id>: <answer>`. The commands are
the same in PowerShell; quote the answer with `"..."` or `'...'`.

A question takes one answer: a second `answer` to it is refused, because Alice
may already have acted on the first. Answering sends `hub.answer` with the operator token `robomate
up` created beside the machine's hub registry, which agents never hold. The hub
records the answer, its time, and actor `operator`, and Alice receives it as a
`user_answered` event. Workers can read the answer with
`get_operator_answer(question_id)`. Agent text in the inbox is shown with
control characters escaped.

Asking or answering in Alice's chat is not an escalation path: only an answer
given with `robomate answer` counts as an operator decision.

## Status, shutdown, and report

When the workflow is finished, Alice releases the workers. In the hub's owning
checkout, stop the hub you started with:

```sh
uv run --project /absolute/path/to/robomate robomate down
```

### The run headline

`robomate status` opens with a headline block between rules, then the hub and
agent detail described below:

```text
── RUN 20261123_14b361f1 ─────────────────────────────────────────────
State:     ACTIVE
Questions: 0 open operator questions
Step:      REVIEW r1 (charlie)   approved: no
PR:        #649 @ e04bed6   for #377   started 2026-11-23 05:21:12 (42 m ago)
Delivery:  pending
Findings:  0 unresolved
Closeout:  steps not recorded
Gate:      14:02:11 (4 m ago)  CI pending (test, windows)  head matches: yes  base behind: no  mergeable: clean (CLEAN)
Agents:    3 alive · 0 stalled · 0 lost    Queue: 0
──────────────────────────────────────────────────────────────────────
```

- `State` is upper-case on its own line, so a `watch` or `grep` catches
  `ESCALATED` (or any other state needing you) at a glance.
- `Questions` is the open operator-question count with
  `(see robomate inbox)` when it is not zero. `--inbox` prints the same content
  as `robomate inbox` directly below the headline; without the flag, question
  text never appears in `status`.
- `Step` names the current open task's role and assignee (with its review round
  `r1`, `r2`, … where the task is a review) and whether a typed reviewer
  approval is bound to the head under review. The `PR` line carries the PR or
  MR number and short head. With no open task, `Step` names the last terminal
  step instead.
- `PR` names the run's PR or MR number and short head, the issue it addresses
  (or the statement's source and short SHA-256 when the statement names no
  issue), and the start time with its age. The date appears when the run did not
  start today and ages roll over to hours and days. Before a submitted result
  binds a PR, the line names the hub's run branch instead —
  `none recorded (branch robomate/<hub>/<agent>/<label>)` — and still shows the
  issue and start.
- `Delivery` shows the hub's recorded delivery state. `Findings` counts
  unresolved typed finding IDs. `Closeout` counts incomplete steps when the
  checklist or its results are recorded.
- `Gate` is the latest stored merge-gate reading for the run: time and age, the
  CI summary with pending or failed check names, `head matches`,
  `base behind` and `mergeable` with the forge's merge-state status. With no
  reading it says `no gate reading yet`. **`--live`** makes read-only forge
  queries, prints the gate reading labelled `live`, and writes nothing; a forge
  failure prints `live failed: …` and falls back to the stored reading without
  failing `status`. With no recorded PR, `--live` first looks up the open PR/MR
  for the run branch in your own repository (`gh pr list --head` on GitHub, the
  equivalent `glab` query on GitLab): exactly one match is used and labelled
  `found by branch` on the `PR` line, then its gate is read like a recorded
  PR's. On that reading, `head matches` compares the PR against the head the
  lookup itself returned, not a recorded or approved head. Zero or several
  matches show none and name the reason. A PR found this
  way is never written back; binding still comes only from a submitted result.
  With a recorded PR, `--live` uses it and makes no branch query. Both use only
  your existing `gh`/`glab` login.
- `Agents` counts alive, stalled, lost and released agents and the queue length.
  **`--activity`** adds one line per agent, the orchestrator included: the last
  hub call or action, its UTC time and age, and the task it concerns or
  `holding`.

`--brief` prints only the headline block (including `--activity` lines when
asked), for `watch -n 2 robomate status --brief`. `status` makes no forge call
unless `--live`, so it stays cheap.

Below the headline, every agent line also shows its current task role, last
call, last progress as `HH:MM:SS (N m ago)`, and the path of its harness
session log where the hub's session directory has one. The latest progress
note is escaped and truncated to one inert line; `--notes` prints the full
escaped note. Notes are untrusted agent text, are never interpreted, and never
appear under `--brief`.

`--json` carries the same facts additively: a `run` object (status, created,
age, branch, PR/MR URL, head and approved SHAs, issue or statement source and
SHA, review rounds used, current and last task, `delivery_state`,
`unresolved_findings` and `incomplete_closeout_steps`), a `gate` object (or `null`), and
per agent `last_call_at`, `last_progress_at`, `last_progress_age_s`,
`current_task_role`, `latest_note`, `latest_note_escaped` and `session_log`.
The top-level `idle_after_s` is the hub's not-listening threshold.
`--live` adds `live_gate` and `live_gate_error`; a `live_gate` reading carries
its `pr_url` and `pr_found_by_branch` so tools can tell a branch lookup from a
recorded binding.
Existing keys and their meaning are unchanged. A hub older than these fields
still prints, with `not reported by this hub` where they are missing.

`robomate status` marks an agent `STALLED` when its bridge still heartbeats but it has made no hub call, task progress or event consumption for `stall_after_min`, and names the evidence; it is a warning, not a lifecycle change (see [agent recovery](development/agent-recovery.md#an-agent-that-stops-working-but-keeps-running)). Before that threshold, a live agent with no task, no hold and no hub call for the hub's `idle_after_s` (120 s) prints `not listening` with its last hub call: nudge that live session with `Continue.`, or, if its CLI already exited, resume the exact saved conversation with `agent-launcher.py resume <agent>` (takeover only for a conversation that joined). A healthy hold, an open task, a released agent, a lost bridge, a paused or escalated workflow and a done or queued run print their own state instead. `robomate status --snapshot [--stopped-at UTC_TIME]` prints the before-snapshot guarded manual resume asks for, from read-only calls that never take over Alice's session.

`robomate status` also shows closeout collection for the current or most recent
run: not requested, collecting with a deadline, or finalized; submitted/expected
counts; submitted authors and timing labels; and missing authors and reasons.
It shows no retrospective text and changes no collection state. In `status --json`,
these are the additive `closeout.state`, `deadline`, `submitted_count`,
`expected_count`, `submitted`, `missing` and `export_error` fields. The per-run
`export_error` covers only the displayed run. The top-level
`closeout_export_errors` list contains `{run_id, error}` for every outstanding
export failure, so failures stay visible when a queued or newly started run
becomes current. Each successful retry removes its run from that list.

### Reading a run report

Run `robomate report` for the current run or, when there is none, the most recent
completed run. Use `robomate report RUN_ID --hub HUB` to select another run on a
specific hub. The command needs the operator credential. During a run it prints a
draft. After completion it prints the final report, with delivery evidence, review
findings and their dispositions, and the close-out steps. The status headline shows
the counts without the finding or close-out text.

Find the hub's `state_dir` with `robomate status --json` before stopping it.
You can then open `<state_dir>/runs/<run_id>/report.md` offline. A run exported
before this command
was added may have no `report.md`. With the hub running, the command can render
that run from the database. Treat exported reports as sensitive. Store them under
a private directory or per-user ACL, and review the contents before sharing.
Deleting an exported file or run directory leaves the stored data in `hub.db`
and the hub will respect the deletion once export completed.

### Reading retrospective reports

Once a run reaches done, read `<state_dir>/runs/<run_id>/closeout/index.json` for
the expected-author list, missing reasons and submitted report receipts/filenames.
Open the named `report-<ordinal>.md` files to read each report, even after stopping
the hub. Worker timing `collected_before_closeout` means the report preceded the
later closeout task; Alice's `after_closeout` report follows it. Claims marked
verified include the agent's evidence; the hub does not certify them. Report text
and URLs are escaped literal data and confer no approval or authority. Nothing
is executed, followed or automatically filed from them.

An export failure does not prevent done. Status shows an export error and the hub
logs it; incomplete exports retry on each hub start. Once complete, exports are not
rewritten on restart. Delete exported files or the run directory to remove them
permanently; reports remain in `hub.db` until the hub state is removed. Files are
private on POSIX (0700 directory, 0600 files). On Windows, keep the state directory
under a private per-user ACL. Retrospectives are sensitive run artifacts: review
their contents before sharing. To exercise collection in an isolated scripted run, use
`scripts/mock-alice.py --mcp --closeout` with the usual hub and worker options;
without `--closeout` the script retains its original task/release flow.

Closing Alice's session stops only her bridge; it does not stop the HTTP hub. `down`, `status`, `report`, `inbox` and `answer` act on the hub of the checkout they run in, or the one `--hub <name | hub_id | checkout path>` selects; anywhere else they refuse and list the live hubs rather than pick one, so `down` never stops a hub you did not select. `robomate ls` lists every hub on the machine, live or stopped, with its name, owning checkout, repository and URL; `robomate status` shows one hub's name, checkout, repository, state directory and hub log path (`Log:`, normally `<state_dir>/logs/hub.log`, which holds no token values). If the hub is unreachable, inspect its `hub.json` PID and listener before taking action.

A stopped hub keeps its registry entry, so its name stays taken and its port stays
reserved for its next `up`. When you no longer need a stopped hub in the list, remove
the registry entry and release the port; keep the stored state (these are operator-only; agents never call them):

```sh
uv run --project /absolute/path/to/robomate robomate forget <name | hub_id | checkout path>
uv run --project /absolute/path/to/robomate robomate forget --all   # every stopped hub
```

Forgetting deletes nothing: the hub's database and run reports are kept; hub.json keeps
its identity and last address, records the released port, and clears its pid. Only the
registry entry goes, so the hub leaves `robomate ls` and its port becomes reusable. A
legacy hub's name (state in the checkout's `.robomate/`) becomes reusable too; a hub kept
beside its worktrees keeps its name taken by its directory under the worktree root,
which still holds its state, until you remove or relocate that state. A live hub is never forgotten: stop it first. The forgotten hub's next `up`
takes the first free port from 8420 and says that its port was released and names the
new one, since remote agents configured with the old port need the new one. If another
hub has taken the freed name since, that `up` is refused; rename with
`robomate up --name <new>`. A rename starts a new worktree/branch namespace: agent
worktrees and `robomate/<old>/…` branches made under the old name keep it. To stop
every running hub at once:

```sh
uv run --project /absolute/path/to/robomate robomate down --all
```

This stops each live hub with the operator credential and reports what it stopped;
state and reports are kept. Removing hub state stays a separate, explicit operator
action.

On Linux or macOS, `ss -ltnp 'sport = :8420'` shows the listener for the default port. On Windows, `Get-NetTCPConnection -LocalPort 8420` shows its owning PID. Compare it with the PID in the hub's `hub.json` (in the `state_dir` that `robomate status --json` reports) before stopping a process manually. A merged PR closes its issue when its body contains `Closes owner/repo#N`.

Keep the run's hub state (`hub-target.robomate/` beside the checkout, or a legacy hub's `hub-target/.robomate/`) for its report, including after shutdown.
Submit another statement to reuse a hub for a sequential run, or select a second
checkout and hub for work in parallel. The diagnostic report reads the hub state
that `robomate status` shows. Alice's MCP rows have `boundary=mcp`, `actor=alice`, and nonzero `content_bytes` after calls with text results. Worker HTTP rows have `boundary=a2a`.

```sh
uv run --locked python scripts/hub-report.py --hub /absolute/path/to/my-run/hub-target
```

`--hub` takes a hub name, `hub_id` or checkout path like `robomate status --hub`, and without
it the report is of the hub the current directory's checkout is bound to or owns.
`hub-report.py` asks `robomate status --json` where that hub keeps its state, running or stopped.
For the state directory of an earlier run (a legacy hub's `hub-target/.robomate`), pass it with
`--state-dir`.


The hub records each completed merge-gate call in `gate_reading` (DB schema v13),
independently of call accounting. The report shows the PR/MR heads, CI checks,
mergeability, base freshness, elapsed time, and any gate error code. Check names
are capped at 256 characters in storage and sanitized and truncated for display;
`--no-labels` omits check names and base branch labels. For pre-v13 databases,
the report still extracts readings from Alice's decision rationales. Those old
readings cannot be recovered during migration if Alice never logged them.
Readings taken before workflow initialization retain a NULL workflow ID and are
included in the run's report once its workflow has been initialized.

---

## Clean up

After delivery, stop the hub with `robomate down` in its owning checkout.
Run `robomate clean` there (or select it with `--hub`) to remove the selected
hub's generated worktrees. Clean requires a stopped hub and refuses to remove
dirty worktrees or unmerged work. It keeps durable hub state and run reports.
Workspaces, cleanup and hub lifecycle commands are operator actions.

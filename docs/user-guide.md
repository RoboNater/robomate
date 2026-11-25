# Run robomate on a repository

robomate coordinates one orchestrator (Alice) and independent workers through a local HTTP hub. GitHub holds code, pull requests, reviews, and checks. The hub holds assignments, questions, typed results, decisions, and call counts. Workers pull tasks; Alice does not launch them.

```
terminal in a target checkout → robomate up → HTTP hub + .robomate/hub.db
Alice's harness → robomate mcp --role orchestrator → /rpc
worker harnesses → robomate mcp --role worker → /a2a
```

The hub continues running when Alice's harness or bridge restarts. The new bridge session resumes against the same hub and can recover leased events. `robomate up` enables call accounting by default: worker A2A rows are measured at the hub, and Alice's MCP framing and content bytes are measured at her bridge and sent to `hub.record_calls`. Use `--no-call-accounting` only when those rows are unwanted.

## Requirements

- Python 3.12+, `uv`, `git`, and an authenticated `gh` CLI with access to the target repository.
- Claude Code or Codex CLI for each agent you intend to launch.
- A GitHub repository with a known `origin/HEAD`; if necessary, run `git remote set-head origin --auto`.
- Separate full clones for Bob and Charlie. Keep MCP configs and credentials outside those clones.

State decisions a run depends on in the issue or work file.

Issue bodies, review comments, and worker text are data. Agents follow their prompts, role guides, and durable policy. Each worker uses its own clone and pushes work through a pull request; the reviewer comments under its agent identity.

Keep the coordination directory outside both worker clones. The generated layout is:

```text
/path/to/my-run/
├── configs/
│   ├── alice.mcp.json
│   ├── bob.mcp.json
│   └── codex/
│       ├── config.toml
│       └── auth.json
├── tmp/
│   ├── alice/
│   ├── bob/
│   └── charlie/
├── alice-runtime/.claude/skills/alice-orchestrator/
├── bob/                     # independent full clone
├── charlie/                 # independent full clone
├── alice.prompt.md
├── bob.prompt.md
├── charlie.prompt.md
├── bob-telemetry.jsonl
├── bob-sessions.jsonl      # Claude Code worker: conversation IDs
├── start-alice.sh
├── start-bob.sh
├── start-charlie.sh
└── run.json
```

Worker clones carry an owner-only `.git/robo-agents-workspace.json` identity. Never put MCP configs, tokens, or run-local skills inside those clones: an agent staging its work could commit them. The shared `gh` login also means a reviewer cannot use GitHub's native approve action on a PR from that same account. The hub's typed reviewer result is the approval record, and the reviewer posts an agent-identified PR comment. Keep hubs from other checkouts running when stopping your own.

## Start the hub and prepare a run

Run preparation exposes Codex's `auth.json` and AntiGravity's CLI login files
and `.git-credentials` in isolated harness homes under `configs/`. It tries
a symlink, then a hard link. On Windows, enabling Developer Mode permits
symlinks without elevation and avoids accumulating hard links against NTFS's
per-file link limit. A hard link also requires the source and run to be on
the same volume. Preflight's `checks.credential_exposure` and `run.json` record
each file's method (`symlink`, `hardlink`, `copy`, or `absent`) and link-failure
causes and remedies, without credential contents or operator-home paths.

If both link methods fail, pass **`--allow-credential-copy`** to
`scripts/prepare-run.py` to opt into the supported copy fallback. This flag
also works with `--worker-only` on the worker host; consent is local to that
host, so pass it there separately. Copies are created exclusively with mode
0600 on POSIX; on native Windows they inherit the run directory's ACL, as
other generated private files do. Use a private run directory whose Windows
ACL grants access only to the operator and trusted administrators. Preparation
refuses a POSIX copy on a filesystem that ignores mode 0600.

Copies are snapshots. Token refresh in a run does not update the operator's
login. Hard links can also drift if a harness refresh replaces its login file
by rename rather than rewriting it in place. Codex's refresh behavior has not
been verified here. If a run's login expires, reauthenticate that isolated home
or prepare a fresh run. Preparation reuses existing run-local files rather
than overwriting a refreshed login. Remove copied credentials when retiring a
run, after its agents have stopped; never remove the operator's source files.

### Archive a stopped run

Use the credential-excluding helper instead of archiving the entire directory
with an unrestricted tar, zip, or directory copy:

```sh
uv run --locked python scripts/archive-run.py --run-dir /absolute/path/to/my-run --output /absolute/path/to/my-run.tar.gz
tar -tzf /absolute/path/to/my-run.tar.gz
```

The same Python command works in native PowerShell with Windows paths. The
output must be a new file outside the run. Stop the run before archiving: a
database still being written can be archived in an inconsistent state. The
helper excludes `auth.json`, `antigravity-oauth-token`, `jetski_state.pbtxt`,
`.git-credentials`, and only the AntiGravity
`.gemini/antigravity-cli/settings.json` path (Claude settings remain). It also
excludes files named `token` or `operator-token`, and all symlinks. It includes
the hub database (`hub.db` in the hub's `state/` directory, which sits beside the
checkout in `hub-target.robomate/<hub>/` and, for a legacy hub, in `.robomate/`, with its
`-wal`/`-shm` files and `hub.json`). These exclusions apply equally to copies
and hard links, including older runs. Credentials remain in the stopped run;
after checking the archive, remove the retired run directory using your usual
operator procedure. Logs and task data may still be sensitive: this local
archive is not a scrubber for publishing run logs.

### Prepare the run

A hub belongs to the checkout where `robomate up` runs: a clone's main
checkout or one of its linked worktrees. Until M2 supports sequential workflows
in one hub, give each run a fresh checkout of the target repository. Keep it
inside the run directory so its hub state remains available for `hub-report`
after shutdown. For example:

```sh
mkdir -p /absolute/path/to/my-run
git clone git@github.com:your-org/your-repo.git /absolute/path/to/my-run/hub-target
cd /absolute/path/to/my-run/hub-target
uv run --project /absolute/path/to/robomate robomate up
```

A linked worktree (`git worktree add`) works too and gets a hub of its own,
beside any hub of the main checkout. A new hub keeps its state outside every
checkout, so `git worktree remove` does not delete it: `robomate ls` still lists
the hub and `robomate status --hub <name>` reaches its state. A hub created
before this layout (a legacy hub) keeps its state in the checkout's own
`.robomate/`, where `git worktree remove` would delete it with the run's
history and report; `up` warns about that, and `robomate relocate` moves it out
(below). A bare repository works through a linked worktree; `up` in the bare
directory itself is refused. `up` is refused in a checkout that is bound to a
hub as an agent (an agent's generated worktree); it runs in the hub's owning
checkout. If you created a checkout using `git init` and
`git remote add origin` instead of `git clone`, run `git fetch origin` and
`git remote set-head origin --auto` in it before `robomate up`.

The command creates the hub's state and token under the worktree root, `<parent>/<repo>.robomate/` by default (a `worktree_root` in the repository or user configuration moves it): `<root>/<hub>/state/` holds `hub.json` and `hub.db`, and `<root>/<hub>/token/token` is the owner-only token file in a directory of its own, so a sandbox can be granted the token without the state. The root records the repository it belongs to in `robomate-root.json`; a hub of another repository refuses a root of the same name and asks for a repository-level `worktree_root`. A binding, `robomate-binding.json` in the checkout's git directory, names the hub, its state and its token file (it holds no token) and is how the checkout finds the hub from any directory inside it. `up` prints the hub's name, its state directory, its URL, and bridge environment settings. Keep this terminal open. A second `up` in the same checkout reports the running hub. The first `up` takes the first free port from 8420, skipping ports that are in use or that the host forbids this process to bind (on Windows, WinError 10013: an excluded port range or an exclusive listener). A later `up` reuses the recorded name and port, and a recorded port that another process has taken meanwhile, or that the host now forbids, fails startup rather than moving the hub, as a `--port` does. The name comes from the checkout's directory name; give another with `up --name <name>` at the first `up`. It is then fixed, and unique among the repository's hubs: `up` refuses a name a sibling hub holds, or a directory of that name under the worktree root, rather than pick another. A legacy hub (state already in the checkout's `.robomate/`) keeps running there, with the same `hub_id`, port and token, and gets an owner binding that names `.robomate/`; that directory is excluded from git by the repository's local exclude file. `robomate relocate` moves a stopped legacy hub into the worktree root when you choose (it needs the operator credential): it moves state and token together, rewrites the bindings and the registry entry, and leaves a one-line pointer file in `.robomate/`. A hub's own `config.toml` may not hold a `[forge]` table at the new location, so move it to the repository-level configuration first.

The hub writes its own log as well as to the terminal. `HUB_LOG_LEVEL`
(`DEBUG`, `INFO` the default, `WARNING`, `ERROR` or `CRITICAL`,
case-insensitive; anything else fails startup) sets the level for both
`robomate up` and the `hub` entry point — `up` honours it like the other
environment settings it does not set itself — and `DEBUG` also shows the
sweeper's per-event lines. The persistent file sits at
`<state_dir>/logs/hub.log` (`up` always writes there; the `hub` entry point
also honours `HUB_LOG_FILE`, and `HUB_LOG_FILE=-` disables the file): the
same records as stderr with a UTC timestamp, UTF-8 regardless of the console
code page, rotated at 5 MiB with 5 backups (`hub.log.1`..`hub.log.5`), and
created owner-only (0600) like the other state artifacts. It never holds
token values — only paths. `robomate status` prints the log path as its
`Log:` line.

In the robomate checkout, prepare agent configs and start scripts:

```sh
uv run --locked python scripts/prepare-run.py \
  --hub-repo /absolute/path/to/my-run/hub-target \
  --repository git@github.com:your-org/your-repo.git \
  --run-dir /absolute/path/to/my-run \
  --issue 42 --account your-github-username
```

`--hub-repo` must point to the checkout running `robomate up`, main checkout or linked worktree. Preparation reads that hub's URL from its `hub.json` and its token file path; it does not start a hub or create a token. The configs use the MCP key `robomate`, so tools appear as `mcp__robomate__check_in`, `mcp__robomate__get_state`, and so on. The generated bridge command is `uv run --locked --project /absolute/path/to/robomate robomate mcp --role orchestrator` for Alice and `--role worker` for workers. It passes `ROBOMATE_HUB_URL` and `ROBOMATE_TOKEN_FILE`; no bearer value is copied into a config.

The hub database keeps one workflow even after the hub stops. For the next
issue, use a fresh checkout: clone the target again into the next run directory,
or add a linked worktree, start `robomate up` there, and pass that checkout as
`--hub-repo`. The previous hub can keep running beside it. Preparation
refuses a hub that already has a workflow and reports its stored goal and status.
To resume the existing workflow, use its generated `resume-<agent>.sh` (or
`.ps1`) script from the original run directory on that agent's host. It reads
the saved manifest and conversation metadata while retaining the durable goal
and policy. Do not rerun `prepare-run.py` to resume: even the same issue or work
file can render a different goal after a generator update. This also applies
to older manifests containing `work.roadmap`; keep their saved goal unchanged.

The run directory contains `configs/`, `alice-runtime/`, the worker clones, `*.prompt.md`, `start-*.sh` (or `*.ps1`), telemetry files, and `run.json`. Agent launch scripts use their own working directories. Start Alice, then each worker, in separate terminals. A Codex Alice gets a run-local `CODEX_HOME` with the orchestrator skill and the enabled tools listed in `ALICE_TOOLS` in `scripts/prepare-run.py`; a Codex worker gets the worker tools listed in `TOOLS` in `scripts/run_common.py`. Generated prompts ask each agent to keep working until released and then write its own closeout report. Every auto-started worker, on any harness and on either a local or remote host, runs under `scripts/worker-launcher.py`, which waits for an attached predecessor to detach or become lost and resumes the same conversation with a doubling backoff if the harness exits before Alice releases the worker and logs each wait and conversation ID to `<worker>-sessions.jsonl`; see [agent recovery](development/agent-recovery.md). An auto-started Alice, on any harness, runs under `scripts/alice-launcher.py`, which resumes her same conversation with the same backoff if she exits before the workflow is `done` and logs each launch and wait to `alice-sessions.jsonl`.

When automatic recovery stops, run `"$RUN_DIR/resume-alice.sh"`,
`resume-bob.sh`, or `resume-charlie.sh` on that agent's host with no arguments
(`& "$RUN_DIR/resume-alice.ps1"` on Windows). The script discovers the saved
conversation, captures the current hub before-snapshot and constructs the
prompt while keeping the original workspace, configuration, model and effort.
It explains missing prerequisites and how to retry. Optional `--resume-session`,
`--stopped-at` and advisory-only `--force`, and interactive/missing-log fallbacks,
are described in [manual recovery controls](development/agent-recovery.md#manual-recovery-controls).

Preparation accepts a clone URL or a bare `owner/repo` slug. A slug uses `gh`'s configured SSH or HTTPS protocol. It checks `gh auth status`, the harness versions, the repository's merge setting, and the presence of CI workflows before creating the run. A local repository or `--skip-github-checks` skips the GitHub checks. For GitLab, `--repository` must be the full origin URL (SSH or HTTPS) of the running hub's project: preparation refuses a hub with no recorded `origin` (restart it with `robomate up`, which records the origin) and a `--repository` that differs from it, including SSH vs HTTPS spellings of one project. It then calls the hub's shared preflight: `glab` authentication, project readability with `web_url` match, developer access or more, merge-method compatibility, and CI presence for `--allow-no-ci auto` (`.gitlab-ci.yml` on the default branch, `ci_config_path`, Auto DevOps, with only a definite 404 meaning absent). An explicit `--allow-no-ci true|false` skips the CI probe entirely. It links Codex authentication into the run-local home and reports `codex login status`. A rerun with the same run directory preserves clean clones and their identity files; a dirty clone causes an actionable error. The start scripts quote paths with spaces or shell metacharacters and keep Codex sessions available for inspection.

| Flag | Effect |
|---|---|
| `--alice-harness`, `--bob-harness`, `--charlie-harness` | Select Claude Code or Codex, case-insensitively. Defaults are Claude Code Alice and Bob, Codex Charlie. |
| `--alice-model`, `--bob-model`, `--charlie-model` | Pass the model to the launcher. A worker model also pins `HUB_MODEL`; leave it empty to let the worker declare its model. |
| `--alice-effort`, `--bob-effort`, `--charlie-effort` | Pass reasoning effort through to the launcher. |
| `--no-auto-start` | Open each agent interactively without its rendered prompt; tell the agent to read the prompt once ready. |
| `--merge-method`, `--allow-no-ci` | Set the workflow policy according to the repository's merge settings and CI. |
| `--stall-after-min` | Set the policy's `stall_after_min`: minutes without agent activity before `robomate status` reports a stall (default 20). |
| `--max-resumes`, `--resume-delay-s`, `--resume-max-delay-s`, `--resume-total-s`, `--resume-series-reset-s` | Bound both launchers' automatic resumes after an early exit: an optional count (`--max-resumes N`; 0 disables; default: no count limit), the first wait (default 5 s) doubling up to the cap (default 1800 s, about 30 minutes), the time since the current series' first exit after which resumes stop (default 43200 s, about 12 hours), and the run length that restarts the series at the short delay and refills the budget (default 300 s). |

For a Codex Alice, the generated `start-alice.sh` uses `codex exec -C . --skip-git-repo-check` because `alice-runtime/` is outside a git checkout. Interactive `--no-auto-start` launches omit that exec-only flag. `--bob-provider`, `--charlie-provider`, and capability flags override the worker profiles used by Alice's pairing policy.

For work spanning several issues in **one PR**, use `--work-file /absolute/path/to/statement.md` instead of `--issue`. Name every issue with `owner/repo#number` and state the acceptance criteria. Use one run per PR. `--reservations` optionally names the shared-counter issue. Ordinary runs do no
roadmap decision or completion edit; the PR's `Closes` reference
closes the issue and triage maintains the plan. Only counter-using runs have the implementer bob claim a
value before code uses it, advance the next value, and close out that claim
after merge per the reservation bob proposed in the PR and charlie accepted in review. If no reservations issue was supplied and a counter is needed,
Alice asks for its authoritative shared issue before using a value. `--roadmap`
now fails with an error naming `--reservations`; rename `roadmap` to
`reservations` in standard-run TOML too. Old `run.json` files still load and
resume with their original goals; do not regenerate the goal to resume. `--alice-harness`, `--bob-harness`, and `--charlie-harness` select Claude Code or Codex; model and effort flags pass through to the launchers. `--no-auto-start` launches interactive sessions and leaves you to give each agent its rendered prompt.

### Scripted run start (M2)

Before starting, configure the static, user-level `robomate` MCP entry for each
team harness. A harness used only for Denise needs no MCP entry, because Denise
runs with MCP servers disabled (see the `denise_*` row below). Use a stable
robomate checkout for `--project`, not a run lane. Follow the
[MCP setup prompt](mcp-setup-prompt.md) for the entry and unattended settings,
and run `scripts/check-mcp-tools.py` after updating that checkout. Devin's entry
cannot currently be checked automatically; start-run warns. The preflight checks
entry presence only.

`scripts/start-run.py` (#268) is the joined successor of the standard run
helper below: one command, with no interactive step, starts the hub of a fresh
checkout, the three agents' worktrees, the submitted work and Alice, Bob and
Charlie headless under the launcher's supervision. The run then proceeds to a
merged PR without further input. Nothing is rendered: no team config, prompt
file or start script. The command runs `robomate up`, `robomate workspace`,
`robomate submit` and `scripts/agent-launcher.py start` in order. The same
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

The config is the standard helper's flat TOML; an existing standard-run file
works as it is. Keys match CLI options with underscores, the command line
overrides the file, and `--issue` and `--work-file` exclude each other (a CLI
selection replaces the file's). Unknown keys and wrong types are errors, and
generation refuses to overwrite a file. Paths resolve as in the helper below.

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

#### Asking an agent after the run

`ask` is the `start-run.py` successor of the generated `ask-<agent>` scripts
(#491). The final report prints this command for each agent; it opens that
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
not support `--fork`, so `ask --fork` refuses it on 2.x). The per-harness suppression and
transcript rules are in [Ask an agent after the run](#ask-an-agent-after-the-run);
for the launcher's own behavior see
[agent recovery](development/agent-recovery.md#asking-an-agent).

#### Consulting Denise about a run

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

#### Stopping and resuming one agent mid-run

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

The standard helper's keys, and what replaces each:

| Standard helper | `start-run.py` |
|---|---|
| `repository`, `run_parent_dir`, `run_dir` | Same keys. The clone is `<run_dir>/<hub_name>`; new optional `hub_name` and `hub_port` (0: first free from 8420). |
| `issue` / `work_file` | Same, mutually exclusive. The statement goes to `robomate submit`, with the counter text below. |
| `account` | Same key, required. The statement ends with `Forge comment identity account: <account>.`, which Alice uses for comment identities, as prepare-run put it in her prompt. |
| `reservations` (was `roadmap`) | Same key. The statement names the reservations issue and adds prepare-run's counter and close-out text; without the key it says no issue was supplied. |
| `forge` | Same key, passed to `robomate up --forge`. Empty (the new default) detects the forge from the origin. |
| `alice_*`, `bob_*`, `charlie_*` harness, model, effort | Same keys, passed to `agent-launcher.py start`. Names and roles stay fixed: Alice orchestrator, Bob implementer, Charlie reviewer. |
| `denise_settings_from`, `denise_*` | Defaults to Bob's settings. Choose Alice, Bob, Charlie or `none`; `none` requires an explicit `denise_harness`. Model and effort inherit only when the harness matches. Empty overrides select the harness defaults when changing harnesses. Effective settings and the log directory are saved to `runs/<run_id>/denise/settings.json`; the doctor command is printed once the team joins and again in the final report. `denise_clean_login = true` (`--denise-clean-login`) requests a separate login for Claude Code or Codex; OpenCode and AntiGravity already use one. See [Consulting Denise](#consulting-denise-about-a-run). |
| `auto_start` | Same key. `false` prints each agent's `agent-launcher.py start ... --interactive` command, then keeps the hub up until the run is done, its agents are released, or Ctrl-C. |
| `pytest_workers` | Same key, the agents' `PYTEST_XDIST_AUTO_NUM_WORKERS`; 0 clears an inherited value. |
| `workspace_setup` | Shell command run in each agent worktree before launch, with its isolated cache and temporary directory. `"auto"` (the default, also used when the key is absent or empty) runs `uv sync --locked --all-packages --dev` when the worktree has a `uv.lock` at its root and does nothing otherwise, so targets that are not uv projects start untouched. `"none"` (compared case-insensitively, ignoring surrounding whitespace) disables setup; any other string is run as given. On manual resume it runs again if `.venv` is missing, keeping the setting the start recorded (an explicit `"none"` stays off). A failure stops that agent’s launcher and prints the output; during `start-run` startup, a launcher failure aborts the team and stops the hub, as with other preflight failures. Setup time counts against `join_window_s` (120 seconds by default); a cold sync on a slow link is best given a larger window. Setup output appears on failure. |
| `stall_after_min`, `stall_action_after_min` | Read as `[policy]` keys. |
| `--yes` (the hub is already running) | Not needed: the command starts or reuses the hub itself and never prompts. |
| `--generate-default-config`, `--generate-config` | Same options. A merged config also saves `[policy]`. |

Policy, prepare-run's `merge_method`, `max_review_rounds`, `allow_no_ci`,
`reviewer_harness_differs` and `stall_after_min` among others, is set per run
in `[policy]` or with `--policy k=v` (repeatable, TOML values). It goes to
`robomate submit --policy` over the hub, repository and user configuration
layers. Its keys, tables and value types are checked against the hub's own
policy model before anything starts. Rules that compare two fields, such as
`stall_action_after_min` above `stall_after_min`, are left to `submit`, because
a layer may set the other field. The launcher's resume bounds are top-level
keys: `max_resumes` (-1, the default, is unlimited), `resume_delay_s`,
`resume_max_delay_s`, `resume_total_s` and `resume_series_reset_s`.

Two prepared-run features have new homes. Per-agent `RUN_DIR/closeout/<agent>.md`
reports are collected by the hub before release (#215). It exports them to
`<state>/runs/<run_id>/closeout/`, and the command prints that path. Per-run
instructions, which prepared runs added to the rendered prompts, go in the
statement of work: write them in a `--work-file`. Every assignment carries
the statement's SHA-256, and agents read it verbatim. Agents need no
closeout-report instruction now that the hub requests the reports.

For an agent-driven check without real harnesses, `scripts/mock-harness.py
--install DIR` writes `claude` and `codex` shims: scripted stand-ins that join
through `robomate mcp` and play their roles to a `done` run. Put `DIR` first on
`PATH`, inside the isolated smoke recipe. A local-path repository has forge
`unknown`, which has no orchestrator guide appendix (#481), so set
`forge = "github"` for such a check.

### Standard run helper (until M2)

`scripts/prep-standard-run-area.py` combines creating the run directory, cloning
the target repository into its `hub/` directory, and calling `prepare-run.py`.
It prints a hub start command: run that in another terminal, keep the hub running,
then press Enter in the helper's terminal. Launch the generated agent scripts as
described above. When finished, run `uv run --locked --project /path/to/robomate
robomate down` from the hub checkout. The helper does not start or stop the hub.
Use a new run directory for each new job; an existing hub clone is reused only
if its origin matches `--repository`. `prepare-run.py` still checks hub liveness
and rejects incompatible existing workflows.

From the robomate checkout, the Bash wrapper syncs the locked workspace and runs
the Python helper; paths and arguments containing spaces are preserved:

```sh
scripts/prep-standard-run.sh --repository git@github.com:your-org/your-repo.git \
  --run-parent-dir ~/working --run-dir 01-issue-42 --issue 42 --account your-user \
  --reservations 409 --bob-harness codex --bob-model your-model --bob-effort medium

scripts/prep-standard-run.sh --repository git@github.com:your-org/your-repo.git \
  --run-dir /absolute/path/to/02-work --work-file ./statement.md --account your-user
```

In PowerShell use the corresponding wrapper (each wrapper also works when called
by absolute path from another directory):

```powershell
.\scripts\prep-standard-run.ps1 --repository git@github.com:your-org/your-repo.git `
  --run-dir C:/runs/01-issue-42 --issue 42 --account your-user
```

For reusable settings, generate a flat **TOML** file, edit it, then prepare:

```sh
scripts/prep-standard-run.sh --generate-default-config ./standard-run.toml
# Edit repository, run_dir, account, and either issue or work_file.
scripts/prep-standard-run.sh --config ./standard-run.toml
# Save the merged settings without cloning or contacting a hub:
scripts/prep-standard-run.sh --config ./standard-run.toml --issue 43 \
  --run-dir 03-issue-43 --generate-config ./next-run.toml
```

Both generation modes are alternatives to preparation and refuse to overwrite
an existing file. `--generate-default-config` writes defaults only, ignoring any
other settings; `--generate-config` writes defaults plus the config and CLI
overrides. Neither starts a run. The default template has empty required values
and `issue = 0` (unset), so fill it before preparing. Only TOML is supported;
Python's standard library reads it without an extra dependency.

Config keys match CLI names with underscores, for example:

```toml
repository = "git@github.com:your-org/your-repo.git"
run_parent_dir = "~/working"
run_dir = "01-issue-42"
issue = 42
account = "your-user"
reservations = "409"
forge = "github"
bob_harness = "codex"
bob_model = "your-model"
bob_effort = "medium"
auto_start = true
```

Run names are relative to `run_parent_dir`; absolute `run_dir` paths override it.
Relative `run_parent_dir` and `work_file` values in TOML resolve beside the config;
CLI paths resolve from your current directory. Saved merged configs use absolute
paths. Unknown keys and wrong value types are errors. CLI settings override the
file, and a CLI `--work-file` replaces a configured issue (or vice versa).
Choose exactly one work source. `--alice-harness`, `--bob-harness`, and
`--charlie-harness`, with each agent's `--*-model` and `--*-effort`, customize
launchers; the defaults are Claude, Claude, Codex with no model/effort pins.
`--no-auto-start` overrides `auto_start = true`. `--forge github|gitlab` controls
the printed hub command; the running hub remains the authority for forge
selection. `--yes` skips the prompt only when you have already started that hub.
Use `prepare-run.py` directly for advanced network and remote-worker options.

## Remote workers and network addresses

Start the hub with a dialable address and an explicit public URL:

```sh
uv run --project /absolute/path/to/robomate robomate up \
  --bind 0.0.0.0 --public-url http://192.0.2.10:8420
```

`--bind` controls the listener; `--public-url` controls the agent card and bridge URL. Use `--port N` when the hub should bind a different port. Configure firewalls and forwarding so the public URL reaches that listener. `prepare-run.py` reads the running hub's URL; any `--hub-url`, `--public-url`, or `--hub-port` passed to it must agree with `hub.json`. A remote worker cannot use a loopback URL.

For a joined agent on another host (including a Windows IDE with a hub in WSL),
create a full clone with that host's git. On Bob's host, run:

```sh
uv run --project /absolute/path/to/robomate robomate workspace bob \
  --clone /absolute/path/to/agents/bob \
  --hub-url http://192.0.2.10:8420 --token-file /private/path/hub-token
```

The origin comes from the hub, so this command can run outside the target repository.
Supply a readable copy of the hub's bearer token file, owner-only on POSIX, and keep it
available at runtime. The command rejects unexpanded variables and placeholders,
checks `/healthz`, verifies that the agent card advertises exactly the supplied URL
plus `/a2a`, and authenticates to read the origin before creating any directories.
An unreachable or mismatched hub leaves the destination untouched. A repeated call
reuses a clean clone with a matching identity; it refuses existing work or a different
origin. The identity in `.git/robo-agents-workspace.json` contains no token.

Keep the installed static `robomate mcp` entry token-free. Set the connection in the
agent host's launch environment (PowerShell uses `$env:NAME = 'value'`):

```sh
export ROBOMATE_HUB_URL=http://192.0.2.10:8420
export ROBOMATE_TOKEN_FILE=/private/path/hub-token
cd /absolute/path/to/agents/bob
```

Start the harness there and call `join(name="bob", role="implementer")`. The bridge
uses the explicit URL and token file and detects the clone identity, including from
a subdirectory; the response reports `workspace_kind: "clone"` and its local path.
It creates no worktree on the hub host. `HUB_WORKSPACE` can also explicitly select the
clone. Each host needs access to the origin with its own git credentials.

For prepared runs, pass `--remote-worker bob` when preparing on the hub host. The printed `--worker-only bob` command is run from the robomate checkout on Bob's host, with `--hub-url` set to the public URL and `--token-file` set to the readable path to the hub's token file (`robomate status --json` reports it as `token_file`). The worker host reads that file; the generated config records the path, not the token value. `--worker-only` bootstraps the clone using that host's git and probes that host's harness version. Keep the token file available to the bridge at runtime.

## Manual MCP configuration

For joined runs, follow the [MCP setup prompt](mcp-setup-prompt.md) for the
static, token-free user-level entry and its unattended settings. The
[runtime templates](../runtimes/README.md) are for the older prepared-run
route, which supplies per-run connection and identity settings.
The bridge uses an explicit hub URL and token file when supplied, and can find
and verify the hub bound to its checkout. It does not attach to an unrelated
hub. See [hub discovery](mvp-spec.md#4-local-layout-discovery-multiple-hubs)
for the full selection order.

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

The joined-agent launcher runs model-free checks on every Linux host before start and resume, including hosts outside Ubuntu, and refuses to launch if they fail. It requires `/usr/bin/bwrap` and nested user namespaces; a launcher already inside a sandbox may fail the probe. There is no launcher bypass: fix the host or launch manually after checking the sandbox. Claude Code also gets `failIfUnavailable: true` so it refuses startup rather than silently running without its sandbox. The nested userns probe detects the restrictive profile that Claude's startup check alone misses. Prepared or manually launched agents should run the verifier themselves before launch.


These grants were measured for **Claude Code 2.1.312** and **Codex 0.164.0**
in the #316 run. Harness sandboxes change often; M3's `robomate certify`
(§9.3) re-measures them, including branch creation, commit and push. They
are static per repository and agent, not per run. Keep the prepared route's
run-local configuration when using prepared clones; joined agents can put
these grants in their harness's user configuration.

For a generated worktree, run `git rev-parse --git-common-dir` there to find
`<common>`; resolve a relative result against that working directory. A
linked worktree's `.git` is a file, not the common directory. With a hub on
the main checkout, `<common>` is that main checkout's `.git` directory.
The four-root example below is for generated linked worktrees. Prepared full
clones have a different git layout; their sandbox grants were not measured
in this run. Keep their run-local MCP configuration, but do not assume the
linked-worktree git roots cover a full clone.

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

**Codex 0.164.0 (Linux).** In the session's `config.toml` (the run-local
`CODEX_HOME/config.toml` for prepared runs), use `workspace-write` and:

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
| OpenCode | **2.x (Linux, verified on `2.4.1`):** headless starts and resumes launch with `--standalone` and an ordered `agents.<name>.permissions` array that denies absolute paths outside the assigned roots and parent-directory traversals for filesystem tools (`read`, `edit`, `write`, `glob`, `grep`, `list`), allows the assigned roots, re-appends inherited protective filesystem rules (such as global denies and default `.env` asks) after the root allows, and appends `read` denies for the token directory (both absolute and relative to the worktree; verified live on Linux, including when the token directory is inside an allowed root). OpenCode 2.x performs **no shell path check** — shell commands retain full OS access. Built-in outside-path reads (such as tool-output files outside assigned roots) are intentionally denied by `read /*`, matching 1.x's boundary, while the `skill` tool itself remains allowed. **1.x:** adds `read` and `external_directory` denies for the token directory after inherited read rules and launcher directory allows (shell path checks are best effort and shell processes retain OS access). Interactive launches use the harness's configured permissions. Native Windows enforcement is tracked in [#711](https://github.com/RoboNater/robomate/issues/711). |
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

When the workflow is finished, Alice releases the workers. In that run's
`hub-target` checkout, stop the hub you started with:

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
SHA, review rounds used, current and last task), a `gate` object (or `null`), and
per agent `last_call_at`, `last_progress_at`, `last_progress_age_s`,
`current_task_role`, `latest_note`, `latest_note_escaped` and `session_log`.
`--live` adds `live_gate` and `live_gate_error`; a `live_gate` reading carries
its `pr_url` and `pr_found_by_branch` so tools can tell a branch lookup from a
recorded binding.
Existing keys and their meaning are unchanged. A hub older than these fields
still prints, with `not reported by this hub` where they are missing.

`robomate status` marks an agent `STALLED` when its bridge still heartbeats but it has made no hub call, task progress or event consumption for `stall_after_min`, and names the evidence; it is a warning, not a lifecycle change (see [agent recovery](development/agent-recovery.md#an-agent-that-stops-working-but-keeps-running)). `robomate status --snapshot [--stopped-at UTC_TIME]` prints the before-snapshot a manual `resume-alice` prompt asks for, from read-only calls that never take over Alice's session.

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
permanently; reports remain in `hub.db` until the hub state is removed. A purge
command is out of scope. Files are
private on POSIX (0700 directory, 0600 files). On Windows, keep the state directory
under a private per-user ACL. Retrospectives are sensitive run artifacts: review
their contents before sharing. Prepared runs still also ask agents for their
separate closeout files. To exercise collection in an isolated scripted run, use
`scripts/mock-alice.py --mcp --closeout` with the usual hub and worker options;
without `--closeout` the script retains its original task/release flow.

Closing Alice's session stops only her bridge; it does not stop the HTTP hub. `down`, `status`, `inbox` and `answer` act on the hub of the checkout they run in, or the one `--hub <name | hub_id | checkout path>` selects; anywhere else they refuse and list the live hubs rather than pick one, so `down` never stops a hub you did not select. `robomate ls` lists every hub on the machine, live or stopped, with its name, owning checkout, repository and URL; `robomate status` shows one hub's name, checkout, repository, state directory and hub log path (`Log:`, normally `<state_dir>/logs/hub.log`, which holds no token values). If the hub is unreachable, inspect its `hub.json` PID and listener before taking action.

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
For the next run, use a fresh target checkout and start a new hub there. The run
report reads that run's hub state, the directory `robomate status` shows. Alice's MCP rows have `boundary=mcp`, `actor=alice`, and nonzero `content_bytes` after calls with text results. Worker HTTP rows have `boundary=a2a`.

```sh
uv run --locked python scripts/hub-report.py --hub /absolute/path/to/my-run/hub-target
```

`--hub` takes a hub name, `hub_id` or checkout path like `robomate status --hub`, and without
it the report is of the hub the current directory's checkout is bound to or owns.
`hub-report.py` asks `robomate status --json` where that hub keeps its state, running or stopped.
For the state directory of an earlier run (a legacy hub's `hub-target/.robomate`), pass it with
`--state-dir`.

The report reads SQLite without changing it. `--format json` and `--format md` are available. Call accounting stores byte counts and labels, never payload text. `scripts/measure-call-bytes.py /absolute/empty-run-dir` starts a scratch `robomate up` hub and bridges to replay a published call sequence; `scripts/mock-alice.py --mcp --hub <name | hub_id | checkout path>` connects through an orchestrator bridge to the already running hub it names and a worker.

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

## Ask an agent after the run

For a run started with `scripts/start-run.py`, the successor of the generated
`ask-<agent>` scripts is `scripts/agent-launcher.py ask <agent> --hub
<checkout>` (#491); the scripted run's final report prints the full command for
each agent. It opens the same saved conversation with the same hub-tool
suppression, needs no hub, and is described in the
[scripted run start section](#asking-an-agent-after-the-run) and
[agent recovery](development/agent-recovery.md#asking-an-agent).

For prepared runs, use the generated `ask-alice.sh`, `ask-bob.sh`, or
`ask-charlie.sh` to reopen that agent's last conversation interactively,
including after the hub is stopped:

```bash
"$RUN_DIR/ask-charlie.sh"
"$RUN_DIR/ask-charlie.sh" 019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8 --fork
```

On native Windows use `& "$RUN_DIR/ask-charlie.ps1"` with the same arguments.
Worker-only bundles include the worker's ask script on its own host. Windows
preparation also writes the Bash version for Git Bash. These scripts accompany
newly prepared runs; old run directories do not acquire them automatically.

With no arguments, the script reads `<agent>-sessions.jsonl`, uses the latest
recorded conversation ID, and prints other recorded IDs. An explicit ID as the
argument before `--harness-args` overrides discovery, including when an interactive run has no
session log. IDs use the same exact syntax as recovery; session names, `--last`,
and `--continue` are rejected. **Do not use Claude Code's `-c` to resume an ID**:
it continues the most recent conversation in that directory and ignores the
intended ID. The ask script uses `--resume ID` instead.

Before launching, the script prints the agent, harness, conversation ID, working
directory, and that hub tools are unavailable. It restores the run's working
directory, temporary directory, isolated harness home/config, model and effort.
It uses the run's permission flags by default, and supplies no prompt, launcher,
recovery guard, or hub connection. Ask questions at the harness's interactive prompt. Use
`resume-<agent>` to continue hub work; use `ask-<agent>` after the run and after
the original agent and launcher have exited.

The script never appends to `<agent>-sessions.jsonl`. By default it resumes the
original conversation, so questions become part of that harness transcript.
Forking is opt-in to keep the default a direct reopening and behave consistently
with AntiGravity, which has no CLI fork option. **Use `--fork` when preserving
the original transcript matters**: Claude Code uses `--fork-session`, Codex uses
`codex fork ID`, and OpenCode 1.x uses `--fork` (OpenCode 2.x rejects `--fork`
on interactive `opencode`, so `ask --fork` refuses it on 2.x; only non-interactive
`opencode run --fork` supports forking). AntiGravity and Devin CLI reject
`--fork` before launching; retain a transcript copy before asking there if
needed.

Hub MCP suppression is specific to the harness:

| Harness | Ask configuration |
|---|---|
| Claude Code | `--permission-mode auto`, with `--strict-mcp-config` without `--mcp-config`. |
| Codex | Same `CODEX_HOME`, with `-c mcp_servers.robomate.enabled=false` and `--approve-for-me`. See the [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference). |
| OpenCode | **2.x:** launches with `--standalone` and an `OPENCODE_CONFIG_CONTENT` override setting `mcp.servers.robomate` with `type`, `command` and `"disabled": true` (a partial entry without `type` and `command` is ignored by 2.x), and carries `model` / `model#variant` in `OPENCODE_CONFIG_CONTENT` since interactive 2.x rejects `--model` and `--variant`. **1.x:** same `OPENCODE_CONFIG`, with an [inline override](https://docs.opencode.ai/docs/config/#precedence-order) disabling `mcp.robomate`, with no permission override and `--auto` so the run config's `external_directory: allow` applies; effort is preserved as the build agent's `variant`. |
| AntiGravity | `--dangerously-skip-permissions`, same `HOME`/`USERPROFILE`; temporarily replaces the run home's `.gemini/config/mcp_config.json` with an empty server map and restores the original bytes on exit, including harness errors and ordinary interrupts. |
| Devin CLI (prototype, #224) | Same working directory, `devin --permission-mode dangerous --respect-workspace-trust=false --resume ID`; temporarily replaces the agent's `.devin/mcp_config.local.json` with a disabled `robomate` entry, which also masks a `robomate` server inherited from project or user scope (an empty map would not), restored as for AntiGravity. |

The same permission defaults apply to **every `--no-auto-start` start and resume
script**, for Alice and workers, local and worker-only, in Bash and PowerShell.
Windows worker-only interactive bundles include both `.sh` and `.ps1` forms.
This changes harness access only: role-guide restrictions and operator-only
actions still apply. Resume still runs the identity, attachment and workflow
recovery checks before launching.

All of these interactive scripts (`ask-<agent>`, `start-<agent>` and
`resume-<agent>` with `--no-auto-start`) accept the same per-launch controls:

- `--safe-mode` restores prompting defaults for this launch. Claude Code,
  AntiGravity and Devin omit the run's permission flags (Devin also omits the
  trust bypass). Codex uses `-a on-request`. OpenCode omits `--auto` and overrides
  permissions (1.x: `{"*": "ask", "external_directory": "ask"}`; 2.x:
  `agents.build.permissions: [{"action": "*", "resource": "*", "effect": "ask"}]`
  with `--standalone`) while retaining ask's MCP disable and effort/model settings.
  Other installed harness settings still
  apply. This wrapper option is distinct from Claude Code's own `--safe-mode`.
- `--harness-args` marks the end of wrapper controls. Every subsequent word goes
  to the harness unchanged, in order; put the ID, `--fork`, recovery controls and
  wrapper `--safe-mode` **before** this marker, in any order. Nothing after the
  marker is interpreted, validated or filtered by robomate. Quote words with
  spaces as usual in your shell. Extras follow generated options and precede
  positional IDs or prompts; for `--resume`/`--session`/`--conversation` adapters
  they precede that final option and its ID. Start scripts append extras after
  generated flags. Resume passes them through `agent-recovery.py --shell-command`.

```sh
"$RUN_DIR/ask-bob.sh" --safe-mode --fork ID --harness-args --model 'a model'
"$RUN_DIR/start-alice.sh" --safe-mode --harness-args --model 'a model'
"$RUN_DIR/resume-bob.sh" --safe-mode ID --confirm-stopped --harness-args --model 'a model'
```

PowerShell uses the same spellings and order, for example
`& "$RUN_DIR/ask-bob.ps1" ID --safe-mode --harness-args --model 'a model'`.
The line printed before the harness starts states the permission mode and lists
the extra words. Explicit extras may override generated settings, including
permissions; the printed mode describes the wrapper's generated defaults.
Existing run scripts must be regenerated to acquire these controls.

Harness support was checked against local Claude Code, Codex, OpenCode and
AntiGravity CLI help. Claude's `--permission-mode auto` applies to interactive
`--resume`; OpenCode 1.x's TUI supports `--auto`, `--session` and `--fork` together,
while OpenCode 2.x's TUI supports `--standalone`, `--auto` and `--session` (no `--fork` or `--model`)
([CLI reference](https://opencode.ai/docs/cli/)). Devin's
[command reference](https://docs.devin.ai/cli/reference/commands.md) lists
`--permission-mode` and `--respect-workspace-trust` as general flags, including
interactive REPL use; Devin was not installed on the implementation host, so
live REPL verification remains an operator check. Native Windows live prompt
behavior also needs an operator run. PowerShell 5.1's OpenCode inline JSON
quoting issue (#287) still applies; the override is smaller, but still exists.

For AntiGravity and Devin CLI, do not start recovery or another agent using
that home or working directory while asking. A private
`mcp_config.json.ask-backup` (`mcp_config.local.json.ask-backup` for Devin)
holds the original MCP config.
Ctrl-C, SIGTERM, and POSIX SIGHUP (including terminal/SSH hangup) run cleanup.
A hard kill, power loss, or native Windows console-window close
(`CTRL_CLOSE_EVENT`, which this helper does not handle) cannot run cleanup:
after confirming the ask process has exited, restore that backup over
`mcp_config.json` before using the home again. Both another ask and guarded
recovery refuse while the backup exists. The script never changes
the operator's global MCP configuration.

## Manual workspace bootstrap

Each worker needs its own isolated, non-shallow git clone of your target repository.

Use the provided workspace bootstrapping script (`scripts/bootstrap-workspace.py` or `scripts/bootstrap-workspace.sh`):

### Linux / macOS

```bash
uv run --locked python scripts/bootstrap-workspace.py bob /absolute/path/to/workspaces/bob-repo git@github.com:your-org/your-repo.git
uv run --locked python scripts/bootstrap-workspace.py charlie /absolute/path/to/workspaces/charlie-repo git@github.com:your-org/your-repo.git
```

### Windows (PowerShell)

```powershell
uv run --locked python scripts/bootstrap-workspace.py bob C:\workspaces\bob-repo https://github.com/your-org/your-repo.git
uv run --locked python scripts/bootstrap-workspace.py charlie C:\workspaces\charlie-repo https://github.com/your-org/your-repo.git
```

### What Bootstrap Does
- Clones the target repository to the given canonical path.
- Verifies that the clone is full (non-shallow) and has its own independent `.git` directory.
- Atomically creates `.git/robo-agents-workspace.json` (mode `0600` on POSIX) containing:
  ```json
  {
    "agent": "bob",
    "path": "/absolute/path/to/workspaces/bob-repo",
    "repository": "https://github.com/your-org/your-repo.git",
    "workspace_id": "8f3a...64_hex_chars...1e9b"
  }
  ```
- Rerunning bootstrap on an existing clean clone outputs the existing identity without modifying or deleting files.

---

## Windows paths in JSON configs

Prefer forward slashes in every JSON file (`C:/target/.robomate`,
`C:/workspaces/bob-repo`). A backslash must be escaped as `\\` in JSON
(e.g., `C:\\target\\.robomate`): a single unescaped `\` either fails to parse
(`\w` is an `Invalid \escape`) or silently corrupts the value — `C:\n\robomate`
parses without error as `C:` + newline + carriage-return + `obomate`, so the
failure surfaces later as `uv` or the hub reporting a missing directory.

This covers `ROBOMATE_TOKEN_FILE`, `HUB_WORKSPACE`, `HUB_TELEMETRY_LOG`, and every
checkout/config path inside `--mcp-config` files (`alice.mcp.json`,
`bob.mcp.json`):

- Git Bash spellings such as `/c/work/robomate` do not work inside these JSON
  files; Claude Code and `uv` are native Windows programs, so use a Windows path.
- Keep the drive-letter case that `bootstrap-workspace.py` printed for
  `HUB_WORKSPACE`: `read_identity` compares the stored `path` string against the
  configured value.
- `uv` must be on the PATH of the shell that starts the runtime (Claude Code /
  Codex), because the MCP config invokes it by bare name (`"command": "uv"`).

---

## Reducing permission prompts and handling pauses
- **Reducing Prompts with `--allowed-tools`**:
  Claude Code prompts for confirmation before editing files, running shell commands, or calling MCP tools. You can pre-approve these operations by launching Claude with `--allowed-tools`:
  ```bash
  claude --strict-mcp-config --mcp-config /path/to/my-run/configs/bob.mcp.json \
    --allowed-tools "Read,Edit,Write,TaskOutput,Bash(git *),Bash(gh *),Bash(pytest *),Bash(python3 *),mcp__robomate__*"
  ```
  *(Customize test commands such as `Bash(pytest *)`, `Bash(npm *)`, or `Bash(cargo *)` to match your repository's test runner).*
- **Turn Pauses on Long Holds**:
  Passing `--allowed-tools` reduces permission prompts, but Claude Code may still end its turn while waiting for assignments on long `await_assignment` holds or after completing an individual task step. If Claude pauses while work is pending or while awaiting Alice, re-prompt it:
  ```text
  Continue the worker loop.
  ```
- **Unattended Supervision**:
  To run Bob fully unattended without manual continuation prompts, you can drive him with `scripts/supervise-claude-code.sh`.
  1. Render Bob's launch prompt by copying [`prompts/worker.md`](../prompts/worker.md), replacing `$AGENT_NAME` with `bob`, and saving to `/path/to/my-run/bob.prompt.md`.
  2. Ensure `HUB_TELEMETRY_LOG` in `/path/to/my-run/configs/bob.mcp.json` matches the path passed to the supervisor so release records are observed.
  3. Change into **Bob's clone directory** first (so Claude runs inside Bob's workspace, not the coordination checkout), and launch the supervisor using its absolute path:
     ```bash
     cd /absolute/path/to/workspaces/bob-repo
     CLAUDE_MCP_CONFIG=/path/to/my-run/configs/bob.mcp.json \
     HUB_TELEMETRY_LOG=/path/to/my-run/bob-telemetry.jsonl \
     CLAUDE_WORKER_PROMPT_FILE=/path/to/my-run/bob.prompt.md \
     CLAUDE_WORKER_TOOLS="Bash,Read,Edit,Write,TaskOutput,mcp__robomate__check_in,mcp__robomate__get_role_guide,mcp__robomate__await_assignment,mcp__robomate__report_progress,mcp__robomate__ask_alice,mcp__robomate__submit_result" \
     CLAUDE_WORKER_ALLOWED_TOOLS="Read,Edit,Write,TaskOutput,Bash(git *),Bash(gh *),Bash(pytest *),Bash(python3 *),mcp__robomate__*" \
     /absolute/path/to/robomate/scripts/supervise-claude-code.sh
     ```

---

## Link Codex authentication into `CODEX_HOME`
Codex CLI stores its login session token in `auth.json`. A fresh `CODEX_HOME` directory will not be authenticated by default. Link your existing `~/.codex/auth.json` into Charlie's run-local home directory:

#### Linux / macOS:
```bash
ln -s ~/.codex/auth.json /path/to/my-run/configs/codex/auth.json
```

#### Windows (PowerShell):
```powershell
# Hard link (requires CODEX_HOME to be on the same drive as USERPROFILE, typically C:):
New-Item -ItemType HardLink -Path C:\my-run\configs\codex\auth.json -Target "$env:USERPROFILE\.codex\auth.json"
# Or copy if on a different volume:
# Copy-Item "$env:USERPROFILE\.codex\auth.json" C:\my-run\configs\codex\auth.json
```

Verify that Charlie's `CODEX_HOME` is authenticated:
```bash
# Linux/macOS:
CODEX_HOME=/path/to/my-run/configs/codex codex login status

# Windows (PowerShell):
$env:CODEX_HOME = "C:\my-run\configs\codex"; codex login status
```
*Expected output: `Logged in using ChatGPT` (or your configured login method).*

## Workflow policy parameters

- `Goal` includes the issue or statement of work and explicitly says when no
  reservations issue was supplied. `--reservations N`, `#N`, or `OWNER/REPO#N`
  supplies a counter target; bare numbers resolve against the target repository,
  while other values pass through verbatim for resolution before claiming.
  Ordinary runs require no roadmap decision comment or completion
  edit and no reservations close-out task. Counter-using runs have the
  implementer bob claim a unique value in the shared issue before code uses it,
  advance the next value, and propose the reservation in the PR, have the
  reviewer charlie check it in review, and have bob close out only that claim
  after merge per the reservation bob proposed in the PR and charlie accepted in
  review.
  If no target is available, Alice asks the operator before using a counter;
  logging it only in the hub never establishes a shared claim.
- `GitHub comment identity account`: Your GitHub username (used in comments like `Implementation agent bob on behalf of <username>`).
- `allow_no_ci`: Set to `false` if your repo runs CI (GitHub Actions). Set to `true` if your repository has no automated CI workflows configured so the merge gate will not block on missing workflows.
- `reviewer_harness_differs`:
  - If `true`: Alice requires the reviewer's harness to differ from the implementer's (e.g. Claude Code Bob + Codex Charlie).
  - If `false`: Allows both workers to run on the same harness (e.g. Claude Code for both Bob and Charlie).
- `max_review_rounds`: Maximum number of review remediation rounds before Alice escalates to the operator (default 3).
- `merge_method`: Must match repository settings (`squash`, `merge`, or `rebase`).
- `stall_after_min`: Minutes without substantive hub calls, task progress, or event consumption before the hub reports an agent as stalled (default 20). A diagnostic only: it never fails, reassigns, or stops anything. It sits between `HUB_LOST_AFTER_S` (the bridge heartbeat stopped) and `max_task_lease_min` (one task's total time).
- `closeout_wait_s`: Bounded pre-release closeout collection window in seconds (default 300, maximum 1800). `robomate submit --policy closeout_wait_s=N` and configuration layers may set it; `request_closeout_reports` without `timeout_s` uses it.

---

## The orchestration lifecycle

Once Alice receives the kickoff prompt, she executes the autonomous orchestration loop:

```
┌───────────┐     ┌───────────────┐     ┌──────────────┐     ┌──────────────┐
│  KICKOFF  │ ──▶ │ IMPLEMENT (B) │ ──▶ │  REVIEW (C)  │ ──▶ │ ADDRESS (B)  │
└───────────┘     └───────────────┘     └──────┬───────┘     └──────┬───────┘
                                               │                    │
                                            Approved                │ (changes requested)
                                               │                    ▼
                                               │             ┌──────────────┐
                                               │             │  RE-REVIEW   │
                                               │             └──────┬───────┘
                                               ▼                    │
                                        ┌──────────────┐            │
                        ┌──────────────▶│  MERGE GATE  │ ◀──────────┘
                        │               └──────┬───────┘
                        │                      │
           (clean /     │        Base Moved or │          All Invariants
           approved)    │          Conflicts   ▼               Hold
                        │               ┌──────────────┐            │
                        ├───────────────┤  REBASE (B)  │            │
                        │               └──────┬───────┘            │
                        │                      │Hand-Resolved       │
                        │                      ▼                    │
                        │               ┌──────────────┐            │
                        └───────────────┤  RE-REVIEW   │            │
                                        └──────────────┘            ▼
                                                              ┌──────────────┐
                                                              │ SHA MERGE(A) │
                                                              └──────┬───────┘
                                                                     │
                                                                     ▼
                                                              ┌──────────────┐
                                                              │   WRAP-UP    │
                                                              └──────────────┘
```

1. **Initialization**:
   Alice calls `get_state()`, verifies there is no existing conflicting workflow, and calls `initialize_workflow(goal, policy)`.
2. **Worker Pairing**:
   Bob and Charlie call `check_in()`. Alice evaluates registered workers against `role_policy` and selects the pair.
3. **Implementation**:
   Alice assigns `IMPLEMENT for <owner>/<repo>#<number>` to Bob.
   - Bob fetches `get_role_guide("implementer")`.
   - Bob checks out a new branch in his clone, makes code changes, and runs project tests.
   - Bob pushes the branch, creates a PR (`gh pr create`), and comments: `Implementation agent bob on behalf of <account>`.
   - Bob submits `ImplementerResult` (`outcome="completed"`, `pr_url`, `head_sha`, `commits`, `tests`).
4. **Independent Review**:
   Alice verifies the PR head on GitHub and assigns `REVIEW for <task-id> @ <head-sha> [findings r1-]` to Charlie.
   - Charlie fetches `get_role_guide("reviewer")`.
   - Charlie fetches and checks out that exact `pr_head_sha` in his clone.
   - Charlie runs project tests and reviews the diff against acceptance criteria.
   - Charlie posts a review comment on the PR: `Reviewer agent charlie on behalf of <account>`.
   - Charlie submits `ReviewerResult` (`verdict="approved"` or `"changes_requested"`, `reviewed_head_sha`, findings, tests).
5. **Remediation & Re-review (if needed)**:
   If Charlie requested changes, Alice assigns `ADDRESS` to Bob with the blocking finding IDs. Bob fixes the issues, pushes a new head commit, responds on the PR, and submits his result. Alice then assigns `RE-REVIEW` to Charlie.
6. **Merge Gate Evaluation**:
   When Charlie approves, Alice calls `check_merge_gate(pr_url, expected_head_sha=<approved-sha>)`.
   The gate evaluates the invariant:
   $$\text{verdict} = \text{approved} \land \text{pr\_state} = \text{open} \land \text{head\_matches} \land (\text{ci} = \text{pass} \lor (\text{ci} = \text{no\_workflows} \land \text{allow\_no\_ci})) \land \neg \text{base\_behind\_main} \land \text{mergeable} = \text{clean}$$
7. **Rebase (only if base moved or conflicts exist)**:
   If `base_behind_main` is true or merge conflicts are detected, Alice assigns `REBASE` to Bob. Bob rebases on `main` and pushes with `--force-with-lease`.
   - If conflict-free (`conflict_files: []`), approval is preserved and the workflow returns to the merge gate.
   - If manual conflict resolution occurred, Alice assigns a focused re-review to Charlie, which returns to the merge gate upon approval.
   If the base was already up to date and clean, the workflow proceeds directly to merge without rebasing.
8. **Automated Merge**:
   Once the merge invariant holds, Alice executes the merge in her shell:
   ```bash
   gh pr merge <pr_url> --<merge_method> --match-head-commit <approved_head_sha>
   ```
   Verify the merge with `check_merge_gate`, then delete only the remote branch
   with `git push origin --delete <branch>`. Avoid `--delete-branch` in linked
   worktrees.
9. **Wrap-Up**:
   - Alice logs the final merge details via `log_decision`.
   - Ordinary runs do no roadmap work; triage reflects issue closure. Only when
     the run used a counter does Alice assign `CLOSE-OUT for <merged sha7>` to
     Bob to mark that claim shipped in the run's reservations issue per the
     reservation Bob proposed in the PR and Charlie accepted in review,
     preserving the next value and other claims. Other explicit close-out work
     and follow-ups still apply. Old prepared runs retain their original goal
     obligations.
   - Alice releases Bob and Charlie via `release_agent()`.
   - Alice marks workflow status as `done` via `set_workflow_status()`.
   - Workers observe `release: true` on their next `await_assignment()` and exit.

---

## Alternative Worker Topologies

While a mixed harness (Claude Code Bob + Codex CLI Charlie) provides the strongest model and harness independence, other topologies are fully supported:

### Both Workers on Claude Code
If you prefer running both Bob and Charlie with Claude Code:
1. Provision separate clones for Bob and Charlie via `bootstrap-workspace.py`.
2. In Alice's kickoff policy prompt, set:
   ```json
   "reviewer_harness_differs": false,
   "reviewer_provider_differs": false
   ```
3. Set distinct `AGENT_NAME="bob"` and `AGENT_NAME="charlie"` in their respective external `.mcp.json` files.

### Codex Alice or OpenCode Workers
- PR #72 introduced cross-platform launchers and support for Codex Alice (`codex app-server`) and OpenCode Charlie (`opencode serve`).
- See [`docs/historical/poc/step6-acceptance.md`](historical/poc/step6-acceptance.md#native-windows-and-alternate-harnesses) and [`runtimes/README.md`](../runtimes/README.md) for detailed template configs.

---

## Troubleshooting & Common Pitfalls

| Symptom | Cause | Solution |
|---|---|---|
| Worker `check_in` returns `401 Unauthorized` | Token mismatch | Ensure `ROBOMATE_TOKEN_FILE` points to the running hub's readable token file (`token_file` in `robomate status --json`). |
| Worker `check_in` returns `409 Conflict` | Agent name or workspace collision | Another active process holds that `AGENT_NAME` or `workspace_id`. Terminate stale worker processes and check `get_state()`. |
| Worker startup fails with `ConfigurationError: HUB_WORKSPACE...` | Workspace path not canonical or missing identity | Ensure `HUB_WORKSPACE` is an absolute path to a full clone bootstrapped with `scripts/bootstrap-workspace.py`. Do not point to a worktree. |
| Rerunning bootstrap fails with "existing clone is dirty" | Config files placed inside clone | Remove `.mcp.json`, `.claude`, or other untracked files from the clone. Keep configurations in a directory outside the clone. |
| Alice escalates: "no valid reviewer pair" | Policy constraints violated | If both workers run the same runtime (e.g. Claude Code), ensure `"reviewer_harness_differs": false` in Alice's policy. |
| Alice `assign_task` fails with `409 Conflict` | Worker busy or duplicate event ID | Worker already has an open task, or `event_id` was already assigned. Call `get_state()` to inspect active tasks before retrying. |
| `check_merge_gate` reports `ci == no_workflows` and blocks | Repo has no GitHub Actions CI | Set `"allow_no_ci": true` in Alice's workflow policy if the repo has no automated checks. |
| Codex worker fails with git permission errors | Common-directory writes are outside the sandbox grant | Apply the versioned [Linux sandbox grants](#linux-sandbox-grants): specific git roots plus the uv cache; never grant the whole common directory. Create with `--no-track`, push without `-u`. |
| Worker fails with network errors | Sandbox network grant is missing | Apply the [Linux sandbox grants](#linux-sandbox-grants): Codex needs `network_access = true`; Claude Code needs both `github.com` and `api.github.com`. |
| Codex worker hangs on MCP tool calls | Interactive approval prompt blocking | Add `approval_mode = "approve"` for all worker tools in `config.toml` (see the runtime template; the full list is `TOOLS` in `scripts/run_common.py`). |
| Hub port (`HUB_PORT`, default 8420) already in use | Stale hub listener, or another checkout's hub | Inspect the listener and stop it only if it is your run's. If it belongs to another checkout, leave it and start your hub with `robomate up --port 8521` before preparing from its repository. |
| Hub or `uv` reports a configured path as missing, though the JSON looks correct | Unescaped Windows backslash in a `*.mcp.json` file | Use forward slashes (`C:/target/.robomate`) or escaped backslashes (`C:\\target\\.robomate`). Verify with `python -c "import json; print(json.load(open('configs/bob.mcp.json'))['mcpServers']['robomate']['env']['HUB_WORKSPACE'])"` — a value containing a newline or `r` where a drive letter should be means a `\n`/`\r` escape was parsed. Git Bash `/c/...` spellings also fail here; use a native Windows path. |
| Alice restarts mid-workflow | Session dropped or restarted | Restart Alice pointing to the same running `robomate up` hub. Alice will call `get_state()`, reconcile with GitHub, and resume without re-running completed work. |

---

## Network checks on Windows and WSL2

For a Windows worker connecting to a hub started inside WSL2, start the hub
with `robomate up --bind 0.0.0.0 --public-url http://<WSL-IP>:8420` in the
target repository. Use `--remote-worker bob` when preparing on the hub host.
Run the printed `--worker-only bob` command from the same robomate commit on
the worker host. Its `--token-file` may point through
`\\wsl.localhost\<distro>\...\target.robomate\target\token\token`; the bridge reads it in
place and never copies the bearer value into the config.

Before starting the worker, check the listener from Windows PowerShell:

```powershell
curl.exe -fsS http://<WSL-IP>:8420/healthz
curl.exe -fsS http://<WSL-IP>:8420/.well-known/agent-card.json
```

The agent card URL must be the hub's `--public-url` plus `/a2a`. `curl.exe`
avoids PowerShell's `curl` alias. WSL2's NAT address can change when WSL
restarts; restart the hub with the new public URL and prepare the run again.
A shared `/mnt/c` clone can break workspace identity and owner-only token
permissions, so bootstrap a remote clone with that host's git.

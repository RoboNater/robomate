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

Keep the coordination directory outside both worker clones. The generated layout is:

```text
/path/to/my-run/
├── configs/
│   ├── alice.mcp.json
│   ├── bob.mcp.json
│   └── codex/
│       ├── config.toml
│       └── auth.json
├── alice-runtime/.claude/skills/alice-orchestrator/
├── bob/                     # independent full clone
├── charlie/                 # independent full clone
├── alice.prompt.md
├── bob.prompt.md
├── charlie.prompt.md
├── bob-telemetry.jsonl
├── start-alice.sh
├── start-bob.sh
├── start-charlie.sh
└── run.json
```

Worker clones carry an owner-only `.git/robo-agents-workspace.json` identity. Never put MCP configs, tokens, or run-local skills inside those clones: an agent staging its work could commit them. The shared `gh` login also means a reviewer cannot use GitHub's native approve action on a PR from that same account. The hub's typed reviewer result is the approval record, and the reviewer posts an agent-identified PR comment. Keep hubs from other checkouts running when stopping your own.

## Start the hub and prepare a run

Until M2 supports sequential workflows in one hub, give each run a fresh,
dedicated clone of the target repository. Keep it inside the run directory so
its hub state remains available for `hub-report` after shutdown. For example:

```sh
mkdir -p /absolute/path/to/my-run
git clone git@github.com:your-org/your-repo.git /absolute/path/to/my-run/hub-target
cd /absolute/path/to/my-run/hub-target
uv run --project /absolute/path/to/robomate robomate up
```

The command creates `<target>/.robomate/hub.json`, `hub.db`, and an owner-only token file. It prints its URL and bridge environment settings. Keep this terminal open. A second `up` for the same repository reports the running hub; a later `up` reuses the recorded port. `.robomate/` is excluded from git by the repository's local exclude file.

In the robomate checkout, prepare agent configs and start scripts:

```sh
uv run --locked python scripts/prepare-run.py \
  --hub-repo /absolute/path/to/my-run/hub-target \
  --repository git@github.com:your-org/your-repo.git \
  --run-dir /absolute/path/to/my-run \
  --issue 42 --account your-github-username
```

`--hub-repo` must point to the repository running `robomate up`. Preparation reads its URL from `.robomate/hub.json` and its token file path; it does not start a hub or create a token. The configs use the MCP key `robomate`, so tools appear as `mcp__robomate__check_in`, `mcp__robomate__get_state`, and so on. The generated bridge command is `uv run --locked --project /absolute/path/to/robomate robomate mcp --role orchestrator` for Alice and `--role worker` for workers. It passes `ROBOMATE_HUB_URL` and `ROBOMATE_TOKEN_FILE`; no bearer value is copied into a config.

The hub database keeps one workflow even after the hub stops. For the next
issue, stop the previous hub, clone the target again into the next run directory,
start `robomate up` there, and pass that new clone as `--hub-repo`. Preparation
refuses a hub that already has a workflow and reports its stored goal and status.
To resume the existing workflow, use its original run directory, hub clone,
and goal (the same `--issue` or `--work-file` and roadmap selection).

The run directory contains `configs/`, `alice-runtime/`, the worker clones, `*.prompt.md`, `start-*.sh` (or `*.ps1`), telemetry files, and `run.json`. Agent launch scripts use their own working directories. Start Alice, then each worker, in separate terminals. A Codex Alice gets a run-local `CODEX_HOME` with the orchestrator skill and ten enabled tools; a Codex worker gets six worker tools. Generated prompts ask each agent to keep working until released and then write its own closeout report.

Preparation accepts a clone URL or a bare `owner/repo` slug. A slug uses `gh`'s configured SSH or HTTPS protocol. It checks `gh auth status`, the harness versions, the repository's merge setting, and the presence of CI workflows before creating the run. A local repository or `--skip-github-checks` skips the GitHub checks. It links Codex authentication into the run-local home and reports `codex login status`. A rerun with the same run directory preserves clean clones and their identity files; a dirty clone causes an actionable error. The start scripts quote paths with spaces or shell metacharacters and keep Codex sessions available for inspection.

| Flag | Effect |
|---|---|
| `--alice-harness`, `--bob-harness`, `--charlie-harness` | Select Claude Code or Codex, case-insensitively. Defaults are Claude Code Alice and Bob, Codex Charlie. |
| `--alice-model`, `--bob-model`, `--charlie-model` | Pass the model to the launcher. A worker model also pins `HUB_MODEL`; leave it empty to let the worker declare its model. |
| `--alice-effort`, `--bob-effort`, `--charlie-effort` | Pass reasoning effort through to the launcher. |
| `--no-auto-start` | Open each agent interactively without its rendered prompt; tell the agent to read the prompt once ready. |
| `--merge-method`, `--allow-no-ci` | Set the workflow policy according to the repository's merge settings and CI. |

For a Codex Alice, the generated `start-alice.sh` uses `codex exec -C . --skip-git-repo-check` because `alice-runtime/` is outside a git checkout. Interactive `--no-auto-start` launches omit that exec-only flag. `--bob-provider`, `--charlie-provider`, and capability flags override the worker profiles used by Alice's pairing policy.

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

For Bob, copy [`runtimes/claude-code.mcp.json`](../runtimes/claude-code.mcp.json) to the run directory and fill in the hub URL, token file path, workspace path, harness version, provider, and any pinned model or capabilities. The bridge command remains `robomate mcp --role worker`. `HUB_MODEL` is an operator claim used for pairing: leave it empty when Bob should declare the model he actually uses at check-in. The hub records a mismatch if the claim and declaration differ. Set `HUB_TELEMETRY_LOG` to an absolute path when using the unattended supervisor. Launch Claude Code from Bob's clone:

```sh
cd /absolute/path/to/bob-clone
claude --strict-mcp-config --mcp-config /absolute/path/to/my-run/configs/bob.mcp.json
```

For Charlie, copy [`runtimes/codex.config.toml`](../runtimes/codex.config.toml) into a private run-local `CODEX_HOME`. Its `[mcp_servers.robomate]` entry uses the same hub URL and token file path and identifies Charlie's own clone. Keep `tool_timeout_sec = 330` for long assignment waits and approve the six coordination tools for unattended use. Give Codex access to its own `.git` directory, and feed the worker prompt through stdin:

```sh
cd /absolute/path/to/charlie-clone
CODEX_HOME=/absolute/path/to/my-run/configs/codex codex exec -C . \
  --add-dir /absolute/path/to/charlie-clone/.git --approve-for-me - \
  < /absolute/path/to/my-run/charlie.prompt.md
```

The manual route uses [`prompts/alice.md`](../prompts/alice.md) for Alice and [`prompts/worker.md`](../prompts/worker.md) for each worker. Install the [`alice-orchestrator`](../skills/alice-orchestrator/SKILL.md) skill in Alice's runtime and the [`worker`](../skills/worker/SKILL.md) skill in a Claude worker's runtime. The generated run directory does these steps for you and keeps every config outside the clones.

The bridge discovers a hub from explicit `ROBOMATE_HUB_URL` plus `ROBOMATE_TOKEN_FILE`, from the current repository's `.robomate/`, or from the single live hub in the machine registry. For remote workers and multiple hubs, set the explicit values.

## Status, shutdown, and report

When the workflow is finished, Alice releases the workers. In that run's
dedicated `hub-target` clone, stop the hub you started with:

```sh
uv run --project /absolute/path/to/robomate robomate down
```

Closing Alice's session stops only her bridge; it does not stop the HTTP hub. `down` asks the discovered hub to shut down and leaves another repository's hub alone. If the hub is unreachable, inspect its `hub.json` PID and listener before taking action.

On Linux or macOS, `ss -ltnp 'sport = :8420'` shows the listener for the default port. On Windows, `Get-NetTCPConnection -LocalPort 8420` shows its owning PID. Compare it with the PID in the target repository's `.robomate/hub.json` before stopping a process manually. A merged PR closes its issue when its body contains `Closes owner/repo#N`.

Keep the run's `hub-target/.robomate/` for its report, including after shutdown.
For the next run, use a fresh target clone and start a new hub there. The run
report reads that run's target clone state. Alice's MCP rows have `boundary=mcp`, `actor=alice`, and nonzero `content_bytes` after calls with text results. Worker HTTP rows have `boundary=a2a`.

```sh
uv run --locked python scripts/hub-report.py \
  --state-dir /absolute/path/to/my-run/hub-target/.robomate
```

The report reads SQLite without changing it. `--format json` and `--format md` are available. Call accounting stores byte counts and labels, never payload text. `scripts/measure-call-bytes.py /absolute/empty-run-dir` starts a scratch `robomate up` hub and bridges to replay a published call sequence; `scripts/mock-alice.py --mcp` connects through an orchestrator bridge to an already running hub and a worker.

---

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

- `Goal` (driven by `--roadmap`; default: no roadmap update required):
  - Standard throwaway run: `Address issue <owner>/<repo>#<number>, merge its pull request, and close out with no roadmap edit; record the merge only in the workflow summary.`
  - Roadmap-tracked run (`--roadmap 2`, `--roadmap #2`, or `--roadmap other-org/other-repo#7`): `Address issue <owner>/<repo>#<number>, merge its pull request, and close out. The implementer bob should make a decision on what roadmap (<roadmap>) updates are necessary, if any, when they open the PR and include it as a PR comment so it can be reviewed. After the merge the implementer bob should update the roadmap per the adjudicated PR if necessary. Make sure you include that in bob's initial tasking.` A bare `N` or `#N` renders as `<target repo>#N`; any other value renders verbatim. For `--work-file`, the statement of work is followed by the same close-out: the throwaway `close out with no roadmap edit; record the merge only in the workflow summary` by default, or `close out.` plus the same bob roadmap instructions when `--roadmap` is given.
- `GitHub comment identity account`: Your GitHub username (used in comments like `Implementation agent bob on behalf of <username>`).
- `allow_no_ci`: Set to `false` if your repo runs CI (GitHub Actions). Set to `true` if your repository has no automated CI workflows configured so the merge gate will not block on missing workflows.
- `reviewer_harness_differs`:
  - If `true`: Alice requires the reviewer's harness to differ from the implementer's (e.g. Claude Code Bob + Codex Charlie).
  - If `false`: Allows both workers to run on the same harness (e.g. Claude Code for both Bob and Charlie).
- `max_review_rounds`: Maximum number of review remediation rounds before Alice escalates to the operator (default 3).
- `merge_method`: Must match repository settings (`squash`, `merge`, or `rebase`).

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
   gh pr merge <pr_url> --<merge_method> --delete-branch --match-head-commit <approved_head_sha>
   ```
9. **Wrap-Up**:
   - Alice logs the final merge details via `log_decision`.
   - If the goal names a roadmap target, bob has already posted a proposed decision on what roadmap updates are necessary as a PR comment when opening the PR; after the merge Alice assigns a `CLOSE-OUT for <merged sha7>` task to Bob to update that issue's checkboxes and reservations per the adjudicated decision. For throwaway runs with no roadmap target, close-out is recorded solely in the workflow summary.
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
- See [`docs/step6-acceptance.md`](step6-acceptance.md#native-windows-and-alternate-harnesses) and [`runtimes/README.md`](../runtimes/README.md) for detailed template configs.

---

## Troubleshooting & Common Pitfalls

| Symptom | Cause | Solution |
|---|---|---|
| Worker `check_in` returns `401 Unauthorized` | Token mismatch | Ensure `ROBOMATE_TOKEN_FILE` points to the running hub's readable `.robomate/token`. |
| Worker `check_in` returns `409 Conflict` | Agent name or workspace collision | Another active process holds that `AGENT_NAME` or `workspace_id`. Terminate stale worker processes and check `get_state()`. |
| Worker startup fails with `ConfigurationError: HUB_WORKSPACE...` | Workspace path not canonical or missing identity | Ensure `HUB_WORKSPACE` is an absolute path to a full clone bootstrapped with `scripts/bootstrap-workspace.py`. Do not point to a worktree. |
| Rerunning bootstrap fails with "existing clone is dirty" | Config files placed inside clone | Remove `.mcp.json`, `.claude`, or other untracked files from the clone. Keep configurations in a directory outside the clone. |
| Alice escalates: "no valid reviewer pair" | Policy constraints violated | If both workers run the same runtime (e.g. Claude Code), ensure `"reviewer_harness_differs": false` in Alice's policy. |
| Alice `assign_task` fails with `409 Conflict` | Worker busy or duplicate event ID | Worker already has an open task, or `event_id` was already assigned. Call `get_state()` to inspect active tasks before retrying. |
| `check_merge_gate` reports `ci == no_workflows` and blocks | Repo has no GitHub Actions CI | Set `"allow_no_ci": true` in Alice's workflow policy if the repo has no automated checks. |
| Codex worker fails with git permission errors | Codex protects `.git` directory | Launch Codex with `--add-dir "/path/to/clone/.git"` in addition to `--approve-for-me`. |
| Codex worker fails with network errors | Sandbox disables network access | Add `[sandbox_workspace_write]\nnetwork_access = true` in Charlie's `CODEX_HOME/config.toml`. |
| Codex worker hangs on MCP tool calls | Interactive approval prompt blocking | Add `approval_mode = "approve"` for all six worker tools in `config.toml` (see the runtime template). |
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
`\\wsl.localhost\<distro>\...\target\.robomate\token`; the bridge reads it in
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

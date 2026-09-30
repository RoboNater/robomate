# Empirical GitLab & `glab` Investigation and Prototype Status Report

## 1. Executive Summary & Context

This report documents the empirical investigation, API analysis, safety rail verification, and modular adapter prototyping for early **GitLab forge support** in `robomate`, conducted ahead of Milestone M5 (following [`docs/mvp-spec.md`](mvp-spec.md) §10, §13, and [`docs/feasibility-and-impact-of-supporting-gitlab-centric-workflows.md`](feasibility-and-impact-of-supporting-gitlab-centric-workflows.md)).

### Key Findings
1. **Forge Detection & Multi-Host Discovery**:
   - Detection seamlessly classifies origins into `github`, `gitlab`, or `unknown`.
   - Supports well-known SaaS hosts (`github.com`, `gitlab.com`), explicit repository configuration in `.robomate/config.toml` (`[forge] gitlab_hosts`), local CLI host configurations (`~/.config/glab-cli/config.yml`), and dynamic host auth status probes (`glab auth status --hostname <host>`). CLI probes only execute during `robomate up` (`probe_cli=True`) to avoid slow subprocess probes on shutdown or discovery fallbacks.
   - Host key matching uses exact whole-key regexes under YAML sections to eliminate false-positive substring matches (e.g. `gitlab.example.com` matching `example.com`).
   - Malformed `.robomate/config.toml` files raise a descriptive `DiscoveryError` to notify the operator rather than silently failing open.
2. **Read-Only Gate Facts via `glab api`**:
   - `GitLabGate` reads required gate facts through standard `glab api` endpoints without requiring a dedicated hub forge token.
   - Authoritative head SHA, `diff_refs` (`base_sha`, `head_sha`, `start_sha`), `diverged_commits_count`, and `detailed_merge_status` are acquired through `GET projects/<group%2Fproject>/merge_requests/<iid>?include_diverged_commits_count=true`.
   - CI pipeline and job facts are acquired through `GET projects/<group%2Fproject>/pipelines/<id>/jobs?paginate=true` and `GET projects/<group%2Fproject>/repository/commits/<sha>/statuses?paginate=true`.
   - Pipeline jobs and commit status entries are deduplicated by name, preventing double-counting since GitLab returns pipeline jobs under the commit statuses endpoint as well.
   - Stale pipeline detection compares `head_pipeline.sha` to the MR head SHA; if stale, the gate treats CI as `NO_CHECKS` and continues polling for the new pipeline run.
   - `NO_CHECKS` (transient pending state) versus `NO_WORKFLOWS` (misconfiguration) is determined affirmatively by inspecting `.gitlab-ci.yml` file presence at the head commit, `ci_config_path`, and `auto_devops_enabled`.
3. **Subprocess & Fail-Closed Error Semantics**:
   - Unlike `gh api` (which exits non-zero on HTTP 4xx/5xx), `glab api` exits `0` on HTTP 4xx errors (e.g. 404 Not Found, 403 Forbidden), printing error details to stdout and reporting status codes in stderr as `(HTTP <status>)`.
   - Both `_json` and `_json_list` enforce fail-closed behavior via `_check_result`: non-zero return codes and stderr `(HTTP [45]\d\d)` raise `GitLabGateError`. In `_json_list`, non-list responses (such as error dicts) raise `GitLabGateError`.
   - `_has_ci_config` fails closed on any HTTP or command failure, treating only an affirmative 404 on `.gitlab-ci.yml` as file absence.
4. **Safety Rails & Preflight Enforcement**:
   - Server-side automatic rebase (`automatic_rebase_enabled`) and merge trains (`merge_trains_enabled`) violate the SHA-bound merge invariant (§5 rails). Preflight inspection helper `check_unsupported_project_settings` queries project settings to verify these rails ahead of M5 integration.
   - Merges strictly bind to the approved commit SHA using `glab mr merge <iid> -R <project_repo> --sha <approved_head_sha> --auto-merge=false [--squash] --remove-source-branch --yes` (empirically verified on sandbox).
   - Review comments posted with `glab mr note` create top-level notes that are `resolvable: false`, preventing unresolved discussion blockers.
5. **Modular Architecture & Zero M1 Disruption**:
   - `ForgeGate` protocol in `merge_gate.py` abstracts merge gate evaluation. `GitHubGate` (`MergeGate`) and `GitLabGate` (in `gitlab_gate.py`, with no circular imports) satisfy this protocol and produce standard `GateReport` structures.
   - Role guides compose with forge-specific appendices (`guides/forge/github.md`, `guides/forge/gitlab.md`) at serve time without per-request database state scans.
   - All M1 contracts, databases (schema v12), wire protocols (`hub.schema_version 1`), and tests remain completely undisturbed.

---

## 2. Test Environment & Empirical Setup

Empirical testing was executed against a live self-hosted GitLab instance:
- **Host**: `gitlab-box.local` (GitLab Community Edition 19.4.1, revision `191678a3764`).
- **Sandbox Repository**: `git@gitlab-box.local:RoboNater/robomate-glab-sandbox.git` (`http://gitlab-box.local/RoboNater/robomate-glab-sandbox`).
- **Authentication**: `glab` CLI configured via `~/.config/glab-cli/config.yml` with host-scoped token and SSH git protocol. Note that while `glab auth status --hostname gitlab-box.local` reports `! Invalid token provided` under WSL, API operations and Git operations are fully functional.

---

## 3. Forge Detection & Host Resolution

### 3.1 Origin Parsing
Git remote origin URLs take multiple forms:
- HTTPS: `https://gitlab-box.local/RoboNater/robomate-glab-sandbox.git`
- SSH URL: `ssh://git@gitlab-box.local:2222/RoboNater/robomate-glab-sandbox.git`
- SCP-style SSH: `git@gitlab-box.local:RoboNater/robomate-glab-sandbox.git`

The parser extracts the host by combining standard `urllib.parse.urlparse` (for scheme-based URLs) with a regex fallback (`r"^(?:[^@]+@)?([^:/]+):"`) for SCP-style remotes.

### 3.2 Detection Precedence
To support self-hosted instances without hardcoded domain lists, forge detection follows a strict precedence:
1. **SaaS Well-Known Hosts**: `github.com` maps to `"github"`; `gitlab.com` maps to `"gitlab"`.
2. **Repository Configuration (`.robomate/config.toml` only)**:
   Operators can define known forge hosts in `.robomate/config.toml` (spec §7):
   ```toml
   [forge]
   gitlab_hosts = ["gitlab-box.local", "gitlab.corp.internal"]
   github_hosts = ["github.corp.internal"]
   ```
   If the extracted host matches an entry, the corresponding forge is returned. A malformed TOML file raises `DiscoveryError` to alert the operator.
3. **Local CLI Configuration**:
   The resolver checks `$XDG_CONFIG_HOME/glab-cli/config.yml` (and `~/.config/glab-cli/config.yml`) for `<host>:` under `hosts:`. If present, it resolves to `"gitlab"`. Similarly, `$XDG_CONFIG_HOME/gh/hosts.yml` is checked for `<host>:`. Whole-key line matching (`^[ \t]*<re.escape(host)>:[ \t]*(?:#.*)?$`) prevents false-positive substring matches.
4. **CLI Auth Probe (only when `probe_cli=True`)**:
   At `robomate up` (`probe_cli=True`), if `glab` is installed on PATH, the resolver runs `glab auth status --hostname <host>`. If the host is authenticated, it resolves to `"gitlab"`. A corresponding probe with `gh auth status --hostname <host>` checks GitHub Enterprise hosts. On shutdown (`down`) or general discovery, `probe_cli=False` prevents spawning CLI subprocesses.
5. **Fallback**: Returns `"unknown"`.

---

## 4. `glab api` Query Mapping for `GitLabGate`

The table below contrasts GitHub's `gh` invocations with GitLab's `glab api` queries implemented in `GitLabGate`:

| Fact / Check | GitHub (`gh`) | GitLab (`glab api`) | Notes |
|---|---|---|---|
| **MR / PR Details** | `gh pr view <url> --json state,headRefOid,baseRefName,mergeable,mergeStateStatus` | `GET projects/<group%2Fproject>/merge_requests/<iid>?include_diverged_commits_count=true` | GitLab returns `state`, `sha`, `target_branch`, `diff_refs`, `has_conflicts`, `detailed_merge_status`, and `diverged_commits_count`. |
| **Authoritative Target Tip** | Parsed from `gh api compare/...` (`.base_commit.sha`) | `GET projects/<group%2Fproject>/repository/branches/<target_branch>` (`.commit.id`) | Direct tip query provides authoritative `main_sha`. |
| **Stale Base Detection** | `gh api compare/...` (`.behind_by > 0`) | `diverged_commits_count > 0` | Crucial: requires `?include_diverged_commits_count=true` query parameter. |
| **CI / Checks** | `gh pr checks <url> --json name,bucket,link` | `GET projects/<group%2Fproject>/pipelines/<id>/jobs?paginate=true` and `GET projects/<group%2Fproject>/repository/commits/<sha>/statuses?paginate=true` | Queries jobs with `--paginate`. Deduplicates jobs and commit statuses by name. |
| **Absence of CI** | `gh api repos/.../actions/workflows` (`.total_count == 0`) | `GET projects/<group%2Fproject>/repository/files/.gitlab-ci.yml?ref=<sha>` | Affirmative query checks `.gitlab-ci.yml` at head, project `ci_config_path`, and `auto_devops_enabled`. Fails closed on HTTP errors. |
| **Approvals** | (N/A in PoC - review comment) | `GET projects/<group%2Fproject>/merge_requests/<iid>/approvals` | Inspects `approved` boolean and `approved_by` list. |
| **Discussions / Notes** | `gh pr view ... --json comments` | `GET projects/<group%2Fproject>/merge_requests/<iid>/notes` | Verifies notes and `resolvable` status. |

### 4.1 Detailed Analysis of Queries & Empirical Behavior

#### 1. Merge Request Details & Stale Base Detection
- **Endpoint**: `glab api projects/<group%2Fproject>/merge_requests/<iid>?include_diverged_commits_count=true --hostname <host>`
- **Findings**:
  - `sha`: Top-level string contains the current source branch HEAD commit SHA.
  - `diff_refs`: Provides `base_sha` (the common merge base), `head_sha` (MR head), and `start_sha` (target branch tip when diff was computed).
  - `diverged_commits_count`: Returns `0` when the MR is up to date with the target branch. When new commits land on `main`, `diverged_commits_count` reports the exact number of commits `main` is ahead (verified empirically: created MR !2, advanced `main` by 2 commits, re-queried MR !2; returned `diverged_commits_count: 2`).
  - **Caution**: Omitting `?include_diverged_commits_count=true` results in `diverged_commits_count` being absent/null. The query parameter is mandatory.

#### 2. Authoritative Target Branch Tip
- **Endpoint**: `glab api projects/<group%2Fproject>/repository/branches/<target_branch> --hostname <host>`
- **Findings**:
  - Returns `commit.id`, giving the exact, current `main_sha`.
  - Comparing `diff_refs.base_sha` to `commit.id` provides a reliable secondary check for stale bases even if `diverged_commits_count` is absent.

#### 3. Mergeability & 24 `detailed_merge_status` States
GitLab computes mergeability asynchronously and exposes 24 discrete statuses via `detailed_merge_status`.
`Mergeable` strictly represents git textual conflict status:
- `conflict`, `need_rebase`, or `has_conflicts=true`: Maps to `Mergeable.CONFLICTING`. Textual conflicts or rebase required; halts gate polling immediately and routes to `rebase` task.
- `checking`, `unchecked`, `approvals_syncing`, `preparing`: Maps to `Mergeable.UNKNOWN`. Transient computation states; gate continues polling boundedly.
- `mergeable`, `discussions_not_resolved`, `not_approved`, `requested_changes`, `draft_status`, `blocked_status`, `commits_status`, `ci_still_running`, `ci_must_pass`, `status_checks_must_pass`, `jira_association_missing`: Textually clean (`Mergeable.CLEAN`). The exact blocking reason is stored in `merge_state_status`. This prevents `discussions_not_resolved` from being misidentified as a git conflict requiring a rebase, and prevents blocking states from stalling until the full poll timeout.
- `not_open`: Returned when MR is merged or closed; maps to `Mergeable.UNKNOWN` with `PrState.MERGED` / `CLOSED`.

#### 4. CI Pipelines, Status Checks & `NO_CHECKS` vs `NO_WORKFLOWS`
- **Pipeline & Job Inspection**:
  - `mr.head_pipeline` contains the pipeline running on the MR head.
  - If `head_pipeline.sha != head`: the pipeline is stale (belonging to an older commit). The gate ignores stale pipeline jobs and reports `CiStatus.NO_CHECKS` to poll for the new pipeline run.
  - Jobs are fetched via `glab api projects/<group%2Fproject>/pipelines/<id>/jobs --paginate --hostname <host>`.
  - Statuses map to standard `Check` buckets:
    - `success`, `passed` → `pass`
    - `allow_failure=True` → `skipping` (honours allowed-to-fail jobs)
    - `failed` (with `allow_failure=False`) → `fail`
    - `canceled`, `canceling`, `cancelled` → `cancel`
    - `skipped` → `skipping`
    - `manual` with `allow_failure=True` → `skipping` (optional manual deploy/cleanup steps do not block the gate)
    - `manual` with `allow_failure=False` → `fail` (required manual gate blocks automated merge)
    - `running`, `pending`, `preparing`, `created`, `scheduled` → `pending`
- **Commit Statuses & Deduplication**:
  - External and pipeline statuses are queried via `glab api projects/<group%2Fproject>/repository/commits/<sha>/statuses --paginate --hostname <host>`.
  - Empirically verified on `gitlab-box.local`: `repository/commits/:sha/statuses` returns pipeline jobs as well as external status checks. The adapter deduplicates checks by `name` to avoid double-counting.
- **Distinguishing `NO_CHECKS` vs `NO_WORKFLOWS`**:
  - If no pipeline or checks have reported, the gate queries:
    `glab api projects/<group%2Fproject>/repository/files/.gitlab-ci.yml?ref=<sha> --hostname <host>`
  - If `.gitlab-ci.yml` is present: maps to `CiStatus.NO_CHECKS` (pipeline not yet scheduled; continues polling).
  - If `.gitlab-ci.yml` affirmatively returns 404, and project settings show `ci_config_path == null` and `auto_devops_enabled == false`: affirmative proof that no CI exists; maps to `CiStatus.NO_WORKFLOWS`.
  - Any 401, 403, 500, or command failure raises `GitLabGateError` (fails closed).

#### 5. `glab api` Error Output & Framing Nuances
- **Empirical Observation**: When requesting a non-existent file or resource (e.g. 404 Not Found), `glab api` prints:
  - stdout: `{"message": "404 File Not Found"}`
  - stderr: `glab: 404 File Not Found (HTTP 404)`
  - exit code: `0` (success).
- **Design Impact**: The adapter inspects stderr for `(HTTP [45]\d\d)` patterns and stdout for error objects. `_check_result` raises `GitLabGateError` whenever an HTTP 4xx/5xx code or non-zero return code occurs. In `_json_list`, receiving an error dict instead of a JSON list also raises `GitLabGateError`.

---

## 5. Preflight Checks for Unsupported Project Settings

GitLab provides several merge and pipeline settings that conflict with the client-verified, head-SHA-bound merge rail:

1. **Automatic Server-Side Rebase (`automatic_rebase_enabled`)**:
   - *Behavior*: GitLab automatically rebases MR commits on top of the target branch on the server immediately before merging.
   - *Safety Hazard*: Creates a new commit with a new commit SHA on the server. The merged commit is never reviewed by human/worker reviewers, nor tested by CI on that rewritten SHA.
   - *Preflight Enforcement*: Helper `check_unsupported_project_settings(project_data)` queries `GET projects/<group%2Fproject>` and flags `automatic_rebase_enabled == true`. In M5, this will be wired into orchestrator startup to refuse runs on misconfigured projects.

2. **Merge Trains (`merge_trains_enabled` / `merge_train_enforcement`)**:
   - *Behavior*: Queues MRs into a merge train to be merged asynchronously after speculative merge-pipeline runs. Direct REST API merge invocations are rejected by GitLab.
   - *Safety Hazard*: Asynchronous queuing breaks immediate deterministic verification of the merged head.
   - *Preflight Enforcement*: `check_unsupported_project_settings` flags active merge trains.

3. **Auto-Merge (`glab mr merge` default)**:
   - *Behavior*: If a pipeline is running, `glab mr merge` defaults auto-merge to `true` unless explicitly overridden.
   - *Rail Enforcement*: All automated merge invocations MUST explicitly pass `--auto-merge=false`.

---

## 6. SHA-Bound Merge Invocation & Review Notes

### 6.1 Executable Merge Command
The orchestrator executes the merge via `glab`:
```sh
glab mr merge <iid> -R <project_repo> --sha <approved_head_sha> --auto-merge=false [--squash] --remove-source-branch --yes
```
- `<iid>`: Merge request IID (positional argument; `glab` does not take MR URLs).
- `-R <project_repo>`: Explicitly scopes the command to the target repository.
- `--sha <approved_head_sha>`: Enforces SHA head matching.
- `--auto-merge=false`: Prevents premature auto-merge scheduling.
- `--yes`: Skips confirmation prompts (mandatory for non-interactive subagent execution).

#### Empirical Test Results on `gitlab-box.local`:
1. **Mismatched SHA Test**:
   ```sh
   glab mr merge 1 -R gitlab-box.local/RoboNater/robomate-glab-sandbox --sha 0000000000000000000000000000000000000000 --auto-merge=false --yes
   ```
   *Result*: Failed immediately with exit code 1:
   `PUT .../merge_requests/1/merge: 409 {message: SHA does not match HEAD of source branch: a7f1f5e2fb871446bd8ea71ce3deb7963ff2d751}`
2. **Matching SHA Test**:
   ```sh
   glab mr merge 1 -R gitlab-box.local/RoboNater/robomate-glab-sandbox --sha a7f1f5e2fb871446bd8ea71ce3deb7963ff2d751 --auto-merge=false --yes
   ```
   *Result*: Successfully merged with exit code 0 (`✓ Merged`). Target branch advanced to merge commit `84388471501e9d0b03b0914d00c1cb457a55d89d`.

### 6.2 Review Comments & Non-Resolvable Notes
- Reviewer agents post comments using:
  ```sh
  glab mr note <iid> -R <project_repo> -m "Reviewer agent <name> on behalf of <account>..."
  ```
- *Empirical Verification*: Top-level notes created via `glab mr note` are stored with `resolvable: false`. They do not open discussion threads, ensuring they never trigger `discussions_not_resolved` blockers even if project settings enforce discussion resolution prior to merge.

---

## 7. Modular Architecture & Prototype Implementation

The prototype delivers a clean, modular foundation ready for M5 without disrupting M1:

```
packages/hub/src/agent_hub/
├── merge_gate.py         — Exports ForgeGate protocol, GitHubGate, MergeGate (without circular imports)
└── gitlab_gate.py        — GitLabGate adapter with glab api queries, pagination, and error checking

packages/common/src/agent_hub_common/
└── discovery.py          — detect_forge() supporting config.toml, glab/gh config, and optional up probes

guides/
├── forge/
│   ├── github.md         — GitHub appendix (invariant-critical commands)
│   └── gitlab.md         — GitLab appendix (invariant-critical commands)
└── README.md             — Explains role guides and forge appendices composition

tests/
├── fixtures/gitlab/      — Modelled on and trimmed from live GitLab API recordings
├── test_gitlab_gate.py   — Comprehensive test suite for GitLabGate (19 unit tests)
├── test_forge_detection.py — Hermetic test suite for multi-source forge detection (11 unit tests)
└── test_guides.py        — Verification of forge appendix composition
```

### Design Decisions:
- **`WorkflowPolicy.forge` deferred to M5**: `WorkflowPolicy` enforces strict validation (`extra="forbid"`). Adding `forge` ahead of M5 breaks checked-in default policy prompts and skill dumps. Forge configuration for M1 is handled cleanly via discovery and `hub_info["forge"]`.
- **Fixtures modeled on live recordings**: Fixtures in `tests/fixtures/gitlab/` are trimmed from live responses on `gitlab-box.local` to isolate tested fields (`sha`, `diff_refs.base_sha`, `diverged_commits_count`, `commit.id`, `allow_failure`, `detailed_merge_status`).

### Validation
All validation checks pass cleanly:
```sh
uv sync --locked --all-packages --dev
uv run --locked ruff check .
uv run --locked mypy
uv run --locked pytest
```
783 tests pass (with 2 upstream Starlette/FastAPI testclient deprecation warnings, zero test errors or failures).

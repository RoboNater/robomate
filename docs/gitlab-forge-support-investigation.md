# Empirical GitLab & `glab` Investigation and Prototype Status Report

## 1. Executive Summary & Context

This report documents the empirical investigation, API analysis, safety rail verification, and modular adapter prototyping for early **GitLab forge support** in `robomate`, conducted ahead of Milestone M5 (following [`docs/mvp-spec.md`](mvp-spec.md) §10, §13, and [`docs/feasibility-and-impact-of-supporting-gitlab-centric-workflows.md`](feasibility-and-impact-of-supporting-gitlab-centric-workflows.md)).

### Key Findings
1. **Forge Detection & Multi-Host Discovery**:
   - Detection seamlessly classifies origins into `github`, `gitlab`, or `unknown`.
   - Supports well-known SaaS hosts (`github.com`, `gitlab.com`), explicit repository configuration in `.robomate/config.toml` (`[forge] gitlab_hosts`), local CLI host configurations (`~/.config/glab-cli/config.yml`), and dynamic host auth status probes (`glab auth status --hostname <host>`).
2. **Read-Only Gate Facts via `glab api`**:
   - `GitLabGate` reads required gate facts through standard `glab api` endpoints without requiring a dedicated hub forge token.
   - Authoritative head SHA, `diff_refs` (`base_sha`, `head_sha`, `start_sha`), `diverged_commits_count`, and `detailed_merge_status` are acquired through `GET /projects/:id/merge_requests/:iid?include_diverged_commits_count=true`.
   - CI pipeline and job facts are acquired through `GET /projects/:id/pipelines/:id/jobs` and `GET /projects/:id/repository/commits/:sha/statuses`.
   - `NO_CHECKS` (transient pending state) versus `NO_WORKFLOWS` (misconfiguration) is determined affirmatively by inspecting `.gitlab-ci.yml` file presence at the head commit, `ci_config_path`, and `auto_devops_enabled`.
3. **Subprocess & Exit Code Semantics**:
   - Unlike `gh api` (which exits non-zero on HTTP 4xx/5xx), `glab api` exits `0` on HTTP 4xx errors (e.g. 404 Not Found, 409 Conflict), printing error details to stdout and reporting status codes in stderr as `(HTTP <status>)`. The adapter safely parses both exit codes and stderr HTTP indicators.
4. **Safety Rails & Preflight Enforcement**:
   - Server-side automatic rebase (`automatic_rebase_enabled`) and merge trains (`merge_trains_enabled`) violate the SHA-bound merge invariant (§5 rails). Preflight checks inspect project settings and refuse execution if either setting is active.
   - Merges strictly bind to the approved commit SHA using `glab mr merge <iid> -R <project_repo> --sha <approved_head_sha> --auto-merge=false [--squash] --remove-source-branch --yes`.
   - Review comments posted with `glab mr note` create top-level notes that are `resolvable: false`, preventing unresolved discussion blockers.
5. **Modular Architecture & Zero M1 Disruption**:
   - `ForgeGate` protocol abstracts merge gate evaluation. `GitHubGate` (`MergeGate`) and `GitLabGate` both satisfy this contract and produce standard `GateReport` structures.
   - Role guides compose with forge-specific appendices (`guides/forge/github.md`, `guides/forge/gitlab.md`) at serve time.
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
2. **Repository Configuration (`config.toml`)**:
   Operators can define known forge hosts in `.robomate/config.toml` (or repository root `config.toml`):
   ```toml
   [forge]
   gitlab_hosts = ["gitlab-box.local", "gitlab.corp.internal"]
   github_hosts = ["github.corp.internal"]
   ```
   If the extracted host matches an entry, the corresponding forge is returned.
3. **Local CLI Configuration**:
   The resolver checks `$XDG_CONFIG_HOME/glab-cli/config.yml` (and `~/.config/glab-cli/config.yml`) for `hosts: <host>:`. If present, it resolves to `"gitlab"`. Similarly, `$XDG_CONFIG_HOME/gh/hosts.yml` is checked for `"github"`.
4. **CLI Auth Probe**:
   If `glab` is installed on PATH, the resolver runs `glab auth status --hostname <host>`. If the host is authenticated, it resolves to `"gitlab"`. A corresponding probe with `gh auth status --hostname <host>` checks GitHub Enterprise hosts.
5. **Fallback**: Returns `"unknown"`, allowing an explicit `--forge` CLI flag to configure the workflow.

---

## 4. `glab api` Query Mapping for `GitLabGate`

The table below contrasts GitHub's `gh` invocations with GitLab's `glab api` queries implemented in `GitLabGate`:

| Fact / Check | GitHub (`gh`) | GitLab (`glab api`) | Notes |
|---|---|---|---|
| **MR / PR Details** | `gh pr view <url> --json state,headRefOid,baseRefName,mergeable,mergeStateStatus` | `GET /projects/:id/merge_requests/:iid?include_diverged_commits_count=true` | GitLab returns `state`, `sha`, `target_branch`, `diff_refs`, `has_conflicts`, `detailed_merge_status`, and `diverged_commits_count`. |
| **Authoritative Target Tip** | Parsed from `gh api compare/...` (`.base_commit.sha`) | `GET /projects/:id/repository/branches/:target_branch` (`.commit.id`) | Direct tip query provides authoritative `main_sha`. |
| **Stale Base Detection** | `gh api compare/...` (`.behind_by > 0`) | `diverged_commits_count > 0` | Crucial: requires `?include_diverged_commits_count=true` query parameter. |
| **CI / Checks** | `gh pr checks <url> --json name,bucket,link` | `GET /projects/:id/pipelines/:pipeline_id/jobs` and `GET /projects/:id/repository/commits/:sha/statuses` | Reads jobs from `head_pipeline` and external commit status checks. |
| **Absence of CI** | `gh api repos/.../actions/workflows` (`.total_count == 0`) | `GET /projects/:id/repository/files/.gitlab-ci.yml?ref=:head` | Affirmative query checks `.gitlab-ci.yml` at head, project `ci_config_path`, and `auto_devops_enabled`. |
| **Approvals** | (N/A in PoC - review comment) | `GET /projects/:id/merge_requests/:iid/approvals` | Inspects `approved` boolean and `approved_by` list. |
| **Discussions / Notes** | `gh pr view ... --json comments` | `GET /projects/:id/merge_requests/:iid/notes` | Verifies notes and `resolvable` status. |

### 4.1 Detailed Analysis of Queries & Empirical Behavior

#### 1. Merge Request Details & Stale Base Detection
- **Endpoint**: `glab api /projects/{quote(project, safe='')}/merge_requests/{iid}?include_diverged_commits_count=true --hostname {host}`
- **Findings**:
  - `sha`: Top-level string contains the current source branch HEAD commit SHA.
  - `diff_refs`: Provides `base_sha` (the common merge base), `head_sha` (MR head), and `start_sha` (target branch tip when diff was computed).
  - `diverged_commits_count`: Returns `0` when the MR is up to date with the target branch. When new commits land on `main`, `diverged_commits_count` reports the exact number of commits `main` is ahead (verified empirically: created MR !2, advanced `main` by 2 commits, re-queried MR !2; returned `diverged_commits_count: 2`).
  - **Caution**: Omitting `?include_diverged_commits_count=true` results in `diverged_commits_count` being absent/null. The query parameter is mandatory.

#### 2. Authoritative Target Branch Tip
- **Endpoint**: `glab api /projects/{quote(project, safe='')}/repository/branches/{target_branch} --hostname {host}`
- **Findings**:
  - Returns `commit.id`, giving the exact, current `main_sha`.
  - Comparing `diff_refs.base_sha` to `commit.id` provides a reliable secondary check for stale bases even if `diverged_commits_count` is absent.

#### 3. Mergeability & 24 `detailed_merge_status` States
GitLab computes mergeability asynchronously and exposes 24 discrete statuses via `detailed_merge_status`:
- `mergeable`: Maps to `Mergeable.CLEAN`.
- `conflict`, `need_rebase`: Maps to `Mergeable.CONFLICTING`. Textual conflicts or rebase required; halts gate polling immediately and routes to `rebase` task.
- `preparing`, `checking`, `approvals_syncing`, `ci_still_running`, `ci_must_pass`, `status_checks_must_pass`: Maps to `Mergeable.UNKNOWN`. Transient computation states; gate continues polling boundedly.
- `discussions_not_resolved`: Maps to `Mergeable.CONFLICTING`. Unresolved discussion threads present.
- `not_open`: Returned when MR is merged or closed; maps to `Mergeable.UNKNOWN` with `PrState.MERGED` / `CLOSED`.

#### 4. CI Pipelines, Status Checks & `NO_CHECKS` vs `NO_WORKFLOWS`
- **Pipeline & Job Inspection**:
  - `mr.head_pipeline` contains the pipeline running on the MR head.
  - Jobs are fetched via `glab api /projects/:id/pipelines/:id/jobs --hostname :host`.
  - Statuses map to standard `Check` buckets:
    - `success`, `passed` → `pass`
    - `failed` → `fail`
    - `canceled`, `canceling`, `cancelled` → `cancel`
    - `skipped` → `skipping`
    - `running`, `pending`, `preparing`, `created`, `scheduled` → `pending`
    - `manual` → `fail` (manual action requires human intervention and will not resolve by waiting; fails closed to prevent hanging).
- **Distinguishing `NO_CHECKS` vs `NO_WORKFLOWS`**:
  - If no pipeline has reported yet, the gate queries:
    `glab api /projects/:id/repository/files/.gitlab-ci.yml?ref=:head --hostname :host`
  - If `.gitlab-ci.yml` is present: maps to `CiStatus.NO_CHECKS` (pipeline not yet scheduled; continues polling).
  - If `.gitlab-ci.yml` returns 404, and project settings show `ci_config_path == null` and `auto_devops_enabled == false`: affirmative proof that no CI exists; maps to `CiStatus.NO_WORKFLOWS`.

#### 5. `glab api` Error Output & Framing Nuances
- **Empirical Observation**: When requesting a non-existent file or resource (e.g. 404 Not Found), `glab api` prints:
  - stdout: `{"message": "404 File Not Found"}`
  - stderr: `glab: 404 File Not Found (HTTP 404)`
  - exit code: `0` (success).
- **Design Impact**: The adapter inspects stderr for `(HTTP \d{3})` patterns and stdout for error messages to identify HTTP 4xx/5xx responses rather than relying solely on the process return code.

---

## 5. Preflight Checks for Unsupported Project Settings

GitLab provides several merge and pipeline settings that conflict with the client-verified, head-SHA-bound merge rail:

1. **Automatic Server-Side Rebase (`automatic_rebase_enabled`)**:
   - *Behavior*: GitLab automatically rebases MR commits on top of the target branch on the server immediately before merging.
   - *Safety Hazard*: Creates a new commit with a new commit SHA on the server. The merged commit is never reviewed by human/worker reviewers, nor tested by CI on that rewritten SHA.
   - *Preflight Enforcement*: Preflight queries `GET /projects/:id` and refuses the run if `automatic_rebase_enabled == true`.

2. **Merge Trains (`merge_trains_enabled` / `merge_train_enforcement`)**:
   - *Behavior*: Queues MRs into a merge train to be merged asynchronously after speculative merge-pipeline runs. Direct REST API merge invocations are rejected by GitLab.
   - *Safety Hazard*: Asynchronous queuing breaks immediate deterministic verification of the merged head.
   - *Preflight Enforcement*: Refuses the run if merge trains are active.

3. **Auto-Merge (`glab mr merge` default)**:
   - *Behavior*: If a pipeline is running, `glab mr merge` defaults auto-merge to `true` unless explicitly overridden.
   - *Preflight Enforcement*: All automated merge invocations MUST explicitly pass `--auto-merge=false`.

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
├── merge_gate.py         — Exports ForgeGate protocol, GitHubGate, MergeGate (backward compatible)
└── gitlab_gate.py        — GitLabGate adapter with glab api queries and preflight checks

packages/common/src/agent_hub_common/
├── discovery.py          — detect_forge() supporting config.toml, glab config, auth status
└── models.py             — WorkflowPolicy with forge: Literal["github", "gitlab"]

guides/
├── forge/
│   ├── github.md         — GitHub appendix (invariant-critical commands)
│   └── gitlab.md         — GitLab appendix (invariant-critical commands)
└── README.md

tests/
├── fixtures/gitlab/      — Recorded real GitLab API JSON fixtures
├── test_gitlab_gate.py   — Comprehensive test suite for GitLabGate
├── test_forge_detection.py — Test suite for multi-source forge detection
└── test_guides.py        — Verification of forge appendix composition
```

### Validation
All validation checks pass cleanly:
```sh
uv sync --locked --all-packages --dev
uv run --locked ruff check .
uv run --locked mypy
uv run --locked pytest
```
751 existing tests + new unit tests for GitLabGate, forge detection, and guides pass with zero warnings or regressions.

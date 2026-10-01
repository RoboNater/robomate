# Implementation plan — MVP Milestone M5: GitLab

Status: proposed. Tracking issue [#68](https://github.com/RoboNater/robomate/issues/68);
roadmap [#2](https://github.com/RoboNater/robomate/issues/2), section M5. Decision list and
review items: [#21](https://github.com/RoboNater/robomate/issues/21).

This plan turns [`docs/mvp-spec.md`](../mvp-spec.md) §13 row M5 into a sequence of
PR-sized steps. It is a plan, not a design of record: where it settles something the spec
leaves open, the step that implements it also updates the spec (see
[Spec deltas](#spec-deltas)). §-numbers refer to `docs/mvp-spec.md` unless marked
"PoC §" or "study §" (the
[GitLab feasibility study](../feasibility-and-impact-of-supporting-gitlab-centric-workflows.md)).

M5 is the next milestone after M1, ahead of M2 (§13). It runs on the M1 topology: the hub
from `robomate up`, the `robomate mcp` bridges, and runs prepared by `scripts/prepare-run.py`.

## What M5 has to deliver

From §13:

> **Deliverable:** Forge detection, `GitLabGate` via `glab api`, forge appendix guides,
> preflight for unsupported settings
>
> **Done when:** A multi-round run with a changes-requested round and a stale-base rebase
> merges on the self-hosted GitLab

Inputs:

- [#21](https://github.com/RoboNater/robomate/issues/21): the decision list and the open
  items from the study's final reviews (robo-agents PR #123).
- [#50](https://github.com/RoboNater/robomate/issues/50) / PR #66: the prototype, reported in
  [`docs/gitlab-forge-support-investigation.md`](../gitlab-forge-support-investigation.md).
- [#69](https://github.com/RoboNater/robomate/issues/69): the local instance
  (`gitlab-box.local`), the sandbox project `RoboNater/robomate-glab-sandbox`, its CI runner,
  and the rules for agents.

The study was written for the PoC. Its `httpx` gate, `HUB_GITLAB_BASE_URLS`, hub-held token,
`WorkflowPolicy.forge`, and `{role}.gitlab.md` guide copies predate the MVP spec. This plan
uses it for invariants and pitfalls, not for its design.

Not in M5: `join`, worktrees, `robomate submit`, and `guides/orchestrator.md` (M2);
`robomate certify` and its `glab` check (M3, §9.3); close-out and the run report (M4); a
hub-mediated forge tool (§16).

## Facts from the current code that shape the plan

1. **The GitLab gate exists, but nothing selects it.** `create_app` builds
   `OrchestratorOps(store)`, whose default gate is `MergeGate` (GitHub), whatever the forge.
   `hub_info`, which carries `forge` and `origin`, already reaches `create_app` from
   `robomate up`.
2. **The MR URL picks the host `glab` talks to.** `MergeRequestRef.parse` accepts `http` or
   `https`, any host, and any project path, and `GitLabGate` passes that host to
   `glab api --hostname`. The URL comes from a worker's result, which is untrusted text. Any
   GitLab project on any host that `glab` can reach is accepted. The port is dropped from
   `--hostname`. Whether `glab` attaches an environment token (`GITLAB_TOKEN`) to an
   arbitrary host has not been checked; the design below does not depend on the answer.
3. **Detection is done and runs only at `up`.** `detect_forge` checks well-known hosts,
   `.robomate/config.toml [forge]`, the `glab` and `gh` config files, and then, with
   `probe_cli=True` (only `robomate up`), `glab`/`gh auth status`. `hub.json` records the
   result and `hub.info` and `hub.status` return it. For the sandbox origin it returns
   `gitlab` from `glab`'s config file without a probe, which matters because
   `glab auth status` misreports under WSL (#69). There is no `--forge` override yet (§10
   names one).
4. **The preflight helper is pure and uncalled.** `check_unsupported_project_settings`
   flags automatic rebase and merge trains in an already-fetched project dict. It does not
   look at `merge_method`, `squash_option`, or merged-results pipelines. On the CE 19.4.1
   sandbox, `merge_trains_enabled` and `merge_pipelines_enabled` are absent from the project
   API (they are paid-tier settings), and `squash_option` is `default_off`.
5. **Role guides hard-code GitHub.** `implementer.md`, `reviewer.md`, `rebase.md`, and
   `worker.md` name `gh pr create`, `gh pr view`, `gh pr comment`, and "GitHub". The forge
   appendices are composed only on an explicit `?forge=` query, which `worker_mcp` never
   sends (#66 round 2, r2-1). Both appendices include the merge command, and the GitLab one
   uses `glab mr note`. `guides/README.md` still says the hub composes the appendix when "a
   forge is configured on the hub", which it does not.
6. **`glab` 1.36.0 is the version every live check used.** Its `glab mr merge` defaults
   `--auto-merge` to true, and it has `--sha`, `--squash`, and `--yes`. Its `glab mr note`
   has `-m` and `--unique` and no resolvable flag; newer releases reshape `mr note` (study
   §6.4). `glab api` has `--method`, `-F key=@file`, `--paginate`, and `--hostname`, and
   exits 0 on HTTP 4xx (investigation §4.1.5).
7. **The pre-M2 run path is GitHub-only.** `prepare-run.py` parses a GitHub slug
   (`parse_github_slug`), checks `gh auth` and repository settings (merge method, push
   permission, workflows), and renders "GitHub comment identity account" into Alice's
   prompt. `skills/alice-orchestrator/SKILL.md` has one merge command, `gh pr merge`, and
   `tests/test_runtime_content.py` pins it to the PoC spec's text. `prompts/worker.md`
   mirrors `guides/worker.md`, and a test compares them.
8. **`WorkflowPolicy` is `extra="forbid"` and has no `forge`.** `prompts/alice.md` and the
   orchestrator skill each embed a policy JSON block that a test requires to equal
   `WorkflowPolicy().model_dump()`, so adding a field changes both, and the
   `prepare-run-default` fixtures.
9. **The sandbox is ready for a real run** (#68 comments, 2026-09-30 and 2026-10-01). The
   instance runner `runner01` is online. Sandbox MR !6 is merged, so `main`'s CI runs the
   real tests (about 8 s per pipeline). `GitLabGate` read live returns `ci=pass` on !6 and
   `ci=fail` on !4, the first live terminal CI states. Auto DevOps is off, "Pipelines must
   succeed" is on, and pipelines are branch pipelines only. With that setting on, GitLab
   refuses `glab mr merge` while the head pipeline still runs.
10. **No shared counter moves.** `GateReport` is already forge-neutral, typed results carry
    `pr_url` and `pr_head_sha` unchanged, and nothing here touches the database.

## Design

### #21 decisions

| #21 item | Settled as | Source |
|---|---|---|
| Hub merge tool (study Strategy C) vs merge from the shell | **No hub merge tool.** Alice merges with `glab mr merge --sha`, as she does with `gh pr merge --match-head-commit` | §1 non-goal "Hub-mediated forge operations", §10 "Merge", §14 "Forge operations" |
| Worker strategy A (`glab` CLI) vs C (hub MCP tools) | **A.** Workers run `glab` with the shared operator identity, as they run `gh` | §1 principle 3, §10 "Agents use the CLIs", §12 |
| Are unsupported configurations unsupported permanently? | **Yes, for as long as the merge invariant stands** (below) | this plan |

The first two are already decided by the spec, so M5 only confirms them. On merge of this
plan they are ticked in #21 with a pointer here.

**Unsupported configurations.** The merge invariant (PoC §5 MERGE, §10) is: merge only the
head that a reviewer approved and the gate checked, bound by SHA. These configurations
merge a commit that neither saw:

- server-side automatic rebase (`automatic_rebase_enabled`);
- merge trains and merge-train enforcement;
- merged-results pipelines, whose pipeline SHA is a synthetic merge commit, so the gate
  never sees a pipeline for the reviewed head (it reports `no_checks` until it times out);
- `WorkflowPolicy.merge_method = "rebase"`, which on GitLab is a server-side rebase
  (`glab mr merge --rebase`).

They are unsupported permanently, not as a phase-1 limit. Supporting any of them means
replacing the invariant (for example, "the approved tree, re-tested by the train's
pipeline"), and that is a spec decision to make in §16 if a user needs it, not a preflight
flag. Preflight refuses them where the project API shows them, and the gate fails closed
where it does not.

The project merge methods `ff` (fast-forward) and `rebase_merge` (semi-linear) are **not**
in this list. Both require the MR to be current with its target before it merges, and the
gate already refuses a stale base and routes a client-side REBASE task. The merge then
binds to that task's head with `--sha`. They are supported, subject to the matrix below.

### Gate transport (§15): `glab api`

Decision: **`glab api`**, as §10 recommends and #66 prototyped. The hub holds no forge token
and adds no HTTP client. It runs `glab` with the environment of the operator who ran
`robomate up`, which §12's single-operator model already trusts with every agent's forge
identity. §15's "GitLab gate transport" row is closed.

From the study's `httpx` proposal (study §6.2, §7), these consequences still hold, because
the URL still comes from untrusted text and `glab` still holds a token:

- **The URL must not choose the host.** One hub serves one repository (§14), so instead of a
  `HUB_GITLAB_BASE_URLS` allowlist the gate is **bound to the hub's own project**: the host
  and project path parsed from `origin`. An MR URL for any other host or project is refused
  before any `glab` call, with `MergeGateError` (JSON-RPC -32004). `--hostname` comes from
  the origin, never from the URL.
- **Parser hardening** (#21, adversarial URL tests): `https` only; no userinfo, query, or
  fragment; a port only if it is absent or 443; host compared case-insensitively; project
  path compared segment by segment after rejecting `.`/`..` segments and percent-encoded
  separators; the path must end at `/-/merge_requests/<iid>`, with nothing after it (so not
  `/diffs`). A bad port raises `MergeGateError`, not `ValueError`.
- **Custom CA:** `glab`'s own per-host `ca_cert` or `GLAB_CA_CERT` for the gate, and git's
  `http.sslCAInfo` for agents. The hub has no TLS setting. The sandbox uses a self-signed
  certificate that `glab` already accepts.

These do not carry over: the `httpx` dependency, `HUB_GITLAB_TOKEN*`, the base-URL allowlist
setting, redirect handling (the hub makes no HTTP calls to the forge), and the legacy
`merge_status` fallback for GitLab older than 15.6 (an absent `detailed_merge_status` maps to
`unknown` and fails closed).

**Deferred, failing closed:** GitLab served on a non-default HTTPS port, under a relative URL
root (`https://host/gitlab/…`), or over plain `http`. The origin does not reveal the web URL
for those, so M5 refuses them. Supporting them needs a `config.toml` web-URL setting, which
is a later issue if someone needs it.

### Gate selection and pipelines

- `create_app` chooses the gate from `hub_info["forge"]`: `gitlab` gives a `GitLabGate`
  bound to the origin project, and anything else gives `MergeGate`, as today. A hub without
  `hub_info` (tests, the `hub` entry point) keeps `MergeGate`. The forge is detected again
  on every `up` and passed in `hub_info`, so a restart picks the same gate. Nothing about
  it is stored in the database.
- **Branch and MR pipelines for the same SHA** (#21). The supported configuration is the
  sandbox's: branch pipelines only, which GitLab attaches as the MR's `head_pipeline`. If a
  project also runs detached MR pipelines, there are two pipelines for one SHA. The gate
  reads `head_pipeline`'s jobs, and `commits/<sha>/statuses` returns the jobs of *every*
  pipeline for the SHA, keyed by `pipeline_id`. Jobs of the other pipeline therefore appear
  as extra checks, and all of them must pass. This is the rule: **every pipeline that GitLab
  ran for the head SHA must pass**, rather than whichever one is `head_pipeline`. A
  redundant pipeline that GitLab auto-cancels shows as `cancel`; the gate then polls and
  escalates, which is safe but needs the operator. Merged-results pipelines are unsupported
  (above).

### Preflight

Preflight has two parts. The policy-independent part runs at `robomate up`. The part that
depends on `merge_method` runs where the policy is known: `prepare-run.py` now, and
`robomate submit` or `start_workflow` in M2.

**At `robomate up`, when the forge is `gitlab`:**

| Check | How | On failure |
|---|---|---|
| `glab` present, version ≥ 1.36.0 | `glab version` | warn |
| Authenticated for the origin host | `glab api --hostname <host> user` (not `glab auth status`, #69) | warn |
| Project readable, with developer access or more | `glab api projects/<project>`, `permissions` | warn |
| Automatic rebase, merge trains, merge-train enforcement, merged-results pipelines | `check_unsupported_project_settings`, extended | **refuse** |
| Auto DevOps on | project `auto_devops_enabled` | warn: the gate can never report `no_workflows` |
| "Pipelines must succeed" off | `only_allow_merge_if_pipeline_succeeds` | note only (defense in depth) |

A definite unsupported setting refuses `up`, with the setting's name and how to turn it
off. An inability to check (no `glab`, forge unreachable) only warns. A hub restart must
not depend on the forge being reachable (§7.5), and the gate fails closed on every call
anyway. `up` prints one line per check.

`robomate up --forge github|gitlab` overrides detection (§10) and is recorded in
`hub.json`. When the forge is `unknown`, `up` warns that `check_merge_gate` will use the
GitHub gate.

**Merge-method compatibility matrix** (#21), a pure function next to
`check_unsupported_project_settings`:

| Project `merge_method` | `squash_option` | Policy `merge` | Policy `squash` | Policy `rebase` |
|---|---|---|---|---|
| `merge`, `rebase_merge`, `ff` | `default_off`, `default_on` | accept | accept | refuse |
| same | `always` | refuse (GitLab squashes anyway) | accept | refuse |
| same | `never` | accept | refuse | refuse |

The orchestrator always passes the squash choice explicitly (`--squash` or
`--squash=false`), so `default_on` and `default_off` cannot change the result. Step 4
verifies each accepted cell live on a scratch project, recording the post-merge commit
topology and the MR state.

### Guides: forge-neutral, composed by the hub's forge

- The role guides become forge-neutral ("open a change request with your forge CLI", "read
  the head back from the forge", "the forge is the work-product store"). Every command moves
  to `guides/forge/github.md` or `guides/forge/gitlab.md` (§10). No per-forge copies of role
  guides.
- **Composition is the default.** The guide route appends the appendix for the hub's forge
  (`gitlab` gives `gitlab.md`; anything else gives `github.md`, matching gate selection).
  `?forge=` remains an explicit override. If the appendix for the effective forge is missing,
  the route returns 404 rather than a guide without its forge commands.
- **Appendices are for workers only.** Their merge sections are removed; no worker role
  merges. Until M2, the merge commands live in the orchestrator skill (Step 4). M2's
  `guides/orchestrator.md` gets its own orchestrator-only appendix. A test asserts that no
  served worker guide contains a merge command. This resolves #66's r2-1 concern, which kept
  composition opt-in.
- **GitLab review comments use the Notes REST API** through `glab api`:
  `glab api --hostname <host> --method POST projects/<group%2Fproject>/merge_requests/<iid>/notes -F body=@review.md`.
  The Notes endpoint is part of GitLab's stable REST API, so the command does not change
  with `glab`'s subcommand layout. A top-level note is not a discussion, so it is
  non-resolvable (observed for `glab mr note` in #66) and can never become a
  `discussions_not_resolved` blocker. Step 3 re-verifies `resolvable: false` live. The
  minimum `glab` version, 1.36.0, is still pinned, because the merge flags need it, and
  `up` checks it.
- Byte budget (§11, M6): the served bytes per role, before and after, go in the Step 3 PR.

### `WorkflowPolicy.forge`: not added

§7.2 lists `forge` ("detected, not normally set") as a policy addition, and #66 deferred it.
M5 does not add it. With one hub per repository (§14), the forge is a fact about the hub:
detected at `up`, recorded in `hub.json`, returned by `hub.info`, and, from M2, in the join
response (§5). A policy copy could only disagree with it. Adding it would also change the
policy blocks in Alice's prompt and skill (fact 8) for no reader. Step 4 replaces the §7.2
bullet with "the hub's forge (§4, §10) applies; it is not a policy field". The run
manifest (§11), when it exists, takes the forge from the hub.

### The run path before M2

With M5 ahead of M2 there is no `join`, no worktrees, and no `guides/orchestrator.md`. The
GitLab acceptance run uses the PoC-era path, and this is what needs a forge:

| Piece | Change | Step |
|---|---|---|
| `robomate up` | `--forge`; GitLab preflight | 2 |
| hub gate, guide route | selected by `hub_info["forge"]` | 1, 3 |
| `guides/*.md`, `guides/forge/*.md`, `prompts/worker.md` | forge-neutral guides; worker-only appendices | 3 |
| `scripts/prepare-run.py` | forge from `--hub-repo`'s `hub.json` (no new flag); nested GitLab project paths; GitLab preflight in place of the `gh` checks; forge-neutral identity line | 4 |
| `prompts/alice.md` | "Forge comment identity account"; a `Forge:` line naming host and project | 4 |
| `skills/alice-orchestrator/SKILL.md` | a GitLab section: SHA-bound `glab mr merge`, read-back of the merged MR, `glab` equivalents of the `gh` reads; the `gh pr merge` block unchanged | 4 |
| `skills/worker/SKILL.md`, `runtimes/*` | unchanged (they point at the served guides) | — |

**What M2 inherits,** so that GitLab is not redone:

- Role guides are already forge-neutral and composed at serve time. `guides/orchestrator.md`
  follows the same pattern, with an orchestrator-only appendix per forge holding the merge
  and read-back commands that Step 4 puts in the skill.
- The join response carries the hub's forge (already in §5). Nothing else in M2 needs a
  forge parameter.
- `prepare-run.py`'s GitLab preflight is a thin caller of functions in `agent_hub`. M2's
  `robomate submit` or `start_workflow` calls the same matrix when it removes
  `prepare-run.py` (Appendix B).
- M2's tests keep `tests/test_gitlab_gate.py`, `tests/test_guides.py`, and the Step 1 and 3
  dispatch tests green. M2's done-when stays GitHub-only. M6's acceptance covers GitLab end
  to end (§13).

For M3, certification already checks `glab` as well as `gh` (§9.3), and the M3 run with a
non-Claude orchestrator may be on either forge. For M4, close-out files follow-up issues
through the forge appendix (`glab issue create` on GitLab), and the `done` check uses
`check_merge_gate`'s `merged` state, which `GitLabGate` already reports.

### What #66 left for M5

| Item | Answer | Step |
|---|---|---|
| Wire `check_unsupported_project_settings` into `up`/startup | `up` refuses a definite violation; matrix at `prepare-run.py` (M2: `submit`) | 2, 4 |
| `WorkflowPolicy.forge` | Not added; the forge is a hub fact | 4 (spec) |
| `?forge=` opt-in becomes the default for a `gitlab` hub | Default for every hub, from `hub_info["forge"]`; worker-only appendices | 3 |
| `hub.json` `forge` and `hub.info` | Already written and returned; `up --forge` added; join carries it in M2 | 2 |
| Where `detect_forge` runs, with what `probe_cli` | Only at `up`, `probe_cli=True`, as now; everything else reads `hub.json` | 2 (unchanged) |

## Spec deltas

Each item is settled here and written into `docs/mvp-spec.md` by the step that implements it:

| Item | Settled as | Step |
|---|---|---|
| GitLab gate transport (§15) | `glab api`, no hub-held token; §15 row removed, §10 states it | 1 |
| Gate binding (§10, §12) | MR must be in the hub's origin project; other hosts or projects refused before any `glab` call | 1 |
| `check_merge_gate` forge dispatch (§8) | By `hub.json` forge: `gitlab` gives `GitLabGate`, otherwise GitHub | 1 |
| Pipelines (§10) | Every pipeline for the head SHA must pass; branch pipelines are the supported configuration | 1 |
| `--forge` and preflight content (§2, §10) | `up --forge`; checks table above; refuse only definite violations | 2 |
| Unsupported configurations (§10) | Adds merged-results pipelines and policy `rebase`; permanent while the merge invariant stands; `ff` and semi-linear supported | 2 |
| Minimum `glab` (§10) | 1.36.0, checked at `up` | 2 |
| Guide composition (§10) | Default by hub forge; appendices worker-only; missing appendix gives 404 | 3 |
| GitLab review comments (§10) | Notes REST API through `glab api`; top-level, non-resolvable | 3 |
| `forge` in workflow policy (§7.2) | Not a policy field; the hub's forge applies | 4 |
| Merge-method matrix (§10) | Table above, checked where the policy is known | 4 |

No DB schema version and no wire `hub.schema_version` change is expected in M5: no table
changes, `GateReport` keeps its shape, and `WorkflowPolicy` keeps its fields. A step that
turns out to need one stops and reserves the value on roadmap #2 before editing (AGENTS.md).

## Steps

Each step is one issue and one PR, reviewable on its own, and leaves `main` usable. GitHub
runs keep working throughout. A GitLab run becomes possible after Step 4.

```
Step 1 gate selection + binding ──────────────┐
Step 2 up --forge + preflight ──▶ Step 4 run path (prepare-run, Alice) ──▶ Step 5 acceptance
Step 3 forge-neutral guides ──────────────────┘
```

Steps 1, 2, and 3 are independent and can run in parallel. Step 4 needs Step 2's matrix and
Step 3's guides. Step 5 needs all of them.

Agents testing against `gitlab-box.local` follow #69's rules. In particular, they create
their own branches and MRs with an issue-specific name, leave the #50 probe MRs (!2–!4)
alone, and never change the sandbox's settings, runner, or `main`'s CI file.

### Step 1 — Gate selection, origin binding, URL hardening

**Scope.**
- `create_app` chooses the gate from `hub_info["forge"]` (see
  [Gate selection](#gate-selection-and-pipelines)). `GitLabGate` takes the origin project
  (host and path, parsed once from `hub_info["origin"]`) and uses it for `--hostname` and
  for every API path.
- `MergeRequestRef.parse` is replaced by a strict parser that checks a URL against the bound
  project (see [Gate transport](#gate-transport-15-glab-api)). Refusals raise
  `MergeGateError` and make no `glab` call.
- Fixture tests for two pipelines on one SHA.
- §10, §8, and §15 deltas.

**Tests** (`tests/test_gitlab_gate.py`, `tests/test_app.py`, `tests/test_rpc.py`).
- A table of adversarial URLs, each refused with the injected runner never called: another
  host, a look-alike host (`gitlab-box.local.example`), userinfo, IPv6 literal, ports `0`,
  `99999`, and non-numeric, `http://`, query, fragment, `..` and `.` segments,
  percent-encoded `/`, a trailing `/diffs`, another project on the same host, and a prefix
  or suffix of the bound project path. Also accepted: an uppercase host and nested groups
  (`group/sub/project`) when the origin has them.
- Pipelines: a branch pipeline and an MR pipeline on one SHA, both green, give `pass`; with
  either one failed, `fail`; with a redundant one canceled, `cancelled`; a merged-results
  `head_pipeline` (SHA not the head) gives `no_checks`.
- Dispatch: `/rpc` `check_merge_gate` on an app with `hub_info` forge `gitlab` reaches
  `GitLabGate`; with `github`, `MergeGate`; with no `hub_info`, `MergeGate`. Two app
  instances built from the same `hub_info` (a restart) choose the same gate.

```sh
uv run --locked pytest tests/test_gitlab_gate.py tests/test_app.py tests/test_rpc.py
# live, read-only, in a clone of the sandbox (origin git@gitlab-box.local:RoboNater/robomate-glab-sandbox.git):
uv run --project <robomate checkout> robomate up &
SHA=$(glab api --hostname gitlab-box.local projects/RoboNater%2Frobomate-glab-sandbox/merge_requests/4 \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')
curl -s -H "Authorization: Bearer $(cat .robomate/token)" http://127.0.0.1:<port>/rpc -d \
  '{"jsonrpc":"2.0","id":1,"method":"check_merge_gate","params":{"pr_url":"https://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/4","expected_head_sha":"'"$SHA"'"}}'
#   -> "ci": "fail" (!4's real failed job); the same call with another project's MR URL -> error -32004
uv run --project <robomate checkout> robomate down
```

**Size:** small to medium.

### Step 2 — `robomate up` on GitLab: `--forge` and preflight

**Scope.**
- `robomate up --forge github|gitlab`, recorded in `hub.json`. A warning for `unknown`.
- When the forge is `gitlab`, `up` runs the [preflight checks](#preflight) after resolving
  the repository and before serving. It prints one line per check, refuses on a definite
  unsupported setting, and warns otherwise. `glab` calls go through an injectable runner, as
  in `GitLabGate`.
- `check_unsupported_project_settings` gains merged-results pipelines. A new pure
  `check_merge_compatibility(project, merge_method)` implements the matrix. Step 4 calls it.
- §2 and §10 deltas (preflight content, minimum `glab`, unsupported list).

**Tests** (`tests/test_cli.py`, `tests/test_gitlab_gate.py` or a new
`tests/test_forge_preflight.py`). Every cell of the matrix. Each unsupported setting refuses
`up` with its name. No `glab`, an auth failure, and an unreadable project each warn and
`up` still serves. `--forge` overrides detection and is written to `hub.json`. A GitHub
origin runs no `glab` at all (runner never called).

```sh
uv run --locked pytest tests/test_cli.py tests/test_forge_preflight.py tests/test_forge_detection.py
# live, read-only, in a clone of the sandbox:
uv run --project <robomate checkout> robomate up      # prints the preflight lines; all pass on the #69 baseline
uv run --project <robomate checkout> robomate status  # Forge: gitlab
uv run --project <robomate checkout> robomate down
```

**Size:** small to medium.

### Step 3 — Forge-neutral role guides, composed by default

**Scope.**
- Rewrite `guides/implementer.md`, `reviewer.md`, `rebase.md`, and `worker.md`
  forge-neutrally, and move their commands to `guides/forge/github.md` and
  `guides/forge/gitlab.md`. Keep `prompts/worker.md` and `skills/worker/SKILL.md` in step
  with `guides/worker.md`.
- Remove the merge sections from both appendices. Add GitLab's MR creation (checked against
  `glab mr create --help` on 1.36.0), head read-back (`glab api …/merge_requests/<iid>`,
  `.sha`), the reviewer note through the Notes API, and reading an issue
  (`glab issue view`).
- The guide route composes by `hub_info["forge"]` by default, keeps `?forge=` as an
  override, and returns 404 when the effective appendix is missing.
- `guides/README.md` describes the composition. §10 deltas.
- Live check: on a branch and MR created for this step in the sandbox, post one note with
  the appendix's command, read it back (`resolvable: false`), then close the MR and delete
  the branch.

**Tests** (`tests/test_guides.py`, `tests/test_runtime_content.py`,
`tests/test_accounting.py`).
- Restart and idempotency (#21): no `hub_info` gives the GitHub appendix; a `gitlab` hub
  gives the GitLab appendix; a missing appendix gives 404; two app instances serve identical
  bytes.
- No served worker guide, for either forge, contains `pr merge` or `mr merge`.
- `test_runtime_content.py`'s assertions on `gh` strings in guides move to the GitHub
  appendix. The skill's `gh pr merge` assertion is untouched.

```sh
uv run --locked pytest tests/test_guides.py tests/test_runtime_content.py tests/test_accounting.py
# with a hub from `robomate up` in a sandbox clone, and one in a GitHub clone:
curl -s -H "Authorization: Bearer $(cat .robomate/token)" http://127.0.0.1:<port>/guides/reviewer.md
#   -> forge-neutral reviewer guide + the hub's appendix; no merge command
```

**Size:** medium. Mostly prose, but every worker reads it.

### Step 4 — The run path before M2: `prepare-run.py`, Alice's prompt and skill

**Scope.**
- `scripts/prepare-run.py`: read the forge from `--hub-repo`'s `hub.json`. For `gitlab`,
  require `--repository` to be the full origin URL of that hub (SSH or HTTPS), parse nested
  project paths (`parse_github_slug` becomes a forge-aware parser; GitHub behaviour is
  unchanged), and replace the `gh` checks with: authentication (`glab api user`), developer
  access or more, the matrix against `--merge-method`, and CI presence for
  `--allow-no-ci auto`, using the gate's affirmative rules (`.gitlab-ci.yml` at the default
  branch head, `ci_config_path`, Auto DevOps). `--issue N` names an issue in the GitLab
  project.
- `prompts/alice.md`: "Forge comment identity account", plus a `Forge:` line with host and
  project. `tests/test_runtime_content.py` and `scripts/prepare-run.py` change their
  section markers to match, and `tests/fixtures/prepare-run-default/` is regenerated.
- `skills/alice-orchestrator/SKILL.md`: a GitLab section with
  `glab mr merge <iid> -R <host>/<project> --sha <approved head> --auto-merge=false --squash|--squash=false --remove-source-branch --yes`
  (squash per `merge_method`). After it, a read-back of the MR (`state == "merged"`, and the
  merge or squash commit SHA recorded separately). The section says that a `--sha` 409 and a
  "Pipelines must succeed" refusal are the same safe outcome as a `--match-head-commit`
  refusal. It also gives the `glab` equivalents of the skill's `gh issue view` and
  `gh pr view` reads. The GitHub text and its pinned `gh pr merge` block are unchanged.
- §7.2 and §10 deltas (policy `forge`, the matrix).
- Live verification of the matrix: on a scratch GitLab project that the operator provides
  for this step (not the sandbox, whose settings stay at the #69 baseline), merge one MR per
  accepted cell with the skill's exact command. Record `git log --graph` and the MR state
  in the PR.

**Tests** (`tests/test_prepare_run.py`, `tests/test_runtime_content.py`). A `gitlab`
`hub.json` with a fake `glab` covers each preflight outcome, the rendered prompt's `Forge:`
line, and that no `gh` command runs. GitHub fixtures are unchanged except the identity line.

```sh
uv run --locked pytest tests/test_prepare_run.py tests/test_runtime_content.py
uv run --locked python scripts/prepare-run.py --hub-repo /abs/sandbox-clone \
    --repository git@gitlab-box.local:RoboNater/robomate-glab-sandbox.git \
    --run-dir /abs/run --issue <sandbox issue> --account RoboNater
#   -> preflight lines for GitLab; alice.prompt.md names Forge: gitlab
```

**Size:** medium.

### Step 5 — M5 acceptance run on `gitlab-box.local`

**Operator prerequisites** (#69; #68 comments of 2026-09-30 and 2026-10-01; all done as of
2026-10-01):
- `runner01` online. Sandbox !6 merged, so `main` is green under CI (pipelines 5 and 6).
- Runner `pull_policy = ["always", "if-not-present"]`, Auto DevOps off, and "Pipelines must
  succeed" on. The settings baseline is otherwise unchanged: `merge_method=merge`, no
  automatic rebase, no merge trains, branch pipelines only. Any later runner or project
  setting change is recorded as a comment on #69 before the run.
- `glab` ≥ 1.36.0, authenticated for `gitlab-box.local`, for the operator and every agent
  (one shared account, as on GitHub).
- A seeded sandbox issue for the run, and a small unrelated change ready to land on `main`
  after approval.

**Scope.** Meet the §13 done-when.
- The operator runs `robomate up` in a clone of the sandbox and keeps its preflight output.
  Step 4's `prepare-run.py` prepares Alice, Bob, and Charlie, who take the seeded issue to a
  merged MR.
- Required shape: at least one `changes_requested` round. After approval, the operator lands
  the unrelated change on `main` through its own MR, so the approved MR's base is stale. The
  gate reports `base_behind_main`, Alice assigns REBASE, and the merge binds to the rebased
  head, after re-review if the rebase reported conflict files.
- Evidence goes in `docs/evidence/m5-<run-id>.md`, modeled on M1's Step 7, using only the
  facts that follow:
  - The robomate commit, `glab version`, the GitLab version (`glab api version`), and the
    runner and project settings listed in #69, read at run time.
  - `hub.json`'s forge and the `up` preflight lines.
  - Every `check_merge_gate` report behind a merge decision: heads, `ci`, `mergeable`,
    `merge_state_status`, and `base_behind_main`.
  - The changes-requested round: finding IDs, note URLs, and `resolvable: false` on each
    reviewer note.
  - The stale base: `main` before and after, `diverged_commits_count`, and the REBASE task
    and its head.
  - The merge: the approved head equals the MR head accepted at merge; the merge commit SHA
    is recorded separately; the MR is `merged` and its source branch removed.
  - The pipeline ID and status for every head.
  - `hub-report` bytes by role.
  - Every attempt, including failed ones (poc-lessons). A CI failure caused by the
    environment (an image pull, the runner) is recorded as such.
- Afterwards, the run's branches and MRs are cleaned up per #69.

**Tests.** The run itself. The PR adds only the evidence document and any small fixes the run
exposed. Fixes of more than a few lines get their own issue.

## Risks

| Risk | Mitigation |
|---|---|
| A worker's MR URL steers `glab` to another host or project | Gate bound to the origin project; refused before any call (Step 1); `--hostname` from the origin |
| `glab` drift: `mr note` reshaped, `--auto-merge` defaulting to true | Notes through the REST API; explicit `--auto-merge=false` and squash flag; minimum version checked at `up` |
| The forge is unreachable when the hub restarts | `up` warns instead of refusing on failed checks; the gate fails closed per call |
| An image pull or registry outage fails CI, and the gate reports `fail` for an unrelated reason | Runner `pull_policy` falls back to the local image; evidence classifies such failures |
| One runner (`concurrent = 1`) against the gate's 60 s poll | Pipelines take about 8 s; a queued pipeline reads as `pending`, and Alice calls the gate again; avoid parallel pipelines during Step 5 |
| Auto DevOps turned back on hides "no CI" | `up` warns |
| The guide rewrite changes GitHub runs | Step 3 tests the served GitHub guides and reports bytes; commands are moved, not reworded |
| `prepare-run.py` work is discarded in M2 | It stays a thin caller of `agent_hub` functions that M2 reuses |
| The shared account cannot give native GitLab approvals | As on GitHub (PoC #37): the typed reviewer verdict is the approval; the project must not require native approvals |

## Roadmap bookkeeping

- #2's M5 section moves ahead of M2. Its "Milestone work" line links this plan (#68). One
  issue per step is opened when the step is picked up.
- #21: on merge of this plan, its first two decisions are ticked with a pointer here, and the
  third is answered in [#21 decisions](#21-decisions). Its review items close with Steps 1
  (URL parser, pipelines), 2 and 4 (matrix), and 3 (guide route and restart tests, notes).
  #21 closes with the last of them.
- **Reservations:** none expected (no DB schema bump, no wire `hub.schema_version` change).
  A step that finds otherwise reserves on #2 first.

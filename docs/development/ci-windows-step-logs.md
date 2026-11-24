# Windows CI step logs (#611)

How to prove, from a completed CI run, that the Windows job's fast
"Run changed test files" step actually ran the changed tests instead of
skipping them. The step's `conclusion` is `success` in both cases; the proof
is in the job log. For what the step is and when it runs, see the Validation
section of [`AGENTS.md`](../../AGENTS.md).

## 1. The job log is available only after the job completes

The changed-test step finishes within minutes, but the job log at
`GET /repos/<owner>/<repo>/actions/jobs/<job-id>/logs` answers `BlobNotFound`
(HTTP 404) until the whole `windows` job is done, about 15–20 minutes later.
Check the job's `status` and `conclusion` first and fetch the log only once
`status` is `completed` — while the job runs there is no log to read, whatever
`gh pr checks` shows for the whole run. The same extraction also works for the
`test` job, which runs its own copy of the step.

## 2. Fetch the log and strip ANSI codes

`gh api` refuses to print a body containing terminal escape sequences: without
a flag it writes `the response contains terminal escape sequences; pass
--allow-escape-sequences to output it anyway` into the output file and exits 0,
so the file holds an error message rather than the log. Pass
`--allow-escape-sequences` and strip the codes afterwards.

Write the log to a uniquely named file in your own temporary directory
(`mktemp -d`), never a fixed shared path — other agents may work on the same
machine. The endpoint redirects to Azure blob storage
(`productionresultssa<N>.blob.core.windows.net`, where `N` changes between
runs): a sandboxed agent must allow that host for the fetch, and a
`*.blob.core.windows.net` wildcard may be refused by the harness's auto-mode
classifier, so allow the specific host the redirect names for that run.

Use the head SHA that CI ran on — the pull request's head (`gh pr view
<number-or-url> --json headRefOid`), not the squash-merge commit — because the
changed-test step only exists on `pull_request` runs:

```sh
D=$(mktemp -d)
RUN=$(gh run list --repo RoboNater/robomate --commit <head-sha> --workflow ci.yml --json databaseId -q '.[0].databaseId')
JOB=$(gh api repos/RoboNater/robomate/actions/runs/$RUN/jobs -q '.jobs[] | select(.name=="windows") | .id')
gh api repos/RoboNater/robomate/actions/jobs/$JOB -q '.status + " " + (.conclusion // "-")'   # must be "completed ..."
gh api --allow-escape-sequences repos/RoboNater/robomate/actions/jobs/$JOB/logs > "$D/windows.log"
sed 's/\x1b\[[0-9;]*m//g' "$D/windows.log" \
  | awk '/##\[group\]Run changed_tests_file=/{hdr=1;next} hdr && /##\[endgroup\]/{hdr=0;on=1;next} on && /##\[group\]/{exit} on' \
  | grep -E ' =+ .*(passed|failed|skipped|error)|SKIPPED \[|FAILED |ERROR |No changed test files'
```

## 3. Anchor on the step's script, not its name

The log groups each step by the first line of its script, not by the step's
name, so the changed-test section starts at
`##[group]Run changed_tests_file="$RUNNER_TEMP/changed-tests"`, not
"Run changed test files". The group echoes the whole script — including
`echo "No changed test files to run."` — which is why a naive grep for that
phrase matches script text instead of output. The `awk` above prints only what
follows the `##[endgroup]` that closes that group, stopping at the next
`##[group]` (the full-suite step).

## 4. What counts as a pass

The step runs `uv run --locked pytest -n auto -rs` on the changed test files
([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)), so the
extracted lines are the pytest summary and, with `-rs`, skip-reason lines.
Pytest groups skips in that summary: one
`SKIPPED [N] <file>:<line>: <reason>` line per group of skips that share a
location and a reason, with `N` counting the group, and it reports a skip at
the line that raised it. Three consequences make the summary weaker than it
looks:

- `SKIPPED [2]` names a location and a reason but not which parametrized
  cases skipped, so a folded group can hide the material test — the one that
  needs native Windows to be meaningful — among its skips.
- A skip raised in a shared helper or fixture points at the helper's line no
  matter which test skipped through it: two runs can print the identical,
  fully accounted `SKIPPED [1] helper.py:<line>: <reason>` line with the
  material test skipping in one and passing in the other.
- Logs from before `-rs` (run 45109110210 reported `1709 passed, 31 skipped`
  with no reason lines) carry no skip details at all.

The step passed for real when:

- the summary shows the number of tests the changed files contain — count
  them locally with `uv run --locked pytest --collect-only -q <files>` — and
  `0 skipped`; or
- the step's output proves that no reported skip comes from a material test:
  the `SKIPPED [N]` lines account for the summary's whole skipped count (the
  bracketed counts add up to it), every line's reason is understood, and each
  skip's location identifies one specific non-material test — not a shared
  helper, fixture, or folded group, none of which can say which test skipped.

When the step's output leaves a material test's outcome uncertain — folded
groups, skips sited in shared helpers or fixtures (including a singleton
whose location names no test), or reason-free logs from before `-rs` — the
summary alone is not evidence. Get positive native Windows evidence before
approving: run the material test on native Windows with verbose per-test
outcomes, for example `uv run --locked pytest <files> -vv`, which prints
`PASSED` or `SKIPPED (reason)` for every node ID, and cite that run.

`No changed test files to run.` means the PR changed no `tests/test_*.py`
file, so the step ran nothing; changed helpers such as `tests/conftest.py` and
`tests/fake_harness.py` run only in the full suite. A summary naming `failed`
or `error` is a failure, not a pass.

## 5. PowerShell equivalent

The same steps for a Windows agent's shell; the state machine mirrors the
`awk` above:

```powershell
$D = New-Item -ItemType Directory (Join-Path ([IO.Path]::GetTempPath()) (New-Guid))
$sha = '<head-sha>'   # the PR head SHA CI ran on
$run = gh run list --repo RoboNater/robomate --commit $sha --workflow ci.yml --json databaseId -q '.[0].databaseId'
$job = gh api "repos/RoboNater/robomate/actions/runs/$run/jobs" -q '.jobs[] | select(.name=="windows") | .id'
gh api "repos/RoboNater/robomate/actions/jobs/$job" -q '.status + " " + (.conclusion // "-")'   # must be "completed ..."
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$esc = [char]27
gh api --allow-escape-sequences "repos/RoboNater/robomate/actions/jobs/$job/logs" > "$D/windows.log"
$clean = (Get-Content "$D/windows.log" -Raw) -replace "$esc\[[0-9;]*m", ''
$inScript = $false; $collect = $false
foreach ($line in ($clean -split "`r?`n")) {
  if (-not $inScript -and $line -match '##\[group\]Run changed_tests_file=') { $inScript = $true; continue }
  if ($inScript -and -not $collect) {
    if ($line -match '##\[endgroup\]') { $collect = $true }
    continue
  }
  if ($collect) {
    if ($line -match '##\[group\]') { break }
    if ($line -match ' =+ .*(passed|failed|skipped|error)|SKIPPED \[|FAILED |ERROR |No changed test files') { $line }
  }
}
```

# GitLab repository mirrors for robomate #100

One-time import performed on 2026-10-03 for
[RoboNater/robomate#100](https://github.com/RoboNater/robomate/issues/100), after
reading the instance and sandbox rules in
[#69](https://github.com/RoboNater/robomate/issues/69).
Only the three newly created projects below were changed. The existing sandbox,
scratch project, instance settings, and runner were left alone.

## Imported snapshot

| GitLab project | ID | Visibility | Source branches | Archived PR refs | Reachable source commits | Open issues |
|---|---:|---|---:|---:|---:|---:|
| [repo-archive-tool](https://gitlab-box.local/RoboNater/repo-archive-tool) | 4 | public | 9 | 13 | 57 | 5 |
| [print-my-calendar](https://gitlab-box.local/RoboNater/print-my-calendar) | 5 | public | 7 | 8 | 55 | 13 |
| [w520-engineering-handbooks](https://gitlab-box.local/RoboNater/w520-engineering-handbooks) | 6 | private | 1 | 0 | 3 | 0 |

The corresponding GitHub sources are
[repo-archive-tool](https://github.com/RoboNater/repo-archive-tool),
[print-my-calendar](https://github.com/RoboNater/print-my-calendar), and
[w520-engineering-handbooks](https://github.com/RoboNater/w520-engineering-handbooks).
Visibility matches the sources, including the private handbooks repository.
All three default branches remain `main`.

Each source was cloned with `git clone --mirror`. All advertised source branch
refs were pushed under their original names and with identical commit SHAs.
There were no tags. GitHub's advertised `refs/pull/<number>/head` refs were also
preserved as `refs/heads/github-pull/<number>/head`, retaining commits that would
otherwise disappear when only ordinary branches are pushed. This is a snapshot,
not a scheduled synchronization service. Unadvertised/deleted GitHub history
cannot be fetched and is outside this import.

Imported `main` SHAs:

- repo-archive-tool: `833ca9006c39de93ceea0068f02d00e0f61aec27`
- print-my-calendar: `7e6ef91b43153b255ffae6e674c650a4ce44e091`
- w520-engineering-handbooks: `e73d1e0e555852280fbb460a00e75d582a1fa2ba`

All open GitHub issues were recreated as open GitLab issues with their original
titles and bodies, prefixed with the source URL and original author. Both
existing comments were copied as notes with author, original timestamp, and
source-comment link. GitLab issue numbers and native creation timestamps differ;
GitLab trims trailing newlines. Original links remain valid GitHub links, and
bare `#N` references in copied Markdown retain their original text; consult the
source link for their original context.

Issue mappings (GitHub number → GitLab IID):

- repo-archive-tool: `10→1, 11→2, 12→3, 17→4, 21→5`.
- print-my-calendar: `7→1, 8→2, 10→3, 11→4, 17→5, 18→6, 19→7, 20→8,
  21→9, 22→10, 24→11, 27→12, 32→13`.
- w520-engineering-handbooks: no open issues.

[inventory.json](inventory.json) records every source ref and full SHA, issue and
comment mappings, project IDs, visibility, CI pins, and main pipeline URLs.
Closed issues, PR discussion, release assets, and GitHub project settings were
not part of the requested minimum import.

## CI adaptation

Two additional branches, both named `gitlab/issue-100-ci`, hold the GitLab CI
files. No new commits were made directly to an imported `main` branch. To enable
CI on source-identical branches, each project's `ci_config_path` points to its
own configuration file at the full CI commit SHA, using GitLab's
[custom CI configuration path](https://docs.gitlab.com/ci/pipelines/settings/#specify-a-custom-cicd-configuration-file).
The copies in this directory are verified byte-for-byte against GitLab.

| Project | CI commit | Main pipeline |
|---|---|---|
| repo-archive-tool | `38bf87639fe5a0cc0633aa3afec206a917526251` | [35](https://gitlab-box.local/RoboNater/repo-archive-tool/-/pipelines/35) |
| print-my-calendar | `0f1c37a30c7b03ef55c32fb7acdbb41ea4525123` | [39](https://gitlab-box.local/RoboNater/print-my-calendar/-/pipelines/39) |

The only project settings set for CI were disabling Auto DevOps on creation and
setting these two custom configuration paths. Both files passed
`POST projects/<id>/ci/lint` with `valid: true`.

[repo-archive-tool.gitlab-ci.yml](repo-archive-tool.gitlab-ci.yml) retains uv
0.6.2, locked dependencies, the Python 3.11/3.12/3.13 matrix, formatting, lint,
pytest, and CLI help. Git and Git LFS are installed in the container so the
integration tests exercise their real dependencies. The available instance
runner is Linux with a Docker executor, so the GitHub Windows/macOS matrix axes
cannot run here. The three Linux jobs pass on the unchanged imported main: jobs 38/39/40 each
passed all 156 tests, plus formatting, lint, and CLI help.

[print-my-calendar.gitlab-ci.yml](print-my-calendar.gitlab-ci.yml) uses the
source's .NET SDK 8.0.424, locked restore, full-solution formatting, cross-build
with `EnableWindowsTargeting=true`, the portable Core and YahooCalDav test
projects with coverage, and the dependency vulnerability report. Test artifacts
are retained for seven days. WPF App/Printing tests and installer/sample execution
require Windows; cross-building does not validate those runtime behaviors.

The first calendar pipelines ([36](https://gitlab-box.local/RoboNater/print-my-calendar/-/pipelines/36)
and [37](https://gitlab-box.local/RoboNater/print-my-calendar/-/pipelines/37))
exposed a pre-existing Windows assumption in
`ArchitectureSmokeTests.ProductionProjectDependenciesMatchAllowedGraph`: it
passes backslash-containing project references to `Path.GetFullPath` before
normalizing separators, leaving `..` unresolved on Linux. Only this test is
excluded from the Linux Core run; the Windows job still runs it. No source test
or production code was changed to make the adaptation pass. These failed
pipelines remain available as evidence of the limitation. Pipeline
[38](https://gitlab-box.local/RoboNater/print-my-calendar/-/pipelines/38),
created by pushing the CI branch before repinning the project setting, also
used the previous configuration and failed on the same test. The final main
pipeline 39 uses the corrected pin and succeeds: 29 Core tests passed and
20 YahooCalDav tests passed. The source
`RealYahooIntegrationTests.DiscoveryAndReadOnlyQueryAgainstOptInTestAccount`
test remains skipped because it requires an opt-in live Yahoo test account.

The complete Windows workflow is translated into a job tagged `windows`,
including tool verification, Inno Setup discovery, all tests, installer build,
print samples, and seven-day validation/self-contained artifacts. This job is
excluded by default because no Windows runner is available. An operator can
supply a Windows PowerShell 7 runner with Git, .NET SDK 8.0.424, Windows Desktop
runtime, and Chocolatey, then start a pipeline with `RUN_WINDOWS_CI=true`.
That runner also needs permission to install Inno Setup 6.7.1. Windows commands
and their artifacts remain unverified on this instance. No runner was added or
changed for this issue.

Two trigger/artifact differences from GitHub remain deliberate limitations of
this initial translation. Neither GitLab file has `workflow:rules`: every branch
push can start CI, including updates to archived `github-pull/*` branches. This
keeps future development branches directly usable for GitLab regression runs;
calendar's GitHub configuration is narrower (main pushes and PRs). The serial
instance runner may therefore queue extra work. Trigger optimization can be
done when those mirrors' development workflow is chosen.

The optional Windows job combines validation and self-contained outputs in one
GitLab artifact archive on every enabled run, rather than uploading the latter
only from main. GitLab artifact collection warns on missing paths and does not
implement GitHub's `if-no-files-found: error` behavior here. These upload semantics
have not been validated without a Windows runner; enabling Windows CI should
include checking its expected output directories and artifact contents.

Handbooks has no GitHub workflow on its imported main, so no CI was invented.
Auto DevOps is disabled and the GitLab pipeline list is empty.

The CI path is pinned deliberately. Editing the CI branch alone does not change
the active configuration: after review, update `ci_config_path` to the new full
SHA. This also lets future source changes be imported without overwriting the
GitLab-only CI files. The GitHub workflows remain unchanged in every mirror.

## Verification commands

Run from the robomate checkout with authenticated `gh`, `glab`, and Git SSH.
The verifier reads the snapshot inventory, freshly clones each GitLab repository
inside this checkout's Git directory, runs `git fsck --full`, checks every source
ref, walks their complete commit history, checks every copied issue/comment
against GitHub, checks project visibility/default branch/CI configuration, and
requires successful main pipelines. It also compares the currently advertised
GitHub refs, open issue set, and comment counts with the snapshot. Temporary
clones are removed on exit.
It exits nonzero on any failed comparison or pipeline still pending. Source
repository or issue edits after the snapshot can intentionally cause a
comparison failure. The successful run output is retained in
[verification.txt](verification.txt).

```sh
python3 docs/development/gitlab-mirrors-100/verify.py
uv run --locked mypy docs/development/gitlab-mirrors-100/verify.py
```

For direct forge evidence, these commands show projects, branches, open issues,
and pipelines. The `--hostname` must remain explicit. Inspect response bodies:
this glab version can exit zero even when the API body reports a 404.

```sh
set -euo pipefail
for name in repo-archive-tool print-my-calendar w520-engineering-handbooks; do
  glab api --hostname gitlab-box.local "projects/RoboNater%2F${name}"
  git ls-remote "git@gitlab-box.local:RoboNater/${name}.git" HEAD 'refs/heads/*' 'refs/tags/*'
  glab api --hostname gitlab-box.local --paginate "projects/RoboNater%2F${name}/issues?state=opened&per_page=100"
  glab api --hostname gitlab-box.local --paginate "projects/RoboNater%2F${name}/pipelines?per_page=100"
done
```

Specific main-pipeline evidence:

```sh
glab api --hostname gitlab-box.local projects/4/pipelines/35
glab api --hostname gitlab-box.local projects/4/pipelines/35/jobs
glab api --hostname gitlab-box.local projects/5/pipelines/39
glab api --hostname gitlab-box.local projects/5/pipelines/39/jobs
```

Robomate repository validation passed: locked all-package dependency sync,
Ruff, mypy (82 files), and pytest (1,028 tests on the final tree). `UV_CACHE_DIR` was set to a
workspace-local cache under `.git/issue-100/uv-cache` because the default cache
is outside the writable workspace. No robomate production code changed.
The snapshot verifier has explicit type annotations and passed the separate
mypy command above; the repository's normal mypy configuration covers only
`packages` and `tests`.

## Roadmap decision

No update to robomate roadmap #2 is needed. Issue #100 provisions additional
repositories for ongoing GitLab development; it does not complete the separate
human-operated M5 acceptance run #101 or change a milestone deliverable or
counter reservation. The DB schema reservation remains v12/next v13 and the wire
schema remains 1. This decision is also submitted as a PR comment for review.

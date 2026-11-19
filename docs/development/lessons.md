# Lessons ledger

This is the curated index of robomate's lessons learned. [#3][i3] is the inbox
and the source record: closeout reviews post lessons there as comments, with
evidence. This file says what became of each one, so the next run can find a
lesson and see whether its remedy has shipped.

Each entry is one rule. **Sources** link to the lesson's comment on #3,
labelled by the run's PR and the item number within that comment (`PR 560/1`).
The six lessons in #3's own body are labelled `#3 body/1`–`/6`. Sources list
separate incidents; a second write-up of the same incident is marked *same
incident*, and a related technique is marked *related*. **Recurred** gives
incidents after the first, and whether a remedy was already available to that
run.

Rank work by consequence, by recurrence after a remedy was available, and by
what prevention costs. Recurrence counts are a rough signal, not a score.

## Dispositions

- **applied**: the remedy shipped; the link names where.
- **tracked**: an open issue owns it.
- **planned**: accepted in triage but not filed yet; becomes *tracked* when
  it is.
- **reference**: no change planned. The #3 comment is the record; techniques
  marked *testing doc* move into a testing reference when one is written.
- **retired**: superseded by a later design.

## Updating this ledger

- **New lesson.** Post it to #3 in the usual layout first. Then add a row here,
  or a source to an existing row, in the next docs change that touches this
  file. Closeout review is the natural time to do both.
- **Recurrence.** On #3, post `Recurrence of <link to the entry's source>` with
  what happened, its consequence, and whether the remedy was available in that
  run, instead of writing the lesson again. Add the incident to the row's
  **Recurred** cell.
- **Disposition change.** When an issue is filed, a remedy merges, or a
  decision retires a lesson, update the row and its link.
- This file is a development doc, not a served guide: agents read it when
  triaging lessons or checking whether one is known, not on every operation.

## Verification and evidence

| Lesson | Sources | Recurred | Disposition |
|---|---|---|---|
| Read every full SHA in evidence back from the forge | [#3 body/1][i3] | [PR 463 trailer][c463] (remedy available: lesson only) | applied: `guides/implementer.md`, `guides/reviewer.md` require the full verified SHA |
| Run documented and acceptance commands live; tests that grep for fragments of a command, or call it in-process, are not enough | [#3 body/2][i3], [PR 259/1–2][c259] | — | applied: `guides/implementer.md`, `guides/reviewer.md`, AGENTS.md ("run every script and entry point") |
| Report a smoke run or check that didn't run as not done; quote its real output | [PR 259/1][c259] | [PR 294/4][c294] (remedy available: lesson only) | tracked: [#286][i286] (run-record drift); pilot proposed in [#286 comment][e286]: record tested commit, platform and outcome in the PR record |
| Re-run lint and format after the last edit; report only the final state | [PR 570][c570] | — | tracked: [#286 pilot][e286] |
| Read a test's assertions against the spec, not only whether it passes | [PR 259/3][c259] | — | reference |
| Check that a fix comes from what the PR claims; don't state an unproven root cause; explain a production change made for a test | [PR 379/5–7][c379] | — | applied in part: AGENTS.md ("If production code had to change…"); rest reference |
| Reproduce a failure before accepting its stated cause; statement-of-work advice can be wrong | [PR 86/1][c86], [PR 89/1][c89] | — | reference (AGENTS.md's timing-flake rule covers the flake case) |
| Recounting an audit doesn't verify it; read the call sites. A literal search misses calls that pass the value in a variable | [PR 406/1–2][c406] | — | reference |
| Check closeout reports against telemetry and the forge; compare exact values before reporting a consistency bug | [PR 432/1–3][c432], [PR 474/3][c474] | [PR 449/4][c449] *related* | tracked: [#286][i286]; operator's closeout-review procedure |
| Compare the issue's Tests section with the test diff | [PR 432/5][c432] | — | reference |
| Exit 0 is not evidence that a tool call worked | [PR 521/5][c521] | — | reference |
| Dependabot closes alerts about a minute after the merge; wait and recheck | [PR 539][c539] | — | reference |
| Reading retained evidence beats reading the summary | [PR 390/5][c390] | — | reference |

## Tests that can fail

| Lesson | Sources | Recurred | Disposition |
|---|---|---|---|
| Show a regression test fails without the fix, in a checkout like CI's | [PR 266][c266] item 3, [PR 306/2][c306] | [PR 379/1][c379], [PR 466/1][c466] (no remedy until #467); [PR 457/2][c457] *related* | applied: [#467][i467] / PR #484 (`guides/reviewer.md`, `guides/implementer.md`) |
| A condition wait that is already true makes the test vacuous; run a test with its feature off | [PR 379/2][c379], [PR 432/7][c432] | — | applied: [#381][i381] (`testing-time-and-waits.md`) |
| Give each negative case input that breaks only the rule under test; with overlapping safeguards, neutralize the fallback to isolate the layer a test claims to cover | [PR 494/2][c494], [PR 521/4][c521] | — (both found by the #467 check working in review) | applied: [#588][i588] (`guides/reviewer.md` step 4; repair checklist in `testing-time-and-waits.md`) |
| Prove a nested pytest inherited the warning policy by injecting a warning | [PR 466/4][c466] | — | reference (testing doc: [#589][i589]) |
| A test message that avoids the old wording proves type-based detection | [PR 274][c274] item 2 | — | reference |

## Search for copies and consumers

| Lesson | Sources | Recurred | Disposition |
|---|---|---|---|
| When a shared rule, instruction, name, field, query or validator changes, search for its copies and trace its callers and readers of persisted data; report out-of-scope instances separately | [#3 body/5][i3], [PR 306/1][c306] | [PR 410/1][c410], [PR 449/2][c449], [PR 560/2][c560] (lesson only); [PR 283/1][c283], [PR 284/4][c284], [PR 494/1][c494], [PR 474/1][c474] *related* | applied: [#588][i588] (AGENTS.md "Changing things"; a reviewer groups instances of one defect into one finding) |
| A new assignment field must be tested all the way to the worker | [PR 348/1][c348] | — | applied: [#351][i351] |
| Check a roadmap decision against every place #2 names the issue | [PR 405][c405] item 2 | [PR 406][c406] (same day; lesson not yet posted) | retired: [#408][i408] removed roadmap edits from ordinary runs |

## Windows and cross-platform

| Lesson | Sources | Recurred | Disposition |
|---|---|---|---|
| Generated and byte-compared files need `eol=lf`, and checks should normalize line endings on every input | [PR 584][c584] | earlier: `guides/**`, two [#4][i4] items; [PR 86/1][c86], [PR 86/3][c86], [PR 89/7][c89] *related* | tracked: [#587][i587] (`sync-worker-prompt.py` normalizes both inputs; CI runs `--check` on Linux and Windows); [#590][i590] (evaluate a repo-wide LF default) |
| On Windows, a venv `python.exe` starts the real interpreter as a child, so the recorded PID isn't `Popen.pid` | [PR 86/2][c86] (second, after #40 / PR #57) | [PR 476/1][c476] (remedy available: lesson only) | reference (testing doc: [#589][i589]) |
| `os.kill(pid, 0)` sends Ctrl-C on Windows; use `process_alive` | [PR 472/3][c472] | — | applied: native probe in `agent_hub_common.registry` and `tests/fake_harness.py`; launcher tests assert the Windows path never sends signal 0 |
| Isolate both user config directories in tests; run platform-branched paths on both platforms | [PR 284/1][c284] | — | applied: conftest `isolated_machine_state` |
| Name the platform a local test count comes from; local counts can't speak for skipped platform cases | [PR 284/1][c284] | [PR 344/6][c344] (lesson only) | tracked: [#286 pilot][e286] records platform |
| Approve Windows-only code only after Windows CI or native evidence on that head | [PR 476/5][c476] | — | applied: [#588][i588] (`guides/reviewer.md` approval decision, scoped to material native-Windows paths) |
| Text printed from served guides needs console-encoding coverage (cp1252) | [#3 body/6][i3] | — | reference (testing doc candidate) |
| `mypy --platform` reproduces a platform-only failure | [PR 86/6][c86] | [PR 89/3][c89] *same day, related* | reference (testing doc candidate) |
| Don't monkeypatch `os.name` on Windows Python 3.12 | [PR 86/4][c86] | — | reference (testing doc candidate) |
| Git for Windows `bin/bash.exe` is a forwarding launcher; log argv at every hop; PowerShell treats typographic quotes as quotes | [PR 344/1–2, 4][c344] | — | reference |
| Python stand-ins don't certify a real npm `.cmd` shim | [PR 463/5][c463] | — | tracked: [#346][i346] |
| PowerShell pipelines garble UTF-8 issue bodies | [PR 283/3][c283] | — | applied: [#255][i255], `editing-shared-bodies.md` |
| Codex on Windows starts outside the working directory; calling WSL from Git Bash | [PR 86/7][c86], [PR 89/8][c89] | — | reference |

## Shell and forge CLI

| Lesson | Sources | Recurred | Disposition |
|---|---|---|---|
| Edit a shared issue or PR body so that a failure writes nothing | [PR 78/1][c78] | — | applied: AGENTS.md, PR #82, `editing-shared-bodies.md` |
| `set -e` doesn't stop a sequence in Claude Code's Bash tool | [PR 236/2][c236] | — | applied: [#255][i255] |
| `gh -q .body` appends a newline | [PR 236/7][c236] | — | applied: `editing-shared-bodies.md` |
| Pass Markdown bodies to `gh` from a file | [PR 294/5][c294] | [PR 560/3][c560] (lesson only) | tracked: [#563][i563] |
| A shared `/tmp` collides across runs | [PR 78/2][c78] | — | applied: [#81][i81] |
| `gh pr view --json headRefOid` lags a push | [PR 78/4][c78] | — | applied: PR #82, forge appendices |
| `addopts = "-q"` hid pytest counts | [PR 78/5][c78] | — | applied: PR #82 |
| `gh pr merge --delete-branch` can delete another worktree | [PR 449/1][c449] | — | applied: PR #449 (no `--delete-branch`) |
| Ancestry doesn't prove a squash merge | [PR 449/3][c449] | — | applied: `robomate clean`, PR #449 |
| `--admin` can't merge past a failing required check; re-run the job | [PR 550/3][c550] | — | reference |
| Script files for multi-hunk edits; `pgrep -f` matches its own loop; `cd dir && cmd &` backgrounds the `cd`; `git stash` with a path (unconfirmed) | [PR 86/5][c86], [PR 89/9][c89], [PR 236/3][c236], [PR 306/5][c306] | — | reference |

## Main moved, rebase and merge

| Lesson | Sources | Recurred | Disposition |
|---|---|---|---|
| Run counter-sensitive or schema-sensitive tests on a scratch merge with `main`; a clean textual merge can still be a semantic conflict | [PR 294/1][c294], [PR 494/1][c494] | — | reference (scratch-merge technique) |
| Recheck `main` immediately before approval | [PR 237/1][c237] | — | reference (the roadmap part is retired) |
| `git show --remerge-diff` shows whether a merge has hand edits | [PR 494/3][c494] | — | reference |
| `base_behind_main` triggers REBASE even when GitHub says clean; a clean rebase merges without re-review by design | [PR 459/4][c459], [PR 474/2][c474] | — | reference |
| Plan for a REBASE and a CI re-wait before merge | [PR 283/5][c283] | [PR 348/6][c348] | reference |

## CI failures and flakes

| Lesson | Sources | Recurred | Disposition |
|---|---|---|---|
| Read the failed step and compare its signature with the change before assigning a repair; an identifiable infrastructure failure justifies a re-run; a passing same-head re-run alone leaves the cause unresolved | [PR 344/3][c344], [PR 472/1][c472] | [PR 344/7][c344], [PR 410/2][c410] (infrastructure); [PR 348/4][c348], [PR 466/2][c466] *related* | applied: [#588][i588] (`guides/orchestrator.md` `ci == fail` route) |
| Transient license-download failures | [PR 344/7][c344] | [PR 410/2][c410] | applied: [#417][i417] (retry) |

## Requirements and scope

| Lesson | Sources | Recurred | Disposition |
|---|---|---|---|
| Requirements live in issue comments too; retrieve the discussion and carry its acceptance items into the review checklist | [PR 432/6][c432] | [PR 457/1][c457] | applied: [#588][i588] (forge appendices read the body and full discussion; `guides/reviewer.md` step 3 carries comment acceptance items) |
| Agreed deferrals and operator-only work need a live tracking URL before `Closes #N` closes the source | [PR 457/1][c457], [PR 459/3][c459] | — | applied: [#588][i588] (`guides/reviewer.md` step 5; FOLLOW-UP route in `guides/orchestrator.md`, operator-only work goes to an `operator-run` issue) |
| An unmet explicit acceptance item is blocking; pass the statement of work verbatim | [PR 294/2–3][c294] | — | applied: [#296][i296], [#297][i297] |
| A plan must not name an unreserved shared counter; reserve before use | [PR 236/1][c236], [PR 259/5][c259] | — | applied: [#409][i409], AGENTS.md |
| A new mandatory step must say when it applies; define every term an agent must act on | [PR 484/2–3][c484] | — | reference |
| Don't name `get_role_guide` as the pre-merge check for a guide change; use `guide_response` from the PR checkout | [PR 484/1][c484] | — | reference |

## Orchestration and agents

| Lesson | Sources | Recurred | Disposition |
|---|---|---|---|
| Fetch the role guide on every assignment, including closeout | [PR 560/1][c560] | — (the worker guide already required it) | tracked: [#564][i564] (actionable error); proposal in [#564 comment][e564]: trial delivering the guide with the assignment if skips keep costing work |
| Resume a crashed Alice with `takeover=true`, then reconcile | [#3 body/4][i3] | — | applied: `guides/orchestrator.md`, recovery docs |
| Name the event that sources CLOSE-OUT | [PR 274][c274] item 3 | — | applied: [#275][i275] |
| A small implementer model stalled on design-heavy work; repeated unresolved questions mean Alice should inspect progress | [PR 521/1][c521] | — | reference (#434 covers the mistyped-ID spin) |
| Recovery by accident isn't recovery; check a stuck agent's calls, not its latest output | [PR 432/3–4][c432] | — | applied in part: [#434][i434] |
| Nonblocking findings are worth a fix cycle | [PR 560/4][c560] | — | applied: `guides/reviewer.md`, operator decision |
| Escalate a finding whose fixes keep missing | [PR 344/5][c344] | — | reference (the orchestrator guide escalates repeated disagreement) |
| Cite the operator question id wherever authority is claimed | [PR 390/3][c390] | — | reference |
| `Closes #N` on an evidence-only PR closes the issue early | [PR 390/4][c390] | — | tracked: [#389][i389] |
| Check typed result payloads before submitting | [PR 476/6][c476] | — | applied: [#478][i478] |
| An agent's own memory doesn't carry to the next run | [PR 78/3][c78] | — | reference |
| Check when a shared-body edit happens, not only what it says | [#3 body/3][i3] | — | reference |
| Plan bookkeeping needs an owner; coordinate parallel planning PRs | [PR 78/7][c78], [PR 237/2][c237] | — | reference |
| Pairing waits, single-worker starts, idle workers, wall-time budgets | [PR 86/8][c86], [PR 89/6][c89], [PR 89/10][c89], [PR 379/8][c379] | — | reference |
| Grant filesystem access only from run-assigned metadata | [PR 521/2][c521] | — | reference |

## Test techniques

Candidates for a short testing reference. Clock and wait techniques belong in
[`testing-time-and-waits.md`](testing-time-and-waits.md).

| Lesson | Sources | Disposition |
|---|---|---|
| Test the actual child environment and harness profile, not only argv; resume through the real guards | [PR 463/1–3][c463] | reference (testing doc: [#589][i589]) |
| Nested pytest: fresh session, real config via `-c`, own `--basetemp`, `LIVENESS_S` bound | [PR 466/3–4][c466], [PR 459/5][c459] | reference (testing doc: [#589][i589]) |
| A behaviour that reads the working directory makes results depend on where the suite runs | [PR 459/1][c459] | applied: [#461][i461] |
| A new launcher preflight needs a stub in every subprocess test | [PR 550/2][c550] | reference (testing doc: [#589][i589]) |
| A check of a harness's config must resolve its relocation variable; a Codex TOML key isn't a server entry | [PR 535][c535] | reference (testing doc: [#589][i589]) |
| Ask the real OpenCode CLI for effective permissions | [PR 521/3][c521] | reference |
| Drive transport code once against a real misbehaving local server | [PR 419][c419] | reference (testing doc candidate) |
| Carry a total deadline through every transport phase; a concurrency cap can starve healthy peers | [PR 266][c266] items 1–2 | reference |
| Set `dispatcher.operator_token` after the app's lifespan starts | [PR 345][c345] | reference (testing doc candidate) |
| Hold caps: `max_wait_s` and `TEST_WAIT_S` in test hubs; run the probe at 100 ms | [PR 379/3–4][c379] | applied in part: `probe-100` CI job |
| Give each future-dated record its own "now"; pin a bad float base to reproduce a rounding flake | [PR 306/4][c306], [PR 405][c405] item 1 | reference (timing doc: [#589][i589]) |
| Capture the real `monotonic` at import; a one-shot watchdog doesn't bound a resume loop | [PR 472/2, 4][c472], [PR 476/3][c476] *same incident* | applied: [#475][i475]; reference (timing doc: [#589][i589]) |
| A fake harness must not inherit production retry budgets | [PR 463/4][c463] | applied: [#464][i464] |
| Normalize Markdown whitespace in content tests | [PR 410/4][c410] | reference |
| A SQLite CHECK constraint is permanent; rowid order must survive table rebuilds; generated names pass the full validator; fail closed | [PR 259/4][c259], [PR 306/3][c306], [PR 348/2–3][c348] | reference |
| Investigations: own output directory per attempt; check the environment before a long run; run nested-sandbox probes on the host; detached recipe for long test files | [PR 390/1–2][c390], [PR 550/1][c550], [PR 284/5][c284], [PR 410/5][c410] | reference |
| Don't edit a checkout while a detached test run uses it | [PR 274][c274] item 1 | reference |

[i3]: https://github.com/RoboNater/robomate/issues/3
[i4]: https://github.com/RoboNater/robomate/issues/4
[i81]: https://github.com/RoboNater/robomate/issues/81
[i255]: https://github.com/RoboNater/robomate/issues/255
[i275]: https://github.com/RoboNater/robomate/issues/275
[i286]: https://github.com/RoboNater/robomate/issues/286
[i296]: https://github.com/RoboNater/robomate/issues/296
[i297]: https://github.com/RoboNater/robomate/issues/297
[i346]: https://github.com/RoboNater/robomate/issues/346
[i351]: https://github.com/RoboNater/robomate/issues/351
[i381]: https://github.com/RoboNater/robomate/issues/381
[i389]: https://github.com/RoboNater/robomate/issues/389
[i408]: https://github.com/RoboNater/robomate/issues/408
[i409]: https://github.com/RoboNater/robomate/issues/409
[i417]: https://github.com/RoboNater/robomate/issues/417
[i434]: https://github.com/RoboNater/robomate/issues/434
[i461]: https://github.com/RoboNater/robomate/issues/461
[i464]: https://github.com/RoboNater/robomate/issues/464
[i467]: https://github.com/RoboNater/robomate/issues/467
[i475]: https://github.com/RoboNater/robomate/issues/475
[i478]: https://github.com/RoboNater/robomate/issues/478
[i563]: https://github.com/RoboNater/robomate/issues/563
[i564]: https://github.com/RoboNater/robomate/issues/564
[i587]: https://github.com/RoboNater/robomate/issues/587
[i588]: https://github.com/RoboNater/robomate/issues/588
[i589]: https://github.com/RoboNater/robomate/issues/589
[i590]: https://github.com/RoboNater/robomate/issues/590
[e286]: https://github.com/RoboNater/robomate/issues/286#issuecomment-7012046991
[e564]: https://github.com/RoboNater/robomate/issues/564#issuecomment-7012047562
[c78]: https://github.com/RoboNater/robomate/issues/3#issuecomment-5944704905
[c86]: https://github.com/RoboNater/robomate/issues/3#issuecomment-5953239222
[c89]: https://github.com/RoboNater/robomate/issues/3#issuecomment-5957589613
[c237]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6215958781
[c236]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6219291947
[c259]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6232967261
[c266]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6240941833
[c274]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6244924962
[c283]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6252351236
[c284]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6252409775
[c294]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6271476494
[c306]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6289720171
[c344]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6377536973
[c345]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6377699100
[c348]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6383064684
[c379]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6404615353
[c390]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6478649985
[c405]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6528493291
[c406]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6534528761
[c410]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6561461391
[c419]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6562685905
[c432]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6588700041
[c449]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6724738999
[c457]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6742858910
[c459]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6747467167
[c463]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6757201027
[c466]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6769251516
[c472]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6784193491
[c476]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6785542569
[c474]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6795005746
[c484]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6823649701
[c494]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6853390713
[c521]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6882599341
[c535]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6901235492
[c539]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6902374049
[c550]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6906662167
[c560]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6947022875
[c570]: https://github.com/RoboNater/robomate/issues/3#issuecomment-6979018263
[c584]: https://github.com/RoboNater/robomate/issues/3#issuecomment-7007607349

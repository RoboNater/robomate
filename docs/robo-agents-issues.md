# robo-agents issues at the move to robomate

Open issues in RoboNater/robo-agents when development moved to RoboNater/robomate (2026-09-25),
seeded from `RoboNater/robo-agents@941870b`. Dispositions follow
[mvp-spec Appendix A](https://github.com/RoboNater/robomate/blob/main/docs/mvp-spec.md). robo-agents was archived as-is; none of these
issues were closed or commented on.

## Carried into robomate

| robo-agents | Title | robomate | Milestone | Note |
|---|---|---|---|---|
| RoboNater/robo-agents#148 | Investigate intermittent async test failures under full-suite load | RoboNater/robomate#6 | M1 Standalone hub + CLI |  |
| RoboNater/robo-agents#119 | prepare-run: enable HUB_CALL_ACCOUNTING so hub-report has Alice and hub-side wire bytes | RoboNater/robomate#7 | M1 Standalone hub + CLI |  |
| RoboNater/robo-agents#113 | Short kickoff prompts: identity plus one tool call; the hub serves the rest | RoboNater/robomate#8 | M2 Join, worktrees, work intake | Core M2 work. |
| RoboNater/robo-agents#143 | Hand workers the statement of work verbatim: hub serves the operator's work file by sha256; Alice references scope instead of restating it | RoboNater/robomate#9 | M2 Join, worktrees, work intake | Core M2 work. |
| RoboNater/robo-agents#106 | prepare-run: reject a --work-file too large for the 16 KiB message-part cap | RoboNater/robomate#10 | M2 Join, worktrees, work intake | Becomes the size cap on robomate submit. |
| RoboNater/robo-agents#84 | Windows-specific: Codex CLI 0.155.1 withholds hub MCP tools when an extra --add-dir is passed | RoboNater/robomate#11 | M3 Harness breadth | Re-test under worktrees; the main .git grant is likely required. |
| RoboNater/robo-agents#112 | Regression test: agent-facing wait defaults stay at or below 100 s | RoboNater/robomate#12 | M3 Harness breadth | Reshape: holds come from harness profiles; test each against its harness limit. |
| RoboNater/robo-agents#61 | mock-alice --endurance: a leftover agent row from a failed run is taken as this run's check-in | RoboNater/robomate#13 | M3 Harness breadth | Fold into robomate certify: fail fast on leftover state. |
| RoboNater/robo-agents#114 | Alice set a run to done with its PR unmerged; merge by default, with an explicit no-merge scope | RoboNater/robomate#14 | M4 Close-out and resilience | Addressed by the done check (mvp-spec §7.3). |
| RoboNater/robo-agents#91 | Close-out drops unaddressed non-blocking findings: no state keeps the promise the NON-BLOCKING prompt makes | RoboNater/robomate#15 | M4 Close-out and resilience | Addressed by the close-out task (mvp-spec §7.3). |
| RoboNater/robo-agents#117 | Let Alice adjudicate roadmap obligations; prepare-run must not hard-code “no roadmap edit” | RoboNater/robomate#16 | M4 Close-out and resilience | Addressed by closeout_steps (mvp-spec §7.3). |
| RoboNater/robo-agents#107 | Decide whether hub policy should enforce one PR per run and a stable work label | RoboNater/robomate#17 | M4 Close-out and resilience | Decide in M4; the done check already binds a run to one PR/MR. |
| RoboNater/robo-agents#31 | Lightweight resume reconciliation | RoboNater/robomate#18 | M4 Close-out and resilience | Reduced scope per mvp-spec §7.5. |
| RoboNater/robo-agents#131 | Hub liveness is not robust to host standby/resume: every worker is declared lost on wake | RoboNater/robomate#19 | M4 Close-out and resilience |  |
| RoboNater/robo-agents#121 | hub-report --timeline: interleaved message/event/decision history without payload text | RoboNater/robomate#20 | M4 Close-out and resilience | Deliver as robomate log --timeline. |
| RoboNater/robo-agents#124 | Plan GitLab support implementation (follow-up to #122 / #123) | RoboNater/robomate#21 | M5 GitLab | Update to mvp-spec §10: glab api gate, forge appendix guides. |
| RoboNater/robo-agents#92 | Reduce hub MCP context cost: trim the payloads that dominate Alice's and the workers' context | RoboNater/robomate#22 | M6 Token budget + MVP acceptance |  |
| RoboNater/robo-agents#94 | call_log: bounded retention and measured write overhead when accounting is on | RoboNater/robomate#23 | M6 Token budget + MVP acceptance |  |

## Replaced

| robo-agents | Title | Now |
|---|---|---|
| RoboNater/robo-agents#2 | Roadmap | Replaced by the robomate roadmap, RoboNater/robomate#2. |
| RoboNater/robo-agents#57 | Lessons learned and post-PoC hardening from issue and PR reviews | Exported to docs/poc-lessons.md in RoboNater/robomate; new lessons go to RoboNater/robomate#3. |
| RoboNater/robo-agents#110 | Minor nits (low priority) | Replaced by the robomate minor-nits issue, RoboNater/robomate#4. |

## Not carried

| robo-agents | Title | Reason |
|---|---|---|
| RoboNater/robo-agents#103 | prepare-run --no-harness: prepare a run without harness-specific setup, for operator-launched harnesses | Superseded by robomate up + join (mvp-spec M2). |
| RoboNater/robo-agents#105 | Share Alice goal rendering across prepare-run, step6 and mock-worker | Superseded by robomate submit (mvp-spec M2). |
| RoboNater/robo-agents#108 | Exercise a statement-of-work goal end to end, not only as skill text | Superseded by the M2 and M6 acceptance runs. |
| RoboNater/robo-agents#135 | prepare-run: let every worker be remote (repeatable --remote-worker) | Superseded by join + the clone fallback (mvp-spec §6). |
| RoboNater/robo-agents#82 | Update robo-agent skills: namespace & project-level installation | Superseded by robomate install (mvp-spec M3). |
| RoboNater/robo-agents#99 | Real Step 6 measurement run with call accounting enabled (deferred from #78) | Replaced by M1–M6 dogfooding with call accounting on. |
| RoboNater/robo-agents#87 | step6 verifier: extract_single_json_object docstring overstates what it rejects | PoC Step 6 verifier; archived with the PoC. |
| RoboNater/robo-agents#88 | step6 collect(): a non-string follow-up URL crashes collection instead of failing follow_ups_verified | PoC Step 6 verifier; archived with the PoC. |
| RoboNater/robo-agents#89 | step6: unreachable except and a thrice-repeated rationale guard after the JSON-extraction change | PoC Step 6 verifier; archived with the PoC. |
| RoboNater/robo-agents#90 | Step 6 Alice prompt: say the gate/follow-up JSON object must appear exactly once in the rationale | PoC Step 6 verifier; archived with the PoC. |
| RoboNater/robo-agents#17 | CancellableStdout.write JSON-parses every outbound MCP message to detect a once-per-session handshake | Likely moot once the hub no longer serves stdio; re-file in robomate if it recurs. |
| RoboNater/robo-agents#100 | Close-out does not track what a run owes: reservation updates barred by the goal, and acceptance items met by a substitute | Partly covered by robomate M4; the general ledger is listed under mvp-spec §16. |
| RoboNater/robo-agents#44 | Team lead / architect as a distinct role (post-PoC) | Post-MVP (mvp-spec §16); re-file in robomate when picked up. |
| RoboNater/robo-agents#96 | Call accounting: reconcile worker and hub byte totals (heartbeat bytes, retried attempts) | Post-MVP (mvp-spec §16); re-file in robomate when picked up. |
| RoboNater/robo-agents#102 | Non-PR deliverables: let a statement of work complete and be reviewed without a pull request | Post-MVP (mvp-spec §16); re-file in robomate when picked up. |
| RoboNater/robo-agents#115 | Log-analysis tools for run audits | Post-MVP (mvp-spec §16); re-file in robomate when picked up. |
| RoboNater/robo-agents#127 | Step 7 hardening: multi-machine networked E2E (physical LAN hosts, SSH launch/collect, link disruption) | Post-MVP (mvp-spec §16); re-file in robomate when picked up. |
| RoboNater/robo-agents#97 | X-Hub-Agent attribution on the guide route is a self-claim any token holder can spoof | Bridge sessions are bound to a join in robomate; re-file if the gap remains after M2. |

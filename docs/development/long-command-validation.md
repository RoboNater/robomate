# Long commands across shell-tool calls (#355)

The worker guide selects retained command sessions where the backend supports
them. A shell background job is a different lifetime mechanism: `nohup` changes
signal handling, and `setsid` creates a session, but neither guarantees that a
tool backend will retain descendants after returning. The [official OpenAI
Codex prompting guide](https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide#shell_command)
describes polling an existing `exec_command` session with `write_stdin`; it
does not establish detached-child survival for this runtime. Probe the actual
route before prescribing detachment. These probes start no agents or hubs.

## Two-call probe

Record the harness/version, OS, shell tool and sandbox mode with the results.
Use the harness temporary directory, outside the worktree. In one shell-tool
call, run this once with `method=nohup`, then once with `method=setsid` if
`setsid` is available:

```sh
method=nohup
D=$(mktemp -d) && echo "$D" &&
ps -o pid,ppid,pgid,sid -p $$ > "$D/parent.txt" &&
(
  if [ "$method" = setsid ]; then set -- setsid nohup; else set -- nohup; fi
  "$@" sh -c '
    ps -o pid,ppid,pgid,sid -p $$ > "$1/child.txt"
    printf "started\n" > "$1/started"
    sleep 2
    printf "finished\n" > "$1/out.log"
    printf "0\n" > "$1/rc"
  ' sh "$D" > "$D/launcher.log" 2>&1 </dev/null &
  for i in $(seq 1 20); do [ -f "$D/started" ] && break; sleep 0.05; done
  if [ -f "$D/started" ]; then cat "$D/started"; else echo 'FAILED: no startup marker'; fi
)
cat "$D/parent.txt"
```

Allow that call to return. In a **separate shell-tool call**, substitute the
printed directory:

```sh
D='<printed directory>'
sleep 3
ls -la "$D"
cat "$D/launcher.log"
if [ -f "$D/child.txt" ]; then cat "$D/child.txt"; fi
if [ -f "$D/rc" ]; then cat "$D/out.log" "$D/rc"; else echo 'INCOMPLETE: no completion rc'; fi
```

This is a bounded observation after a delay, not proof of a particular cleanup
signal. Record missing startup separately from startup followed by missing
completion. The launch tool's rc 0 means only that launch returned successfully.
Absence of completion does not establish kill versus suspension or another
interruption. New SID/PGID does not prove escape from a per-call sandbox.

For a retained-session control, run a foreground `sh -c` that writes `started`,
sleeps 3 seconds, writes `out.log` and `rc`, and exits with that rc. Use
`exec_command(yield_time_ms=1000)` and poll its returned `session_id` with
`write_stdin` in a later call. Inspect the files after the session finishes.
Repeat with a deliberate nonzero exit (for example 7) to check that both the
file and tool preserve failure. The worker guide's full-suite command uses
this same capture-and-exit pattern. If a backend lacks retained sessions,
record that fact rather than simulating one with a terminal subprocess.

On native Windows, perform the equivalent two-call delayed marker check using
`Start-Process pwsh` before relying on that fallback; POSIX results do not
verify PowerShell or Git Bash process lifetime.

## Evidence from 2026-11-08

Run `20261108_83ed3597`, implementation agent bob:

- Launcher-reported Codex 0.167.0, configured model `gpt-6.1-sol`; Linux
  6.8.0-147-generic, Bash 5.2.21; util-linux `setsid` 2.39.3.
- Actual tool route: `functions.exec` -> `exec_command`, then `write_stdin`
  for retained sessions. Commands used `sandbox_permissions=require_escalated`.
  The configured workspace-write `use_default` route failed **before shell
  startup**, with `bwrap: setting up uid map: Permission denied` (tool rc 1).
  That exact sandbox route remains **unverified in this run**. Alice accepted
  testing the available route with this limitation explicitly recorded.
- Scratch root:
  `$TMPDIR/detach-probe.zAmrFi`.
  The two detached variants used separate subdirectories, with a 0.2-second
  startup check in the launch call and a 3-second delay in the inspection call.
  Both launch and inspection tool calls returned rc 0.

| Available route / method | Startup | Completion log | Recorded rc | Final tool exit |
| --- | --- | --- | --- | --- |
| Unsandboxed `nohup sh ... &` | `started` | missing | missing | launch 0 only |
| Unsandboxed `setsid nohup sh ... &` | `started` | `finished` | 0 | launch 0 only |
| Foreground retained session, ID 99331 | `started` | `finished` | 0 | 0 |
| Foreground retained session, ID 22178, deliberate failure | `started` | `deliberate-failure` | 7 | 7 |

Tool-visible process metadata for the detached launch: parent
PID/PPID/PGID/SID `97572/97074/97572/97572`; nohup child
`97576/97572/97572/97572`; setsid child `97578/97572/97578/97578`.
Both launcher logs were empty. Re-running the two-call examples above with
their bounded startup loop gave the same results for both detached methods.
The distinct setsid session completed on this route; it does not establish survival in workspace-write sandboxes.

[Issue #355](https://github.com/RoboNater/robomate/issues/355) records **prior**
Codex 0.164.0 workspace-write observations: both nohup and setsid started but
lacked completion, while a retained session completed. Those are attributed
prior evidence, not a new sandbox result or a universal Codex claim. The
cleanup mechanism and regression date remain unknown.

Claude Code has separate successful evidence: the [issue's prior run
comments](https://github.com/RoboNater/robomate/issues/355) report successful
nohup validation under Claude Code 2.1.310's Bash tool on Linux. During this
run, [reviewer charlie's two-call probes](https://github.com/RoboNater/robomate/pull/437#issuecomment-6718363814)
on Claude Code 2.1.319's Bash tool, Linux 6.8.0-147, unsandboxed, completed
with `started`, `out.log` and rc `0` for both nohup and setsid nohup. Charlie
also completed the changed-file 250 ms validation using the POSIX detached
fallback and recorded rc `0`. These are attributed reviewer/prior-run results,
not probes performed by bob. Matching Claude Code Bash/Linux unsandboxed
workers can reuse that evidence; a changed shell tool or sandbox, or observed
loss of completion, calls for a fresh probe.

Opencode has separate successful evidence on the same host. Run
`20261108_089a9496`, implementation agent bob (provider meta, model
`opencode-go/muse-spark-1.3-contributor`), ran on opencode 1.19.5, Ubuntu
24.04.5 LTS, Linux 6.8.0-147-generic, Bash 5.2.21, util-linux `setsid` 2.39.3.
This is the same machine as the Codex and Claude Code evidence above; the
operator confirmed it. Actual tool route: opencode `bash` tool, unsandboxed
(no sandbox config; `permission.external_directory=allow`, `snapshot=false`
in `configs/bob.opencode.json`; probe wrote outside the worktree to
`$TMPDIR`). The prior run's closeout records a detached `nohup` marker probe
with `started`, `finished` and rc `0`, then two full
`uv run --locked pytest -n auto` suites via the detached fallback, both with
rc `0`: 2568 passed, 35 skipped at `9ef7f69`, and 2569 passed, 35 skipped at
`8837560` ([PR #439](https://github.com/RoboNater/robomate/pull/439)). That
closeout does not record sandbox mode, a `setsid nohup` trial, PID/PGID/SID,
or the probe scratch path, so the doc's two-call probe was rerun on this
host/version and is recorded directly below. Do not read the prior run as
having tried `setsid nohup`.

- Scratch roots `$TMPDIR/tmp.pItobPIudM` (`nohup`) and
  `$TMPDIR/tmp.z9rjcZ3zXs` (`setsid nohup`), both outside the worktree, with a
  0.05-second startup check in the launch call and a 3-second delay in the
  inspection call. Both launch and inspection tool calls returned rc 0.

| Available route / method | Startup | Completion log | Recorded rc | Final tool exit |
| --- | --- | --- | --- | --- |
| Unsandboxed `nohup sh ... &` | `started` | `finished` | 0 | launch 0 only |
| Unsandboxed `setsid nohup sh ... &` | `started` | `finished` | 0 | launch 0 only |

Tool-visible process metadata for the detached launch: `nohup` parent
PID/PPID/PGID/SID `398425/397226/398425/398425`; `nohup` child
`398429/398428/398425/398425` (same PGID/SID as parent, no new session);
`setsid` parent `398490/397226/398490/398490`; `setsid` child
`398494/398493/398494/398494` (distinct PGID/SID, new session). Both
launcher logs were empty. The distinct `setsid` session completed on this
route; it does not establish survival in a workspace-write sandbox.

Practical tip: detached `pytest -n auto` output is buffered so progress dots arrive late; poll the tail of `out.log` together with `ps` for the child to tell a stall from a slow run.

Matching opencode 1.19.x `bash`, Linux, same sandbox mode may reuse this
evidence; a changed opencode version, shell tool or sandbox, or observed loss
of completion, calls for a fresh probe.

Windows PowerShell, Windows Git Bash and other harness routes were not
executed in this run. Their recipes remain conditional fallbacks,
with an actual-route probe required before relying on detachment. Rendering
tests verify that the instructions reach workers; they cannot prove process
survival across a shell-tool boundary.

## Full-suite check on the selected route

The worker guide's foreground capture-and-exit recipe ran
`uv run --locked pytest -n auto` with four workers through the same unsandboxed
`exec_command` route, retained session 67775. It wrote `started`, a complete
`out.log` (2568 passed, 35 skipped in 499.84 seconds), and `rc` containing `0`;
`write_stdin` returned final process exit code `0`. Scratch was
`$TMPDIR/tmp.tL0oCcYwyt`.
No cache override was needed on this route.

The earlier full-suite session 39029 exposed a stale documentation-content
assertion, returning tool exit code `1` and file rc `1` (2567 passed, 35 skipped,
one failed). After updating the assertion from “detached suite” to “validation
suite,” the run above passed. This also checks that the retained-session
recipe preserves a real suite failure, not just the harmless control's rc 7.

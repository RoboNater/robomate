# rbm-bench: prompt for an agent running re-review cases

Hand this to an operator-side agent (for example Codex in an interactive
session) to benchmark one reviewer candidate. Replace the placeholders:

- `<MODEL>`: the candidate, e.g. `opencode-go/claude-haiku-5-5`
- `<LABEL>`: a short run label, e.g. `haiku55` (it names the run id, the GitLab
  project and the results directory; nothing else interprets it)
- `<CASES>`: case ids under `~/robomate-eval/cases/`, e.g. `review-201-r1`,
  `review-307-r1`, `review-143-r1`

Start the agent in `~/robomate-eval`, not in the tooling copy: an agent reads
`AGENTS.md` from its working directory, and the tooling copy's is robomate's
developer guide. Give it full access (Codex: `--sandbox danger-full-access`):
`run_case.py` starts its own bubblewrap sandboxes and Unix sockets, writes
under `~/robomate-eval` and reaches the eval GitLab and the model APIs. Keep
the host awake; a sleeping host stretches runs.

---

```text
Run the "re-review" model benchmark (rbm-bench) in ~/robomate-eval and report results.
Do not change any code.

Context: read ~/robomate-eval/tooling/scripts/rbm-bench/README.md first. The eval tooling is
the copy in ~/robomate-eval/tooling; run everything from there (run_case.py refuses to run
from ~/lw).

Candidate: OpenCode with model <MODEL>, as the reviewer (charlie). Use the model's default
variant/effort; do not pass --charlie-effort.
Cases (already built): <CASES>, as ~/robomate-eval/cases/<case>.json.

1. Smoke check the model first:
     cd ~/robomate-eval && opencode run --model <MODEL> "Reply with exactly: PONG"

2. For each case, one at a time, never in parallel, launch the run detached in its own
   session. Your shell tool kills the command's process group when the call returns, and
   `nohup ... &` alone stays in that group, so use setsid:

     cd ~/robomate-eval/tooling && setsid nohup uv run --locked python \
       scripts/rbm-bench/run_case.py ~/robomate-eval/cases/<case>.json --label <LABEL> \
       --charlie-harness opencode --charlie-model <MODEL> --budget-min 60 \
       > ~/robomate-eval/<LABEL>-<case>.log 2>&1 < /dev/null &

   Within a minute, confirm the run is alive: `pgrep -f "run_case.py .*<case>"` shows a PID
   and the log has an "[eval ...] seeding" line. Record the PID. Each run takes about
   2-20 minutes; poll the log for "results in" rather than holding one blocking call.
   To stop a run, send SIGINT to that exact PID; it then stops its agent, hub and proxy.

3. Watch for a known OpenCode startup stall: if ~/robomate-eval/results/<run id>/driver.jsonl
   has no "agent_checked_in" event within about 5 minutes of the "launching charlie" log
   line, send SIGINT to the run's run_case.py PID, wait for "results in" in the log, and
   rerun the case once with the same command. Report any rerun.

4. Score each finished run with the default judge:
     cd ~/robomate-eval/tooling && setsid nohup uv run --locked python \
       scripts/rbm-bench/score.py ~/robomate-eval/results/<run id> \
       > ~/robomate-eval/<LABEL>-<case>-score.log 2>&1 < /dev/null &
   Poll the log until it prints the score JSON. The judge may also stall; it retries on its
   own (up to three 5-minute attempts).

5. Report a table per case: candidate verdict, blocking findings caught (recall_blocking),
   extra valid / nit / false-positive counts, elapsed minutes and any egress_denied hosts
   (from summary.json and score.json). Compare with earlier runs of the same cases in
   ~/robomate-eval/results/<case>-*/score.json. For each case, read the candidate's review
   (mr-notes.json in the results directory) and say whether you agree with the judge, and why.

Rules:
- Don't edit scripts, cases or the tooling copy. If something breaks, stop and report the
  error and log paths instead of working around it.
- Don't touch ~/lw, any other robomate hub (one at :8420 may belong to another run), or the
  Claude/OpenAI accounts. run_case.py starts and stops its own hub.
- Never use `pkill -f` or `pkill` with a pattern; kill by exact PID.
- Don't delete the eval-* GitLab projects; leave the results directories in place.
```

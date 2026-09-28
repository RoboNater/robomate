# M1 acceptance run — issue #44

This record covers the live self-hosted workflow for
[RoboNater/robomate#44](https://github.com/RoboNater/robomate/issues/44).
The run identifier and operator-controlled restart facts still need to be
confirmed from the run's manifest and hub state before this record is final.

## Attempts

| Attempt | Source | Result |
| --- | --- | --- |
| 1 | `2c3daed8ad47b6b5f61583253e9c367755356649` on 2026-09-28 | Incomplete. Bob checked in, received IMPLEMENT task `41e63b24a9154880b31365eeac93b2d9`, and held it while Alice sought an operator-controlled harness restart. No restart evidence arrived, so Bob submitted a blocked result after pushing the preparation record; no PR was opened. |
| 2 | `c1f5d08b15170ae5ea0393e785a9ac4cae8b6148` on 2026-09-28 | In progress. Alice assigned RETRY task `1a4b27d6d8c443ba8d21bd525239c10e`. Bob verified the pushed branch and its evidence draft, and is holding this task while Alice obtains an operator restart decision. |

No restart, event redelivery, review, or merge is asserted here until the
corresponding live evidence is available. An operator must supply the actual
restart window and before/after hub snapshots. The finished record must compare
worker task owner and state, `agent_lost` and `lease_expired` events, worker MCP
telemetry errors, and event `delivery_attempts` across that window. It must also
record the exact approved PR head and the PR head at merge, plus `hub-report`
figures for Alice's MCP bytes and worker A2A bytes.

## Preparation and validation

Bob created branch `issue-44-m1-acceptance` from `main` at the source commit
above. The repository validation completed in the required order:

```sh
UV_CACHE_DIR=/tmp/robomate-uv-cache UV_LINK_MODE=copy uv sync --locked --all-packages --dev
UV_CACHE_DIR=/tmp/robomate-uv-cache UV_LINK_MODE=copy uv run --locked ruff check .
UV_CACHE_DIR=/tmp/robomate-uv-cache UV_LINK_MODE=copy uv run --locked mypy
UV_CACHE_DIR=/tmp/robomate-uv-cache UV_LINK_MODE=copy uv run --locked pytest
```

All four commands passed; pytest reported 749 passed, 2 deprecation warnings.
The cache override was needed because the default uv cache was read-only in
Bob's sandbox.

Bob also attempted `robomate status --json` in his own checkout. That checkout
had no `.robomate/hub.json`, and the CLI could not open the global registry lock
at `/home/alfred/.local/state/robomate/registry.lock` in this sandbox. This
failed diagnostic command is **not** a before-restart status snapshot. The
operator must capture status from the checkout that owns the hub.

## Evidence to complete after the live run

- Run identifier, manifest, hub checkout, and `robomate up` command/output.
- `robomate status --json` immediately before and after Alice's harness restart.
- Exact restart timestamps and the command or action that ended and relaunched
  Alice's harness session, while a worker's task remained active.
- Hub task rows before and after (task ID, assignee, state), and event rows in
  the restart window proving absence of `agent_lost` and `lease_expired`.
- Worker telemetry for the same window, including zero failed tool calls.
- In-flight event ID, delivery attempts before and after, and redelivery to
  Alice's new session.
- Review approval URL and full approved head SHA; PR head at merge and merge
  record; equality check. A squash merge commit has a distinct SHA, so compare
  the approved head with the PR head accepted by the merge gate.
- Final `hub-report` command and Alice MCP plus worker A2A byte figures.

The post-merge proof cannot be committed into the PR it describes before that
PR merges. Its final GitHub merge record must be linked in the close-out report
or a post-merge evidence update.

# Testing time and waits (#365, #367)

robomate's own test policy for anything that depends on time passing. It
applies to every test in `tests/`. Agents working on other repositories
don't need it, so it is not in `guides/`.

A test whose result depends on how much real time passes while it runs is
flaky: it passes on a fast host and fails on a loaded one. Windows CI is our
slowest host, so that is where these tests fail first. Widening the budget
only moves the failure to a slower day (#260).

## The rules

1. **Fake time for anything the code under test enforces.** A lease, a
   closeout window, the loss grace, a stall threshold: if the hub decides
   something when a deadline passes, the test puts that deadline on the
   `frozen_clock` fixture and passes it with `frozen_clock.advance(seconds)`.
   The deadline then passes exactly when the test says, on any host.
2. **Real time only as a generous bound that catches hangs.** A wait that
   needs a bound uses `LIVENESS_S` (60 s). A passing test never comes near
   it. It costs time only when a test is already failing, so never shorten it
   to make a test faster.
3. **Wait for conditions, not durations.** To wait for another task, a
   background thread or a subprocess, wait until the state you need is there,
   with `wait_until`. Never `sleep(x)` and then assert on what the other side
   did in `x`.
4. **No fixed short literals as hang guards.** A `timeout_s=0.8` hold or a
   `wait_for(..., 2)` that exists only so a broken test doesn't hang is a
   budget in disguise. Use `LIVENESS_S`.
5. **Count, don't time.** Don't assert that something took at least or at
   most some real duration. Even a lower bound can fail: on Windows CI a
   50 ms `time.sleep` measured 46 ms on `time.monotonic()` (#367). To show
   that a delay or a wait happened, record the calls, for example by
   patching `time.sleep`, as `tests/test_timing.py` does for the probe. The
   one exception is a lower bound on a timeout that the code under test
   enforces by checking its deadline against the same `time.monotonic()`
   the test reads, as `wait_until` and the hub's holds do. That file asserts
   that `wait_until` waited at least its timeout before failing. Timing a
   requested `time.sleep` doesn't qualify, because nothing checks a deadline
   there; that was #367's mistake. An upper bound is never safe, not even
   on the same clock: the process or event loop can resume well after the
   deadline. Any upper bound stays the `LIVENESS_S` hang guard.

A `timeout_s=0` hold is not a wait: it reads the current state and returns
at once, so it is fine.

## The tools

All live in [`tests/timing.py`](../../tests/timing.py) and
[`tests/conftest.py`](../../tests/conftest.py). Import them in any test file:

```python
from timing import LIVENESS_S, FrozenClock, wait_until
```

| Tool | What it does |
|---|---|
| `frozen_clock` fixture | A `FrozenClock`, installed by default on this test's `app` / `hub_store` and `store`. Both store clocks stand still until `frozen_clock.advance(seconds)`. Explicit opt-out with `@pytest.mark.real_clock`. |
| `frozen_clock.install(store)` | Drive another store's clocks from the same clock, such as a restarted app's: `frozen_clock.install(restarted.state.store)`. |
| `await wait_until(condition, what)` | Re-reads `condition()` every 10 ms, yielding to the event loop, and returns its value once it is truthy. After `LIVENESS_S` it fails with `timed out after 60 s waiting until <what>`. Write `what` so that sentence reads well. |
| `wait_until_sync(condition, what)` | The same for synchronous tests, such as ones that drive a subprocess hub. |
| `LIVENESS_S` | 60 s: the hang bound for waits and for holds such as `await_assignment(timeout_s=LIVENESS_S)`. |
| `@pytest.mark.hub_settings(field=value)` | Overrides fields of the test hub's `HubSettings`. The test hub caps every hold at `max_wait_s=1.0`, so a test whose hold must last until a condition holds lifts it with `@pytest.mark.hub_settings(max_wait_s=LIVENESS_S)`. |
| `@pytest.mark.real_clock` | Opt out of the default `frozen_clock` on `store` and `hub_store`. |
| `ROBOMATE_TEST_DB_LATENCY_MS` | The slow-host probe; see below. |

`frozen_clock` is installed by default on `store` and `hub_store` (Stage 2, #368). Tests that specifically verify real-time behaviors (such as background heartbeat timer intervals) opt out with `@pytest.mark.real_clock`. Test hub settings also default `detect_host_pauses=False` (`HUB_DETECT_HOST_PAUSES`); tests specifically exercising pause recovery enable it explicitly.

### The two-clock rule

`HubStore` reads two clocks. `clock` is wall time: it stamps rows and decides
when leases and windows expire. `monotonic_clock` lets `_detect_pause` notice
a host pause: if wall time runs more than `CLOCK_JUMP_TOLERANCE_S` (5 s) ahead
of monotonic time between sweeps, the hub records `host_paused` and shifts
leases and closeout deadlines ([host standby](host-standby.md)).

So **never move only the wall clock.** `hub_store.clock = lambda: now +
timedelta(seconds=30)` or `MonotonicClock.advance(30)` looks like a host
pause, and the test then checks pause recovery instead of what it meant to.
`FrozenClock` drives both clocks from one value, and `advance` moves them
together.

One more case: between two sweeps, an advance longer than the sweep's
`lost_after_s` on both clocks is a `sweep_gap`, which the hub also treats as a
pause, by design. The first sweep after a store is created has no baseline,
so it never records one. To lose a silent worker after an earlier sweep,
advance in steps shorter than `lost_after_s` and sweep after each.

### Holds use real time

`HubStore._wait_for`, which times `await_assignment`, `ask_alice` and
`wait_for_event` holds, measures their `timeout_s` with the module's
`monotonic()`, not the store's clocks. A frozen clock therefore never hangs a
hold: its timeout still runs out in real time
(`test_holds_time_out_in_real_time_on_a_frozen_clock` in
`tests/test_timing.py`). Give a hold that should end when a condition holds
`LIVENESS_S`, not a short budget.

### Tests that start a hub as a subprocess

Tests that run `robomate up`, `ls` or the `hub` entry point in another
process, such as `tests/test_main.py` and the `ls` tests in
`tests/test_cli.py`, can't reach that hub's clocks. They rely on condition
waits alone: wait with `wait_until_sync` until the process has done what the
test needs (a `/healthz` answer, a registry entry, a file), bounded by
`LIVENESS_S`, and don't assert on how long it took. The probe doesn't reach a
subprocess hub either.

A helper whose failure message carries diagnostics, such as `await_hub` in
`tests/test_cli.py`, which prints the process state, `hub.json`, the
startup stages `robomate up` marks on stderr and the hub's output, may keep
its own polling loop instead of `wait_until_sync`. The rule still applies to
its bound: `LIVENESS_S`, never a fixed budget (#372).

## Reproduce first: the slow-host probe, or the failing host

A timing flake is **not fixed until it has been reproduced, and the
repaired test passes under the same conditions**. A fix that was never seen
failing is a guess. For an in-process hub, reproduce it with the probe
described here. For a hub in a subprocess, which the probe can't reach, see
[Reproducing a subprocess-hub flake](#reproducing-a-subprocess-hub-flake).

The probe adds latency before each hub SQLite transaction in the test
process. It replaces `agent_hub.database.connect`, which
`agent_hub.database.database` calls once per transaction, with one that
sleeps first. The sleep blocks the event loop, as a slow disk does, because
the hub's SQLite calls are synchronous. It is off unless the variable is set,
and then adds nothing: the hub's own `connect` runs. pytest's header line
says which: `db latency probe: off` or `db latency probe: 50 ms`.

```sh
ROBOMATE_TEST_DB_LATENCY_MS=50 uv run --locked pytest tests/test_closeout.py -q
ROBOMATE_TEST_DB_LATENCY_MS=100 uv run --locked pytest tests/test_closeout.py -q
```

50 ms per transaction is harsher than CI, so treat thresholds as rough; what
matches the CI history is which tests fail. To repair a flaky test:

1. Run it with the probe at 30, 50 or 100 ms until it fails with the error CI
   showed. Record the command and the failure lines.
2. Find each place the test depends on real time: a deadline the hub enforces
   while the test steps through it, a `sleep` before an assertion, a short
   hold or `wait_for` timeout, a store clock replaced by hand.
3. Apply the rules: name `frozen_clock`, replace each enforced deadline's real
   wait with `advance`, each sleep-then-assert with `wait_until`, each short
   guard with `LIVENESS_S`. Change the test, not production code; if
   production code must change, say why in the PR (AGENTS.md).
4. Run it under the probe at 50 and 100 ms ten times each, stopping at the
   first failure, and report the count:

   ```sh
   for i in $(seq 10); do ROBOMATE_TEST_DB_LATENCY_MS=100 uv run --locked pytest tests/test_closeout.py -q || break; done
   ```

### Reproducing a subprocess-hub flake

A hub started by `robomate up` or the `hub` entry point runs in another
process, so the probe adds nothing there. Reproduce the failure under the
conditions that produce it, in one or more of these ways:

- **Native Windows under load.** Run the test in a loop while a full
  `uv run --locked pytest -n auto` suite runs alongside it, or run two full
  suites at once, each in its own clone, as #365's acceptance does.
- **A CI recurrence that names the cause.** A failure message that only says
  "did not start" or "assert False" shows that the flake exists, not why.
  First make the failure say where the time went: the process state, the
  files and registry entries the test waits for, and the stage the
  subprocess reached. Then the next recurrence is evidence.
- **A controlled delay at the stage the evidence names.** Delaying that one
  step shows that the repaired test waits for the condition rather than for
  a duration. A test-only hook like this must be off by default, as the
  probe is, and the PR must name it.

The fix is done when the PR names the cause from that evidence and shows the
repaired test passing under the same conditions. A run of local passes alone
is not enough on a host where the test rarely fails. #350 failed 0 times in
60 local runs on Linux while Linux CI kept hitting it.

## Worked example: `test_isolated_collection_scenario`

`tests/test_closeout.py::test_isolated_collection_scenario` turned main's
Windows check red (#365). Under the probe it failed both ways CI did: at
50 ms, `ConflictError: closeout submission window has ended for this author`
at charlie's `submit_closeout_report`; at 100 ms, `KeyError:
'closeout_requested'` at charlie's `await_assignment`, in both
parametrizations.

**Before** (main at `2d344c5`, abridged):

```python
async def test_isolated_collection_scenario(app, hub_store, settings, crash):
    ...
    pending = asyncio.create_task(call(servers[0], "await_assignment", timeout_s=0.8))
    await asyncio.sleep(0.01)
    requested = await call(alice, "request_closeout_reports", run_id=run_id, timeout_s=1)
    ...
    if crash:
        now = hub_store._now()
        hub_store.clock = lambda: now + timedelta(seconds=1.1)
        await workers[0].heartbeat()
        hub_store.sweep(0.5)
```

Its real-time dependencies:

- The closeout window (`timeout_s=1`) ran on the store's real clock while
  the test stepped through it. On a slow host it closed before charlie read
  the request (the `KeyError`) or submitted (the `ConflictError`).
- bob's hold had a 0.8 s budget, and the test hub's 1 s ceiling capped it
  anyway.
- `asyncio.sleep(0.01)` guessed that bob's hold was parked by then.
- The crash branch moved only the wall clock.

**After:**

```python
WINDOW_S = 100  # on the frozen clock: part of the scenario, not a budget


@pytest.mark.parametrize("crash", [False, True])
@pytest.mark.hub_settings(max_wait_s=LIVENESS_S)
async def test_isolated_collection_scenario(app, hub_store, settings, frozen_clock, crash):
    ...
    pending = asyncio.create_task(call(servers[0], "await_assignment", timeout_s=LIVENESS_S))
    await wait_until(lambda: holding(hub_store, "bob"), "bob's hold is parked")
    requested = await call(alice, "request_closeout_reports", run_id=run_id, timeout_s=WINDOW_S)
    ...
    if crash:
        frozen_clock.advance(max(WINDOW_S, settings.lost_after_s) + 1)
        await workers[0].heartbeat()
        hub_store.sweep(settings.lost_after_s)
    ...
    restarted = create_app(settings)
    frozen_clock.install(restarted.state.store)
```

- The window and the loss grace run on the frozen clock. They pass only at
  `advance`, which the crash branch uses to lose charlie and expire the
  window; the other branch never advances, so the window stays open however
  slow the host is.
- bob's hold and the wait for it are bounded by `LIVENESS_S`; the marker
  lifts the 1 s ceiling.
- `holding` reads the hub's activity tracker for bob's parked
  `await_assignment` hold, replacing the sleep.
- The restarted app shares the frozen clock.

The repaired test passed 10 of 10 runs of `tests/test_closeout.py` at 50 ms
and at 100 ms.

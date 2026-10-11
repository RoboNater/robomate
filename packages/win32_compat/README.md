# robomate-win32-compat

A ctypes stand-in for the pywin32 calls that `mcp` makes on Windows (#181). MIT,
like the rest of the repository.

## Why it exists

`mcp` requires `pywin32` on Windows, and `pywin32` bundles `adodbapi` under
LGPL-2.1. robomate uses none of pywin32 itself. `mcp.os.win32.utilities` imports
`pywintypes`, `win32api`, `win32con` and `win32job`, and uses seven calls from
them to put each stdio server that `stdio_client` spawns, and that server's
descendants, in a kill-on-close Job Object.

The root `pyproject.toml` removes pywin32 from the workspace with
`exclude-dependencies = ["pywin32"]`, and this package, a Windows-only dependency
of `agent-hub` and `worker-mcp`, provides those four modules with exactly the
names `mcp` uses. It is written against `kernel32` from the documented Win32 API;
nothing is copied from pywin32. Job handles are `PyHANDLE`-like objects that
close at most once and close on garbage collection, because `mcp` relies on
collection to close the job after a normal shutdown.

## What a license report will show

- `uv.lock` has no `pywin32` package; its manifest records `excludes = ["pywin32"]`.
- The installed environment has no `pywin32` distribution and no `adodbapi`.
- `mcp`'s own metadata still declares `Requires-Dist: pywin32; sys_platform == 'win32'`.
  uv deliberately leaves that requirement unmet. This package does not satisfy
  it, since it is a different distribution; it supplies the modules and calls
  `mcp` imports. Tools that check declared requirements will report pywin32 as
  missing: on Windows, `uv pip check` exits 1, reporting that `mcp` requires
  `pywin32>=310` but it's not installed. That is expected under this
  arrangement, and this README is the explanation.
- This package is MIT; its `LICENSE` is a copy of the repository's, and both the
  wheel and the sdist carry it.

## Limits

- **Only uv installs from this workspace honour the exclusion.** `pip install` of
  robomate's packages, or any install that ignores the root `[tool.uv]` table,
  pulls in real pywin32. Both would then provide `win32job` and the rest, and
  which one Python imports depends on path order. The planned
  `uv tool install git+…` path (`docs/mvp-spec.md` §2) must keep the exclusion
  and this package, or replace this arrangement, before it ships.
- **A new mcp release may use a call this package lacks.** mcp would then log a
  warning and run without a job. `tests/test_win32_compat.py` drives mcp's own
  job code on Windows, so that shows up as a CI failure.
- `exclude-dependencies` sets the uv minimum in the root `required-version`; CI pins
  the same version.

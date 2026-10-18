# Editing shared issue and PR bodies

Use this procedure for authorized edits, especially roadmap #2. It prevents
failed builds, accumulated newlines, Windows encoding damage and stale-copy
overwrites (#255). Replace only exact text within its own uniquely anchored
section; never rebuild whole sections from memory or an earlier fetch.

## Prepare the body without shell encoding conversions

Create a unique temporary directory (commands below). Save the following as
`body-edit.py` inside it. This is a temporary recipe, not a repository script.
It needs Python 3 and authenticated `gh`. If your host only provides `python3`,
substitute `python3` for every `python` command below. Both shell recipes use it
because Windows PowerShell 5.1 native-output pipelines/redirection can decode UTF-8 with
the console code page and write a BOM. Python captures `gh` bytes directly and
decodes JSON; it writes the body's UTF-8 bytes with **no added newline**. Never
use `--jq .body` here or strip all trailing newlines. Legitimate trailing blank
lines must survive. An API `null` body represents an empty description and is
written as an empty file.

Configure `EDITS` as tuples of `(section_start, section_end, old, new)`, with
unique, exact start/end anchors in document order, and set `MAX_CHANGED_LINES`
to the reviewed scope (removed plus added lines). Keep anchors outside the text
being replaced. For example, use two adjacent headings to delimit one section.
Leave `EDITS = []` and the limit at zero for a read-only round-trip check.
Use an editor that saves this temporary script as UTF-8.

```python
import difflib
import json
import subprocess
import sys
from pathlib import Path

EDITS = []
MAX_CHANGED_LINES = 0

stage, directory, endpoint = sys.argv[1:]
root = Path(directory)
snapshot = root / "snapshot.md"
candidate = root / "body.md"


def require(ok, message):
    if not ok:
        raise SystemExit(message)


def fetch():
    raw = subprocess.check_output(["gh", "api", endpoint])
    return (json.loads(raw.decode("utf-8"))["body"] or "").encode("utf-8")


def planned():
    text = snapshot.read_bytes().decode("utf-8")
    for start, end, old, new in EDITS:
        require(start and end and old, "Empty anchor or replacement source")
        require(text.count(start) == text.count(end) == 1, "Ambiguous section")
        left = text.index(start) + len(start)
        right = text.index(end)
        require(left <= right, "Section anchors out of order")
        section = text[left:right]
        require(section.count(old) == 1, "Missing or ambiguous source text")
        text = text[:left] + section.replace(old, new, 1) + text[right:]
    return text.encode("utf-8")


def check():
    before = snapshot.read_bytes().decode("utf-8")
    after = candidate.read_bytes().decode("utf-8")
    require("\ufeff" not in after, "U+FEFF / BOM in candidate")
    # Exact reconstruction preserves all characters outside the intended edits,
    # including every non-ASCII character and all trailing newlines.
    require(candidate.read_bytes() == planned(), "Candidate differs from anchored edits")
    delta = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="snapshot",
            tofile="candidate",
        )
    )
    changed = sum(1 for line in delta[2:] if line.startswith(("+", "-")))
    require(changed <= MAX_CHANGED_LINES, "Changed-line count exceeds scope")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write("".join(delta))
    print("Checked changed lines:", changed)


if stage == "snapshot":
    require(not snapshot.exists(), "Use a new temporary directory")
    snapshot.write_bytes(fetch())
elif stage == "build":
    candidate.write_bytes(planned())
elif stage == "check":
    check()
elif stage == "fresh":
    check()
    fresh = fetch()
    (root / "fresh.md").write_bytes(fresh)
    require(fresh == snapshot.read_bytes(), "Stale snapshot: stop and rebuild")
elif stage == "verify":
    live = fetch()
    (root / "live.md").write_bytes(live)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(
        "".join(
            difflib.unified_diff(
                snapshot.read_bytes().decode("utf-8").splitlines(keepends=True),
                live.decode("utf-8").splitlines(keepends=True),
                fromfile="snapshot",
                tofile="live",
            )
        )
    )
    require(live == candidate.read_bytes(), "Live body differs: investigate; do not overwrite")
else:
    raise SystemExit("Unknown stage")
```

Review the printed diff and changed-line count before the write sequence. The
exact reconstruction check rejects a cp437-garbled copy even when it is valid
UTF-8, and permits intentional non-ASCII edits only inside the configured
replacement. The explicit U+FEFF check also rejects a BOM. If an existing body
starts with U+FEFF, stop and arrange an explicit repair before retrying. This
procedure does not silently repair existing encoding or trailing blank lines.

## Bash (including Git Bash)

```bash
D=$(mktemp -d) && printf '%s\n' "$D"
# Save/configure body-edit.py in that directory, then:
H="$D/body-edit.py"
E=repos/RoboNater/robomate/issues/2
python "$H" snapshot "$D" "$E" &&
python "$H" build "$D" "$E" &&
python "$H" check "$D" "$E"
```

After reviewing the diff, run this only for an authorized live edit. Tool calls
may not preserve shell variables: reassign these in the same call as the write,
using the existing directory printed during preparation:

```bash
D='<existing directory printed above>'
H="$D/body-edit.py"
E=repos/RoboNater/robomate/issues/2
python "$H" fresh "$D" "$E" &&
gh issue edit 2 --repo RoboNater/robomate --body-file "$D/body.md" &&
python "$H" verify "$D" "$E"
```

Keep every step through `gh issue edit` in the `&&` chain. Alternatively put the
whole sequence in a script file with `set -euo pipefail` at its top and execute
`bash <file>`; do not source it or run it as a conditional. Bare `set -e` in an
agent tool shell may be suppressed (observed in Claude Code on WSL and native
Windows Git Bash). An explicit chain or a fresh Bash script stops before the
write on a failure.

## Native PowerShell (Windows PowerShell 5.1 and PowerShell 7)

```powershell
$D = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
[IO.Directory]::CreateDirectory($D) | Out-Null
$D
# Save/configure body-edit.py in that directory, then:
$H = Join-Path $D 'body-edit.py'
$E = 'repos/RoboNater/robomate/issues/2'
function Invoke-Checked {
    $ErrorActionPreference = 'Stop'
    $command, $arguments = $args[0], $args[1..($args.Count - 1)]
    & $command @arguments
    if ($LASTEXITCODE -ne 0) { throw "Native command failed: $command ($LASTEXITCODE)" }
}
Invoke-Checked python $H snapshot $D $E
Invoke-Checked python $H build $D $E
Invoke-Checked python $H check $D $E
```

After reviewing the diff, run this only for an authorized live edit. In a
`.ps1` file, copy the `$D`/`$H`/`$E` assignments and `Invoke-Checked` definition
above, then append this block. Set `$D` to the existing directory you reviewed;
omit directory creation and the snapshot/build calls. Invoke the file with
`powershell.exe -NoProfile -File <file>` or `pwsh -NoProfile -File <file>`;
a thrown error must terminate it before the write, even if its caller continues.

```powershell
Invoke-Checked python $H fresh $D $E
Invoke-Checked gh issue edit 2 --repo RoboNater/robomate --body-file (Join-Path $D 'body.md')
Invoke-Checked python $H verify $D $E
```

Do not pipe body text into or out of `gh` through PowerShell, or use
`Set-Content`/`Out-File` defaults. If writing text directly in PowerShell, use
`[IO.File]::WriteAllText($path, $text, [Text.UTF8Encoding]::new($false))` after
reading it explicitly as UTF-8, then run the same checks on the exact bytes.

For PR bodies, use the PR number in the same `issues/<number>` API endpoint
and replace `gh issue edit` with `gh pr edit`. Keep temporary snapshots until
the post-write diff is checked. A failed freshness check means stop or rebuild
from a new snapshot, never retry the stale write. Fetch/compare/write is not an
atomic transaction: another editor can still race that final interval; agree
on one editor when concurrent edits are expected, and investigate any
post-write mismatch before attempting another edit.

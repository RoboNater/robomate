# Invoke the configurable helper from any working directory.
$ErrorActionPreference = 'Stop'
$robomateRoot = Split-Path -Parent $PSScriptRoot
& uv sync --locked --all-packages --project $robomateRoot
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& uv run --locked --project $robomateRoot python "$robomateRoot/scripts/prep-standard-run-area.py" @args
exit $LASTEXITCODE

$ErrorActionPreference = "Stop"
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
& $Python (Join-Path $PSScriptRoot "apply.py") @args
exit $LASTEXITCODE

param([ValidateSet('gateway','test','live')][string]$Action = 'gateway')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$candidates = @()
if ($env:CAPTAIN_PYTHON) { $candidates += $env:CAPTAIN_PYTHON }
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) {
    $resolved = & $pyLauncher.Source -3 -c "import sys; print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0) { $candidates += $resolved }
}
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCmd) { $candidates += $pythonCmd.Source }
$candidates += (Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe')
$runtime = $null
foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) {
        & $candidate -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $runtime = $candidate; break }
    }
}
if (-not $runtime) { throw 'Python 3.9+ required. Set CAPTAIN_PYTHON to its full executable path.' }
$env:PYTHONIOENCODING = 'utf-8'
Write-Host "Python: $runtime"
switch ($Action) {
    'gateway' { & $runtime server.py }
    'test' { & $runtime -m unittest discover -v }
    'live' { & $runtime -u verify_delivery.py }
}
exit $LASTEXITCODE

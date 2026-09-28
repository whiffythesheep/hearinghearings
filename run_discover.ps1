# Scheduled-task entrypoint for discover_pending.py.
#
# Registered with Windows Task Scheduler under "HearingHearingsDiscover".
# All Python output (stdout, stderr, logging) is appended to discover.log
# alongside this script. Run interactively at any time to re-poll.
#
# Notes:
# - Banner and trailer use Add-Content with -Encoding utf8 so PS 5.1
#   doesn't fall back to UTF-16.
# - Python output is redirected through cmd /c so the bytes Python emits
#   land in the file untouched. PYTHONIOENCODING=utf-8 forces Python's
#   stderr to UTF-8 rather than the console code page.

$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot

$logFile = Join-Path $PSScriptRoot 'discover.log'
$python  = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

$banner = "=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz') ==="
Add-Content -Path $logFile -Value $banner -Encoding utf8

$env:PYTHONIOENCODING = 'utf-8'

# Record refresh first (recent + upcoming meetings, unsettled matters), so
# pending branches below are cut from an up-to-date master. It resets the
# tree itself on failure; discover runs either way.
cmd /c "`"$python`" refresh_record_nightly.py >> `"$logFile`" 2>&1"
Add-Content -Path $logFile -Value "--- record refresh exit $LASTEXITCODE ---" -Encoding utf8

cmd /c "`"$python`" discover_pending.py >> `"$logFile`" 2>&1"
$rc = $LASTEXITCODE

Add-Content -Path $logFile -Value "--- exit $rc ---" -Encoding utf8
Add-Content -Path $logFile -Value '' -Encoding utf8

exit $rc

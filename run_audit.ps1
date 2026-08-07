# Scheduled-task entrypoint for audit_dependencies.py.
#
# Registered with Windows Task Scheduler under "HearingHearingsAudit" (weekly).
# Mirrors run_discover.ps1: output is appended to audit.log alongside this
# script, and Python output goes through cmd /c so its bytes land untouched.
#
# Why this runs locally rather than as a cloud routine: the whole point is to
# audit the *installed* venv, which drifts stale independently of what
# requirements.txt declares. On 2026-08-07 the venv had vulnerabilities in seven
# packages (yt-dlp four months behind, pypdf entirely unused but carrying eight
# advisories) while GitHub's Dependabot reported one. A cloud agent gets a fresh
# checkout and a fresh install, so it can never see that drift.
#
# audit_dependencies.py exits 1 when something is actionable, 0 when the only
# findings are BLOCKED ones (the accepted Windows ARM64 cryptography cap).
# So a clean week is silent, and exit 1 raises a flag the user will actually
# trip over rather than a log line nobody reads.

$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot

$logFile  = Join-Path $PSScriptRoot 'audit.log'
$flagFile = Join-Path $PSScriptRoot 'AUDIT-ACTION-REQUIRED.txt'
$python   = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

$banner = "=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz') ==="
Add-Content -Path $logFile -Value $banner -Encoding utf8

$env:PYTHONIOENCODING = 'utf-8'
$report = & cmd /c "`"$python`" audit_dependencies.py 2>&1"
$rc = $LASTEXITCODE

Add-Content -Path $logFile -Value $report -Encoding utf8
Add-Content -Path $logFile -Value "--- exit $rc ---" -Encoding utf8
Add-Content -Path $logFile -Value '' -Encoding utf8

if ($rc -eq 1) {
    # Something is actually fixable. Leave a file that is hard to miss.
    $header = @(
        "Dependency audit found something actionable."
        "Generated $(Get-Date -Format 'yyyy-MM-dd HH:mm')  by run_audit.ps1"
        "Re-run any time with:  .venv\Scripts\python.exe audit_dependencies.py"
        "This file is regenerated each run and deleted automatically once clean."
        ""
    )
    Set-Content -Path $flagFile -Value ($header + $report) -Encoding utf8

    # Best-effort desktop notification. Never let a UI failure fail the task.
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $icon = New-Object System.Windows.Forms.NotifyIcon
        $icon.Icon = [System.Drawing.SystemIcons]::Warning
        $icon.BalloonTipTitle = 'Hearing Hearings dependency audit'
        $icon.BalloonTipText  = 'Actionable findings - see AUDIT-ACTION-REQUIRED.txt'
        $icon.Visible = $true
        $icon.ShowBalloonTip(15000)
        Start-Sleep -Seconds 12
        $icon.Dispose()
    } catch {
        Add-Content -Path $logFile -Value "(notification failed: $($_.Exception.Message))" -Encoding utf8
    }
}
elseif (Test-Path $flagFile) {
    # Previously flagged, now clean: clear the flag so it never goes stale.
    Remove-Item $flagFile -Force
    Add-Content -Path $logFile -Value '(cleared previous AUDIT-ACTION-REQUIRED.txt)' -Encoding utf8
}

exit $rc

$env:PYTHONUTF8 = "1"
$logDir  = "$PSScriptRoot\logs"
$logFile = "$logDir\weather_report.log"

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

"`n=== $(Get-Date) ===" | Out-File -FilePath $logFile -Append -Encoding UTF8
& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\main.py" 2>&1 |
    Out-File -FilePath $logFile -Append -Encoding UTF8

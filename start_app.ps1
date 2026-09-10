Set-Location "C:\Users\admin\Documents\newjob"

$logDir = "logs"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

$logFile = Join-Path $logDir "app_$(Get-Date -Format 'yyyyMMdd_HHmmss').log"

cmd.exe /c "`".venv\Scripts\python.exe`" app.py > `"$logFile`" 2>&1"

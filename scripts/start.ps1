param([string]$Config = (Join-Path (Split-Path $PSScriptRoot -Parent) 'config.json'))
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$configPath = (Resolve-Path -LiteralPath $Config).Path
$settings = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$runtimePath = $settings.runtime_dir
if (-not $runtimePath) { $runtimePath = '.runtime' }
if (-not [IO.Path]::IsPathRooted($runtimePath)) { $runtimePath = Join-Path (Split-Path $configPath -Parent) $runtimePath }
$runtimePath = [IO.Path]::GetFullPath($runtimePath)
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { $pythonPath = (Get-Command python -ErrorAction Stop).Source }
$listenerRecord = Join-Path $runtimePath 'supervisor-process.json'
if (Test-Path -LiteralPath $listenerRecord) {
    $record = Get-Content -LiteralPath $listenerRecord -Raw | ConvertFrom-Json
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId = $($record.pid)" -ErrorAction SilentlyContinue
    if ($existing -and $existing.CommandLine -like '*dsh2github*supervise*') {
        Write-Output "Listener supervisor is already running: PID $($record.pid)"
        exit 0
    }
}
$arguments = @('-m', 'dsh2github', '--config', ('"' + $configPath + '"'), 'supervise')
$process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath 'watch.stdout.log') -RedirectStandardError (Join-Path $runtimePath 'watch.stderr.log')
$startTime = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
Start-Sleep -Seconds 2
if ($process.HasExited) { throw 'Listener exited; inspect .runtime/watch.stderr.log' }
$ready = $false
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    $statusFile = Join-Path $runtimePath 'watch-status.json'
    if (Test-Path -LiteralPath $statusFile) {
        try {
            $status = Get-Content -LiteralPath $statusFile -Raw | ConvertFrom-Json
            $actual = Get-Process -Id $status.pid -ErrorAction SilentlyContinue
            if ($actual -and $status.time -ge $startTime -and $status.ok) { $ready = $true; break }
        } catch {}
    }
    Start-Sleep -Seconds 1
}
if ($ready) { Write-Output "Listener verified ready: PID $($status.pid); supervisor PID $($process.Id)" }
else { Write-Output 'Supervisor started, but remote polling is not ready; run dsh2github doctor for the current error.' }
Write-Output "https://github.com/$($settings.repo)/issues"

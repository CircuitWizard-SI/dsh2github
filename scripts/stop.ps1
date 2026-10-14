param([string]$Config = (Join-Path (Split-Path $PSScriptRoot -Parent) 'config.json'))
$ErrorActionPreference = 'Stop'
$configPath = (Resolve-Path -LiteralPath $Config).Path
$settings = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$runtimePath = $settings.runtime_dir
if (-not $runtimePath) { $runtimePath = '.runtime' }
if (-not [IO.Path]::IsPathRooted($runtimePath)) { $runtimePath = Join-Path (Split-Path $configPath -Parent) $runtimePath }
$runtimePath = [IO.Path]::GetFullPath($runtimePath)
# Stop the supervisor first, then the real listener PID and its venv launcher.
foreach ($recordName in @('supervisor-process.json', 'watch-status.json', 'listener-process.json')) {
    $listenerRecord = Join-Path $runtimePath $recordName
    if (-not (Test-Path -LiteralPath $listenerRecord)) { continue }
    $record = Get-Content -LiteralPath $listenerRecord -Raw | ConvertFrom-Json
    foreach ($candidatePid in @($record.pid, $record.child_pid)) {
        if (-not $candidatePid) { continue }
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $candidatePid" -ErrorAction SilentlyContinue
        if ($process) {
            if ($process.CommandLine -notmatch 'dsh2github.*(watch|supervise)' -or $process.CommandLine -notlike ('*' + $configPath + '*')) { throw 'PID identity does not match this listener; refusing to stop' }
            Stop-Process -Id $candidatePid -ErrorAction SilentlyContinue
        }
    }
    if ($recordName -ne 'watch-status.json') { Remove-Item -LiteralPath $listenerRecord }
}
Write-Output 'Listener stopped. Already accepted DSH tasks continue; permissions return to local confirmation when the heartbeat expires.'

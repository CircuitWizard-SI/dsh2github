param([string]$Config = (Join-Path (Split-Path $PSScriptRoot -Parent) 'config.json'))
$ErrorActionPreference = 'Stop'
$configPath = (Resolve-Path -LiteralPath $Config).Path
$settings = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$runtimePath = $settings.runtime_dir
if (-not $runtimePath) { $runtimePath = '.runtime' }
if (-not [IO.Path]::IsPathRooted($runtimePath)) { $runtimePath = Join-Path (Split-Path $configPath -Parent) $runtimePath }
$listenerRecord = Join-Path ([IO.Path]::GetFullPath($runtimePath)) 'listener-process.json'
if (-not (Test-Path -LiteralPath $listenerRecord)) { Write-Output 'No listener PID record'; exit 0 }
$record = Get-Content -LiteralPath $listenerRecord -Raw | ConvertFrom-Json
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $($record.pid)" -ErrorAction SilentlyContinue
if ($process) {
    if ($process.CommandLine -notlike '*dsh2github*watch*' -or $process.CommandLine -notlike ('*' + $configPath + '*')) { throw 'PID identity does not match this listener; refusing to stop' }
    Stop-Process -Id $record.pid
}
Remove-Item -LiteralPath $listenerRecord
Write-Output 'Listener stopped. Already accepted DSH tasks continue; permissions return to local confirmation when the heartbeat expires.'

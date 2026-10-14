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
$listenerRecord = Join-Path $runtimePath 'listener-process.json'
if (Test-Path -LiteralPath $listenerRecord) {
    $record = Get-Content -LiteralPath $listenerRecord -Raw | ConvertFrom-Json
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId = $($record.pid)" -ErrorAction SilentlyContinue
    if ($existing -and $existing.CommandLine -like '*dsh2github*watch*') {
        Write-Output "Listener is already running: PID $($record.pid)"
        exit 0
    }
}
$arguments = @('-m', 'dsh2github', '--config', ('"' + $configPath + '"'), 'watch')
$process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath 'watch.stdout.log') -RedirectStandardError (Join-Path $runtimePath 'watch.stderr.log')
@{ pid = $process.Id; config = $configPath; started = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $listenerRecord -Encoding utf8
Start-Sleep -Seconds 2
if ($process.HasExited) { throw 'Listener exited; inspect .runtime/watch.stderr.log' }
Write-Output "Listener started: PID $($process.Id)"
Write-Output "https://github.com/$($settings.repo)/issues"

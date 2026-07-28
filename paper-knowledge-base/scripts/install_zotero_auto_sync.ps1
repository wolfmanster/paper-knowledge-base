<#
.SYNOPSIS
Install or remove the Zotero auto-sync watcher as a per-user Windows task.

.DESCRIPTION
All paths are discovered from this script's location, so the repository can be
moved or cloned on another Windows computer before running the installer.
#>

[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [string]$ZoteroDir = "",
    [string]$MinerUDir = "",
    [ValidateRange(5, 604800)]
    [int]$IntervalSeconds = 86400,
    [ValidateRange(0, 300)]
    [int]$SettleSeconds = 5,
    [string]$TaskName = "PaperKnowledgeBase-ZoteroAutoSync",
    [switch]$Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Uninstall) {
    $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $existingTask) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已移除后台任务: $TaskName"
    }
    else {
        Write-Host "后台任务不存在: $TaskName"
    }
    exit 0
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$watchScript = (Resolve-Path (Join-Path $PSScriptRoot "watch_zotero.py")).Path

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $PythonExe = (Get-Command python.exe -ErrorAction Stop).Source
}
$PythonExe = (Resolve-Path $PythonExe).Path

& $PythonExe -c "import chromadb, sentence_transformers"
if ($LASTEXITCODE -ne 0) {
    throw "当前 Python 缺少知识库依赖。请先运行: python -m pip install -r requirements.txt"
}

$watchArguments = @(
    $watchScript,
    "--interval",
    [string]$IntervalSeconds,
    "--settle-seconds",
    [string]$SettleSeconds
)
if (-not [string]::IsNullOrWhiteSpace($ZoteroDir)) {
    $watchArguments += @("--zotero-dir", (Resolve-Path $ZoteroDir).Path)
}
if (-not [string]::IsNullOrWhiteSpace($MinerUDir)) {
    $watchArguments += @("--mineru-dir", (Resolve-Path $MinerUDir).Path)
}

$quotedArguments = $watchArguments | ForEach-Object {
    '"{0}"' -f ([string]$_).Replace('"', '\"')
}
$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument ($quotedArguments -join " ") `
    -WorkingDirectory $projectRoot
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "监听 Zotero 新论文，通过 MinerU 自动加入 Paper Knowledge Base"

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existingTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "已安装并启动后台任务: $TaskName"
Write-Host "监听日志: $(Join-Path $projectRoot 'kb\watch_zotero.log')"
Write-Host "同步日志: $(Join-Path $projectRoot 'kb\sync_zotero.log')"
Write-Host "卸载命令: powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Uninstall"

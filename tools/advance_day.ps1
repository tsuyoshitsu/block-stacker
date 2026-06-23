# tools/advance_day.ps1
#
# Daily model step-up: 今日の step モデルで ai_server を起動し current_day を +1 する。
#
# 使い方:
#   tools\advance_day.ps1                        # ai_server 起動 + current_day を進める
#   tools\advance_day.ps1 -DryRun                # 表示のみ（ai_server は起動しない）
#   tools\advance_day.ps1 -NoAdvance             # 起動するが current_day は進めない
#   tools\advance_day.ps1 -DurationSeconds 3600  # 1時間後に ai_server を自動終了
#
# パラメータ:
#   -DurationSeconds <int> : 0 = 無制限（常駐）（既定）。指定すると ai_server が
#                            その秒数経過後に自動終了する（--duration として渡す）。
#                            例: -DurationSeconds 3600  → 1時間後に自動終了
#
# Day ルール:
#   day 1(月) ~ day 5(金) : step_01.zip ~ step_05.zip を順に使用
#   day 5 超 (土・日)      : step_05.zip を固定表示
#   同日に複数回呼ばれた場合: current_day を進めず ai_server だけ再起動（冪等）
#
# タスクスケジューラ:
#   月-金 14:00 -> tools\advance_day.ps1
#   日曜 学習後 -> tools\curate_week.ps1  (state.json が current_day=1 にリセット)

param(
    [string]$WeeksDir         = "output\weeks",
    [string]$FinalModelPath   = "output\mvp2\sac_final.zip",
    [string]$Python           = ".venv\Scripts\python.exe",
    [string]$AiHost           = "127.0.0.1",
    [int]$AiPort              = 8765,
    [int]$DurationSeconds     = 0,
    [switch]$DryRun,
    [switch]$NoAdvance
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- ai_server 操作
function Stop-AiServer {
    $procs = @(
        Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
            Where-Object { $_.CommandLine -and $_.CommandLine -match "block_stacker\.mvp3\.ai_server" }
    )
    if ($procs.Count -eq 0) {
        Write-Host "  (ai_server not running)" -ForegroundColor DarkGray
        return
    }
    foreach ($p in $procs) {
        Write-Host "  stopping ai_server PID $($p.ProcessId)" -ForegroundColor DarkGray
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep 1
}

function Start-AiServer {
    param([string]$ModelPath, [int]$Duration = 0)
    if (-not (Test-Path $ModelPath)) {
        Write-Host "ERROR: model file not found: $ModelPath" -ForegroundColor Red
        exit 1
    }
    $argList = @(
        "-m", "block_stacker.mvp3.ai_server",
        "--model", $ModelPath,
        "--host", $AiHost,
        "--port", $AiPort
    )
    if ($Duration -gt 0) {
        $argList += @("--duration", $Duration)
    }
    $proc = Start-Process -FilePath $Python -ArgumentList $argList -PassThru -WindowStyle Hidden
    Start-Sleep 3
    if ($proc.HasExited) {
        Write-Host "ERROR: ai_server exited immediately (code=$($proc.ExitCode))" -ForegroundColor Red
        exit 1
    }
    $durStr = if ($Duration -gt 0) { "  duration=${Duration}s" } else { "" }
    Write-Host "  ai_server PID $($proc.Id) started  ->  ws://${AiHost}:${AiPort}${durStr}" -ForegroundColor Green
}

# ---------------------------------------------------------------- 状態読み込み
$activeWeekFile = Join-Path $WeeksDir "active_week.txt"
$weekId   = $null
$manifest = $null
$state    = $null
$weekDir  = $null

if (Test-Path $activeWeekFile) {
    $weekId  = [System.IO.File]::ReadAllText($activeWeekFile).Trim()
    $weekDir = Join-Path $WeeksDir $weekId

    $manifestPath = Join-Path $weekDir "manifest.json"
    $statePath    = Join-Path $weekDir "state.json"

    if (Test-Path $manifestPath) {
        $manifest = [System.IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
    }
    if (Test-Path $statePath) {
        $state = [System.IO.File]::ReadAllText($statePath) | ConvertFrom-Json
    } else {
        $state = [PSCustomObject]@{ week = $weekId; current_day = 1; last_advanced = $null }
    }
}

# ---------------------------------------------------------------- モデルパス決定
$today       = (Get-Date -Format "yyyy-MM-dd")
$modelPath   = $null
$dayLabel    = $null
$willAdvance = $false

if ($null -eq $weekId -or $null -eq $manifest) {
    Write-Host ""
    Write-Host "WARN: active_week.txt or manifest.json not found. Falling back to sac_final.zip." -ForegroundColor Yellow
    Write-Host "      Run tools\curate_week.ps1 first to set up weekly curation." -ForegroundColor Yellow
    if (-not (Test-Path $FinalModelPath)) {
        Write-Host "ERROR: $FinalModelPath not found." -ForegroundColor Red; exit 1
    }
    $modelPath   = (Resolve-Path $FinalModelPath).Path
    $dayLabel    = "fallback  sac_final.zip  (weeks/ not configured)"
    $willAdvance = $false
} else {
    $currentDay = [int]$state.current_day

    if ($currentDay -le 5) {
        $stepFile = "step_{0:D2}.zip" -f $currentDay
        $stepPath = Join-Path $weekDir $stepFile
        if (Test-Path $stepPath) {
            $modelPath = (Resolve-Path $stepPath).Path
        } else {
            Write-Host "WARN: $stepPath not found, falling back to sac_final.zip" -ForegroundColor Yellow
            $modelPath = (Resolve-Path $FinalModelPath).Path
        }
        $dayLabel = "day $currentDay / $weekId / $stepFile"

        $alreadyToday = ($null -ne $state.last_advanced -and $state.last_advanced -eq $today)
        if ($alreadyToday) {
            Write-Host "  (already advanced today $today - restarting server only)" -ForegroundColor DarkGray
            $willAdvance = $false
        } else {
            $willAdvance = (-not $NoAdvance)
        }
    } else {
        $step5 = Join-Path $weekDir "step_05.zip"
        $modelPath = if (Test-Path $step5) { (Resolve-Path $step5).Path } else { (Resolve-Path $FinalModelPath).Path }
        $dayLabel  = "fixed final (day $currentDay > 5) / $(Split-Path $modelPath -Leaf)"
        $willAdvance = $false
    }
}

# ---------------------------------------------------------------- 表示
Write-Host ""
Write-Host "=== advance_day ===" -ForegroundColor Cyan
Write-Host "  week    : $(if ($weekId) { $weekId } else { '(none)' })"
Write-Host "  today   : $today"
Write-Host "  model   : $dayLabel"
Write-Host "  path    : $modelPath"
Write-Host "  duration: $(if ($DurationSeconds -gt 0) { "${DurationSeconds}s (--duration passed to ai_server)" } else { 'unlimited (no --duration)' })"
if ($willAdvance -and $null -ne $state) {
    Write-Host "  next    : current_day $([int]$state.current_day) -> $([int]$state.current_day + 1)"
}
if ($DryRun) {
    Write-Host ""
    Write-Host "  [DryRun] skipping ai_server restart and state.json update." -ForegroundColor Yellow
    Write-Host ""
    exit 0
}

# ---------------------------------------------------------------- ai_server 再起動
Stop-AiServer
Start-AiServer -ModelPath $modelPath -Duration $DurationSeconds

# ---------------------------------------------------------------- state.json 更新
if ($null -ne $state -and $null -ne $weekDir) {
    $nextDay = if ($willAdvance) { [int]$state.current_day + 1 } else { [int]$state.current_day }
    $newState = [ordered]@{
        week          = $weekId
        current_day   = $nextDay
        last_advanced = $today
    }
    $statePath = Join-Path $weekDir "state.json"
    [System.IO.File]::WriteAllText(
        $statePath,
        ($newState | ConvertTo-Json),
        [System.Text.UTF8Encoding]::new($false)
    )
    if ($willAdvance) {
        Write-Host ("  state.json: current_day {0} -> {1}" -f [int]$state.current_day, $nextDay) -ForegroundColor DarkGray
    }
}

Write-Host ""

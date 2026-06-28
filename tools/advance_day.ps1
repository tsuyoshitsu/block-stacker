# tools/advance_day.ps1
#
# Daily model step-up (fresh/played 方式):
#   fresh/ の最古 checkpoint で ai_server を起動し、advance_state.json に記録する。
#   次回呼び出し時に前回のモデルを played/ へ移動してから次のモデルへ切り替える。
#   fresh/ が空になったら played/ の最大ステップ checkpoint を繰り返し再生する。
#
# 【設計ポイント: 非ブロッキング】
#   ai_server は --duration で自己終了するが、スクリプト自体は ai_server の終了を待たずに返す。
#   モデルの played/ 移動は「次回呼び出し」のタイミングで実施する（前回のログを参照）。
#   タスクスケジューラから毎日 1 回呼べばよい。
#
# 使い方:
#   tools\advance_day.ps1 -DurationSeconds 86400   # 1日(24h) 後に ai_server を自動終了
#   tools\advance_day.ps1 -DurationSeconds 3600    # 1時間後に自動終了（テスト用）
#   tools\advance_day.ps1 -DryRun                  # 表示のみ（ai_server 起動・移動なし）
#
# パラメータ:
#   -DurationSeconds <int> : ai_server に渡す --duration 秒数（0=無制限）。日次運用は 86400。
#   -DryRun                : 表示のみ。ai_server 起動も played/ 移動もしない。

param(
    [string]$FreshDir         = "output\mvp2\fresh",
    [string]$PlayedDir        = "output\mvp2\played",
    [string]$StateFile        = "output\mvp2\advance_state.json",
    [string]$Python           = ".venv\Scripts\python.exe",
    [string]$AiHost           = "127.0.0.1",
    [int]$AiPort              = 8765,
    [int]$DurationSeconds     = 0,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- ai_server 操作
function Stop-AiServer {
    $procs = @(
        Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
            Where-Object { $_.CommandLine -and $_.CommandLine -match "block_stacker.serving.ai_server" }
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
    $argList = @(
        "-m", "block_stacker.serving.ai_server",
        "--model", $ModelPath,
        "--host", $AiHost,
        "--port", $AiPort
    )
    if ($Duration -gt 0) { $argList += @("--duration", $Duration) }
    $proc = Start-Process -FilePath $Python -ArgumentList $argList -PassThru -WindowStyle Hidden
    Start-Sleep 3
    if ($proc.HasExited) {
        Write-Host "ERROR: ai_server exited immediately (code=$($proc.ExitCode))" -ForegroundColor Red
        exit 1
    }
    $durStr = if ($Duration -gt 0) { "  duration=${Duration}s" } else { "" }
    Write-Host "  ai_server PID $($proc.Id) started  ->  ws://${AiHost}:${AiPort}${durStr}" -ForegroundColor Green
    return $proc.Id
}

# ---------------------------------------------------------------- checkpoint 列挙
# 新形式: sac_YYYYMMDD-HHMMSS_<steps>_steps.zip  → RunTs + Steps
# 旧形式: sac_<steps>_steps.zip (後方互換)        → RunTs = "00000000-000000" + Steps
# ソートキー: (RunTs, Steps) 昇順。-Descending で降順(最新run・最大steps が[0])。
function Get-CheckpointsSorted {
    param([string]$Dir, [switch]$Descending)
    if (-not (Test-Path $Dir)) { return @() }
    $items = @(
        Get-ChildItem $Dir -Filter "sac_*.zip" |
            ForEach-Object {
                $ts = $null; $steps = $null
                if ($_.Name -match "^sac_(\d{8}-\d{6})_(\d+)_steps\.zip$") {
                    $ts = $Matches[1]; $steps = [int]$Matches[2]
                } elseif ($_.Name -match "^sac_(\d+)_steps\.zip$") {
                    $ts = "00000000-000000"; $steps = [int]$Matches[1]
                }
                if ($null -ne $ts) {
                    [PSCustomObject]@{ RunTs = $ts; Steps = $steps; FullName = $_.FullName }
                }
            }
    )
    if ($Descending) { return @($items | Sort-Object RunTs, Steps -Descending) }
    return @($items | Sort-Object RunTs, Steps)
}

# ================================================================ main

Write-Host ""
Write-Host "=== advance_day ===" -ForegroundColor Cyan

# ---- 前回のモデルを played/ へ移動 ----
$prevState = $null
if (Test-Path $StateFile) {
    try {
        $prevState = [System.IO.File]::ReadAllText($StateFile, [System.Text.UTF8Encoding]::new($false)) | ConvertFrom-Json
    } catch {
        Write-Host "  WARN: advance_state.json 読み込み失敗: $_" -ForegroundColor Yellow
    }
}
if ($prevState -and $prevState.from_fresh -and $prevState.model) {
    $prevModel = $prevState.model
    if (Test-Path $prevModel) {
        if (-not $DryRun) {
            if (-not (Test-Path $PlayedDir)) { New-Item -ItemType Directory -Force -Path $PlayedDir | Out-Null }
            $dest = Join-Path $PlayedDir (Split-Path $prevModel -Leaf)
            Move-Item -Path $prevModel -Destination $dest -Force
            Write-Host "  moved to played/: $(Split-Path $prevModel -Leaf)" -ForegroundColor DarkGray
        } else {
            Write-Host "  [DryRun] would move to played/: $(Split-Path $prevModel -Leaf)" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  (prev model already moved or missing: $(Split-Path $prevModel -Leaf))" -ForegroundColor DarkGray
    }
}

# ---- 次に再生するモデルを決定 ----
$freshModels  = @(Get-CheckpointsSorted -Dir $FreshDir)
$playedModels = @(Get-CheckpointsSorted -Dir $PlayedDir -Descending)
$fromFresh    = $false
$modelPath    = $null
$modeLabel    = $null

if ($freshModels.Count -gt 0) {
    $modelPath = $freshModels[0].FullName
    $fromFresh = $true
    $modeLabel = "fresh (oldest: $($freshModels[0].RunTs) $($freshModels[0].Steps) steps, $($freshModels.Count) remaining)"
} elseif ($playedModels.Count -gt 0) {
    $modelPath = $playedModels[0].FullName
    $fromFresh = $false
    $modeLabel = "repeat (fresh/ empty; max played: $($playedModels[0].RunTs) $($playedModels[0].Steps) steps)"
} else {
    Write-Host ""
    Write-Host "ERROR: fresh/ and played/ are both empty. Run training first:" -ForegroundColor Red
    Write-Host "  .venv\Scripts\python.exe -m block_stacker.training.train --n-envs 6 --total-timesteps 4000" -ForegroundColor Yellow
    exit 1
}

Write-Host "  mode    : $modeLabel"
Write-Host "  model   : $modelPath"
Write-Host "  duration: $(if ($DurationSeconds -gt 0) { "${DurationSeconds}s" } else { 'unlimited' })"

if ($DryRun) {
    Write-Host ""
    Write-Host "  [DryRun] skipping ai_server restart and state update." -ForegroundColor Yellow
    Write-Host ""
    exit 0
}

# ---- ai_server 再起動 ----
Stop-AiServer
$pid = Start-AiServer -ModelPath $modelPath -Duration $DurationSeconds

# ---- advance_state.json 更新 ----
$state = [ordered]@{
    model      = $modelPath
    from_fresh = $fromFresh
    started_at = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz")
    server_pid = $pid
    duration_s = $DurationSeconds
}
[System.IO.File]::WriteAllText(
    $StateFile,
    ($state | ConvertTo-Json),
    [System.Text.UTF8Encoding]::new($false)
)
Write-Host "  advance_state.json: updated" -ForegroundColor DarkGray

Write-Host ""
Write-Host "Done." -ForegroundColor Green

# tools/local_loop.ps1
#
# ローカル成長1巡再生: fresh/ の checkpoint を古い→新しい順に -SwitchSeconds 秒ずつ再生し、
# 最後のモデルの再生が終わったら正常終了する（ループなし）。
#
# 用途: ローカル環境で AI の成長過程を1周観察する。
#       日次運用(advance_day.ps1)と違い played/ への移動はしない（読み取り専用）。
#
# 使い方:
#   tools\local_loop.ps1                       # fresh/ を 60 秒ずつ1巡して終了
#   tools\local_loop.ps1 -SwitchSeconds 30     # 30 秒ごとに切り替え
#   tools\local_loop.ps1 -Dir output\mvp2\played  # played/ を指定
#
# 終了: 最後のモデルの再生が終わると自動終了（Ctrl+C で途中中断も可）
#
# パラメータ:
#   -SwitchSeconds <int> : 1 モデルあたりの再生秒数（既定 60）
#   -Dir <path>          : checkpoint ディレクトリ（既定 fresh/）

param(
    [string]$Dir            = "output\mvp2\fresh",
    [int]$SwitchSeconds     = 60,
    [string]$Python         = ".venv\Scripts\python.exe",
    [string]$AiHost         = "127.0.0.1",
    [int]$AiPort            = 8765
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- helpers
# 新形式: sac_YYYYMMDD-HHMMSS_<steps>_steps.zip  → RunTs + Steps
# 旧形式: sac_<steps>_steps.zip (後方互換)        → RunTs = "00000000-000000" + Steps
# ソートキー: (RunTs, Steps) 昇順 = 古い run から順に、同 run 内はステップ昇順。
function Get-CheckpointsSorted {
    param([string]$D)
    if (-not (Test-Path $D)) { return @() }
    @(
        Get-ChildItem $D -Filter "sac_*.zip" |
            ForEach-Object {
                $ts = $null; $steps = $null
                if ($_.Name -match "^sac_(\d{8}-\d{6})_(\d+)_steps\.zip$") {
                    $ts = $Matches[1]; $steps = [int]$Matches[2]
                } elseif ($_.Name -match "^sac_(\d+)_steps\.zip$") {
                    $ts = "00000000-000000"; $steps = [int]$Matches[1]
                }
                if ($null -ne $ts) {
                    [PSCustomObject]@{ RunTs = $ts; Steps = $steps; FullName = $_.FullName; Name = $_.Name }
                }
            } | Sort-Object RunTs, Steps
    )
}

function Stop-AiServer {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match "block_stacker.serving.ai_server" } |
        ForEach-Object {
            Write-Host "  stopping ai_server PID $($_.ProcessId)" -ForegroundColor DarkGray
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

function Start-AiServer {
    param([string]$ModelPath, [int]$Duration)
    $proc = Start-Process -FilePath $Python `
        -ArgumentList @(
            "-m", "block_stacker.serving.ai_server",
            "--model", $ModelPath,
            "--host", $AiHost, "--port", $AiPort,
            "--duration", $Duration
        ) -PassThru -WindowStyle Hidden
    Start-Sleep 2
    if ($proc.HasExited) {
        Write-Host "  WARN: ai_server exited immediately (code=$($proc.ExitCode))" -ForegroundColor Yellow
        return $null
    }
    return $proc
}

# ================================================================ main

$models = @(Get-CheckpointsSorted -D $Dir)
if ($models.Count -eq 0) {
    Write-Host ""
    Write-Host "ERROR: no checkpoints found in $Dir" -ForegroundColor Red
    Write-Host "  Run training first:" -ForegroundColor Yellow
    Write-Host "  .venv\Scripts\python.exe -m block_stacker.training.train --n-envs 6 --total-timesteps 4000" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "=== local_loop ===" -ForegroundColor Cyan
Write-Host "  dir    : $Dir"
Write-Host "  models : $($models.Count) checkpoints"
$models | ForEach-Object { Write-Host "    $($_.RunTs) $($_.Steps) steps -> $($_.Name)" -ForegroundColor DarkGray }
Write-Host "  switch : every ${SwitchSeconds}s"
Write-Host "  loop   : 1 pass then exit"
Write-Host ""

Stop-AiServer

try {
    foreach ($m in $models) {
        Write-Host "  [$($m.RunTs) $($m.Steps) steps] $($m.Name) for ${SwitchSeconds}s" -ForegroundColor Green
        $proc = Start-AiServer -ModelPath $m.FullName -Duration $SwitchSeconds
        if ($null -ne $proc) {
            $proc.WaitForExit(($SwitchSeconds + 10) * 1000) | Out-Null
            if (-not $proc.HasExited) {
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            }
        } else {
            Start-Sleep $SwitchSeconds
        }
        Start-Sleep 1
    }
} finally {
    Stop-AiServer
}

Write-Host ""
Write-Host "local_loop 完了: $($models.Count) モデルを再生して終了" -ForegroundColor Cyan

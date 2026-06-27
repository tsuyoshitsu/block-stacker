# tools/local_loop.ps1
#
# ローカル成長ループ: fresh/ の checkpoint を古い→新しい順に -SwitchSeconds 秒ずつ循環再生する。
#
# 用途: ローカル環境で AI の成長過程を繰り返し観察する。
#       日次運用(advance_day.ps1)と違い played/ への移動はしない（読み取り専用）。
#
# 使い方:
#   tools\local_loop.ps1                       # fresh/ を 60 秒ずつ無限ループ
#   tools\local_loop.ps1 -SwitchSeconds 30     # 30 秒ごとに切り替え
#   tools\local_loop.ps1 -Dir output\mvp2\played  # played/ を指定
#   tools\local_loop.ps1 -SwitchSeconds 5 -MaxCycles 3  # 3 サイクルで終了（テスト用）
#
# 終了: Ctrl+C または -MaxCycles 指定
#
# パラメータ:
#   -SwitchSeconds <int> : 1 モデルあたりの再生秒数（既定 60）
#   -Dir <path>          : checkpoint ディレクトリ（既定 fresh/）
#   -MaxCycles <int>     : 0 = 無限ループ（既定）。N を指定すると N サイクルで終了

param(
    [string]$Dir            = "output\mvp2\fresh",
    [int]$SwitchSeconds     = 60,
    [int]$MaxCycles         = 0,
    [string]$Python         = ".venv\Scripts\python.exe",
    [string]$AiHost         = "127.0.0.1",
    [int]$AiPort            = 8765
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- helpers
function Get-CheckpointsSorted {
    param([string]$D)
    if (-not (Test-Path $D)) { return @() }
    @(
        Get-ChildItem $D -Filter "sac_*_steps.zip" |
            ForEach-Object {
                if ($_.Name -match "^sac_(\d+)_steps\.zip$") {
                    [PSCustomObject]@{ Steps = [int]$Matches[1]; FullName = $_.FullName; Name = $_.Name }
                }
            } | Sort-Object Steps
    )
}

function Stop-AiServer {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match "block_stacker\.mvp3\.ai_server" } |
        ForEach-Object {
            Write-Host "  stopping ai_server PID $($_.ProcessId)" -ForegroundColor DarkGray
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

function Start-AiServer {
    param([string]$ModelPath, [int]$Duration)
    $proc = Start-Process -FilePath $Python `
        -ArgumentList @(
            "-m", "block_stacker.mvp3.ai_server",
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
    Write-Host "  .venv\Scripts\python.exe -m block_stacker.mvp2.train --n-envs 6 --total-timesteps 4000" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "=== local_loop ===" -ForegroundColor Cyan
Write-Host "  dir       : $Dir"
Write-Host "  models    : $($models.Count) checkpoints"
$models | ForEach-Object { Write-Host "    $($_.Steps) steps -> $($_.Name)" -ForegroundColor DarkGray }
Write-Host "  switch    : every ${SwitchSeconds}s"
Write-Host "  cycles    : $(if ($MaxCycles -gt 0) { $MaxCycles } else { 'infinite (Ctrl+C to stop)' })"
Write-Host ""

Stop-AiServer

$cycle    = 0
$running  = $true

try {
    while ($running) {
        $cycle++
        Write-Host "--- Cycle $cycle ---" -ForegroundColor Cyan

        foreach ($m in $models) {
            Write-Host "  [$($m.Steps) steps] $($m.Name) for ${SwitchSeconds}s" -ForegroundColor Green
            $proc = Start-AiServer -ModelPath $m.FullName -Duration $SwitchSeconds
            if ($null -ne $proc) {
                # ai_server は --duration で自己終了する。WaitForExit で同期
                $proc.WaitForExit(($SwitchSeconds + 10) * 1000) | Out-Null
                if (-not $proc.HasExited) {
                    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                }
            } else {
                # 起動失敗のとき SwitchSeconds 待って次へ
                Start-Sleep $SwitchSeconds
            }
            Start-Sleep 1
        }

        if ($MaxCycles -gt 0 -and $cycle -ge $MaxCycles) {
            $running = $false
        }
    }
} finally {
    Stop-AiServer
    Write-Host ""
    Write-Host "local_loop 終了 (${cycle} サイクル完了)" -ForegroundColor Cyan
}

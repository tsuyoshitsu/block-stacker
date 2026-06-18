# tools/demo_checkpoints.ps1
#
# ローカル学習で生成された checkpoint を一つずつデモ実行して、AI の「成長」を観察するヘルパー。
#
# 使い方:
#   tools/demo_checkpoints.ps1                              # 対話モード（一つ選んで再生）
#   tools/demo_checkpoints.ps1 -Mode auto                   # 全 checkpoint を順番に再生
#   tools/demo_checkpoints.ps1 -Mode auto -Seconds 60       # 各 30 秒ずつ
#   tools/demo_checkpoints.ps1 -CheckpointsDir output/mvp2/checkpoints
#
# 前提:
#   - .venv が学習依存をインストール済み (pip install -e .)
#   - learner が output/mvp2/checkpoints/sac_<steps>_steps.zip を生成済み
#   - Godot エディタは別途起動して main.tscn を再生 (またはスクリプトが起動)
#
# 設計上のポイント（日本語レビューノート）:
#   - checkpoint を 'sac_<steps>_steps.zip' から抽出。<steps> は学習を通して連続した総タイム
#     ステップ（ステージ別ではない 1 本の系列）なので、steps 順に並べると学習順になる。
#   - 再生は ai_server が「常に最終ステージの世界」で行う（既定で stages[-1] を使う）。
#     どの段階の checkpoint でも最終ステージ環境で成長を見られる。
#   - ai_server は子プロセスとして起動、PID 管理して時間後に Stop-Process
#   - チェックポイント切替時に Godot 側は WebSocket 切断 → 再接続を自動でこなす
#     （WsClient.cs の AutoReconnectSeconds=2 で 2 秒以内に新サーバへ繋がる）
#   - PowerShell 起動・停止のタイミングを揃えるため、Start-Sleep で待つ

param(
    [string]$CheckpointsDir = "output\mvp2\checkpoints",
    [int]$Seconds = 60,
    [ValidateSet("interactive", "auto")]
    [string]$Mode = "interactive",
    [string]$Python = ".venv\Scripts\python.exe",
    [string]$Godot = "D:\Godot_v4.4.1-stable_mono_win64\Godot_v4.4.1-stable_mono_win64.exe",
    [switch]$LaunchGodot
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- helpers

function Get-Checkpoints {
    param([string]$Dir)
    if (-not (Test-Path $Dir)) {
        Write-Host "checkpoints ディレクトリが見つかりません: $Dir" -ForegroundColor Red
        Write-Host "先に学習を回してください:" -ForegroundColor Yellow
        Write-Host "  .venv\Scripts\python.exe -m block_stacker.mvp2.train --n-envs 6 --total-timesteps 100000" -ForegroundColor Yellow
        exit 1
    }
    # checkpoint を拾う（sac_<steps>_steps.zip）。ステージ別ではなく、学習を通して
    # 連続したステップ数で記録される 1 本の系列。ステップ数でソートすれば学習順に並ぶ。
    # 再生は ai_server が常に最終ステージの世界で行う。
    Get-ChildItem $Dir -Filter "sac_*_steps.zip" |
        ForEach-Object {
            if ($_.Name -match "^sac_(\d+)_steps\.zip$") {
                [PSCustomObject]@{
                    Steps    = [int]$Matches[1]
                    Name     = $_.Name
                    FullName = $_.FullName
                }
            }
        } |
        Sort-Object Steps
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
    param([string]$ModelPath)
    $proc = Start-Process -FilePath $Python `
        -ArgumentList @(
            "-m", "block_stacker.mvp3.ai_server",
            "--model", $ModelPath,
            "--host", "127.0.0.1"
        ) -PassThru -WindowStyle Hidden
    return $proc
}

# ---------------------------------------------------------------- main

$checkpoints = @(Get-Checkpoints -Dir $CheckpointsDir)
if ($checkpoints.Count -eq 0) {
    Write-Host "$CheckpointsDir に checkpoint がありません。" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "発見された checkpoint ($($checkpoints.Count) 件):" -ForegroundColor Cyan
for ($i = 0; $i -lt $checkpoints.Count; $i++) {
    $ck = $checkpoints[$i]
    "{0,3}: {1,8} steps  ({2})" -f $i, $ck.Steps, $ck.Name | Write-Host
}

# 選択
$selected = @()
if ($Mode -eq "interactive") {
    Write-Host ""
    Write-Host "番号を入力 (例: 5)、'all' で全部、'q' で終了" -ForegroundColor Yellow
    $input = Read-Host
    if ($input -eq "q") { exit 0 }
    if ($input -eq "all") {
        $selected = $checkpoints
    } else {
        $idx = [int]$input
        if ($idx -lt 0 -or $idx -ge $checkpoints.Count) {
            Write-Host "範囲外: $idx" -ForegroundColor Red
            exit 1
        }
        $selected = @($checkpoints[$idx])
    }
} else {
    # auto: 全部順番に
    $selected = $checkpoints
}

# Godot 起動（オプション）
if ($LaunchGodot) {
    $godotProc = Get-Process -Name "Godot_v4.4.1*" -ErrorAction SilentlyContinue
    if (-not $godotProc) {
        Write-Host ""
        Write-Host "Godot を起動..." -ForegroundColor Yellow
        Start-Process -FilePath $Godot `
            -ArgumentList @("--path", "client", "res://scenes/main.tscn")
        Start-Sleep 6
    }
}

# 既存の ai_server を念のため止める
Stop-AiServer

# 各 checkpoint を再生
foreach ($ck in $selected) {
    Write-Host ""
    Write-Host "=== Step $($ck.Steps) ($($ck.Name)) ===" -ForegroundColor Green

    $proc = Start-AiServer -ModelPath $ck.FullName
    Write-Host "  ai_server PID $($proc.Id)、 $Seconds 秒間再生..." -ForegroundColor DarkGray

    # PID が即死していないか確認
    Start-Sleep 2
    if ($proc.HasExited) {
        Write-Host "  ai_server が起動直後に終了しました（exit=$($proc.ExitCode)）。スキップ" -ForegroundColor Red
        continue
    }

    Start-Sleep ($Seconds - 2)

    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    # WebSocket が完全に閉じるのを待つ
    Start-Sleep 1
}

Write-Host ""
Write-Host "完了。Godot は起動したままなので、必要なら手で閉じてください。" -ForegroundColor Cyan

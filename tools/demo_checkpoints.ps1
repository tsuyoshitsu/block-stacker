# tools/demo_checkpoints.ps1
#
# ローカル学習で生成された checkpoint を一つずつデモ実行して、AI の「成長」を観察するヘルパー。
# ※ 日次/ループ自動化は advance_day.ps1 / local_loop.ps1 を使う。このスクリプトは開発用手動確認。
#
# 使い方:
#   tools/demo_checkpoints.ps1                                     # 対話モード（一つ選んで再生）
#   tools/demo_checkpoints.ps1 -Mode auto                          # 全 checkpoint を順番に再生
#   tools/demo_checkpoints.ps1 -Mode auto -Seconds 60              # 各 60 秒ずつ
#   tools/demo_checkpoints.ps1 -CheckpointsDir output\training\fresh   # fresh/ を明示（既定）
#   tools/demo_checkpoints.ps1 -CheckpointsDir output\training\played  # played/ を再生
#
# 前提:
#   - .venv が学習依存をインストール済み (pip install -e .)
#   - learner が output/training/fresh/sac_<steps>_steps.zip を生成済み
#   - Godot エディタは別途起動して main.tscn を再生 (またはスクリプトが起動)
#
# 設計上のポイント（日本語レビューノート）:
#   - checkpoint を 'sac_YYYYMMDD-HHMMSS_<steps>_steps.zip' から抽出。
#     同一 run の 5 本は同じ YYYYMMDD-HHMMSS を共有。(RunTs, Steps) 昇順が学習順。
#   - 再生は ai_server が「常に最終ステージの世界」で行う（既定で stages[-1] を使う）。
#     どの段階の checkpoint でも最終ステージ環境で成長を見られる。
#   - ai_server は子プロセスとして起動、PID 管理して時間後に Stop-Process
#   - チェックポイント切替時に Godot 側は WebSocket 切断 → 再接続を自動でこなす
#     （WsClient.cs の AutoReconnectSeconds=2 で 2 秒以内に新サーバへ繋がる）
#   - PowerShell 起動・停止のタイミングを揃えるため、Start-Sleep で待つ

param(
    [string]$CheckpointsDir = "output\training\fresh",
    [int]$Seconds = 60,
    [ValidateSet("interactive", "auto")]
    [string]$Mode = "interactive",
    [string]$Python = ".venv\Scripts\python.exe",
    [string]$Godot = "D:\Godot_v4.4.1-stable_mono_win64\Godot_v4.4.1-stable_mono_win64.exe",
    [switch]$LaunchGodot
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- helpers

# 新形式: sac_YYYYMMDD-HHMMSS_<steps>_steps.zip  → RunTs + Steps
# 旧形式: sac_<steps>_steps.zip (後方互換)        → RunTs = "00000000-000000" + Steps
# ソートキー: (RunTs, Steps) 昇順 = 成長順（古い run → 新しい run、同 run 内はステップ昇順）。
function Get-Checkpoints {
    param([string]$Dir)
    if (-not (Test-Path $Dir)) {
        Write-Host "checkpoint ディレクトリが見つかりません: $Dir" -ForegroundColor Red
        Write-Host "先に学習を回してください:" -ForegroundColor Yellow
        Write-Host "  .venv\Scripts\python.exe -m block_stacker.training.train --n-envs 6 --total-timesteps 4000" -ForegroundColor Yellow
        exit 1
    }
    Get-ChildItem $Dir -Filter "sac_*.zip" |
        ForEach-Object {
            $ts = $null; $steps = $null
            if ($_.Name -match "^sac_(\d{8}-\d{6})_(\d+)_steps\.zip$") {
                $ts = $Matches[1]; $steps = [int]$Matches[2]
            } elseif ($_.Name -match "^sac_(\d+)_steps\.zip$") {
                $ts = "00000000-000000"; $steps = [int]$Matches[1]
            }
            if ($null -ne $ts) {
                $label = if ($ts -ne "00000000-000000") { "$ts / $steps steps" } else { "$steps steps (legacy)" }
                [PSCustomObject]@{
                    RunTs    = $ts
                    Steps    = $steps
                    Name     = $_.Name
                    FullName = $_.FullName
                    Label    = $label
                }
            }
        } |
        Sort-Object RunTs, Steps
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
    param([string]$ModelPath)
    $proc = Start-Process -FilePath $Python `
        -ArgumentList @(
            "-m", "block_stacker.serving.ai_server",
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
    "{0,3}: {1,-15}  ({2})" -f $i, $ck.Label, $ck.Name | Write-Host
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
    Write-Host "=== $($ck.Label) ($($ck.Name)) ===" -ForegroundColor Green

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

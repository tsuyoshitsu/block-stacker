# tools/replay_checkpoints.ps1
#
# 蓄積した checkpoint を古い順に ai_server で再生し、AI の「成長」を目で追うヘルパー。
# demo_checkpoints.ps1（対話選択）と local_loop.ps1（1巡自動再生）を統合したもの。
#
# checkpoint はどこから増えるか:
#   - train: 1 run につきプリセット 1 本を fresh/ に保存し、次の run 開始時に played/ へ退避する。
#   - live_server: **セッション終了ごとに** fresh/ へ 1 本追加する（played/ へは移さない）。
#     平日運用なら fresh/ に月 20 本前後の成長系列が溜まるので、それをこれで通し再生する。
#
# 使い方:
#   tools\replay_checkpoints.ps1                        # 一覧から 1 本選んで再生（対話）
#   tools\replay_checkpoints.ps1 -All                   # 全部を古い順に 1 巡して終了
#   tools\replay_checkpoints.ps1 -All -Seconds 30       # 各 30 秒ずつ
#   tools\replay_checkpoints.ps1 -Dir output\training\played   # played/ を再生
#   tools\replay_checkpoints.ps1 -All -LaunchGodot      # Godot も自動起動
#
# 前提:
#   - .venv に学習依存がインストール済み（pip install -e .）
#   - Godot クライアントで見る場合は main.tscn を再生しておく（-LaunchGodot でも可）
#
# 設計上のポイント:
#   - ソートキーは (RunTs, Steps) 昇順 = 学習順。ファイル名 sac_<YYYYMMDD-HHMMSS>_<steps>_steps.zip
#     から抽出する。旧形式 sac_<steps>_steps.zip は RunTs を番兵にして先頭へ寄せる。
#   - 再生は ai_server が**常に最終ステージの世界**で行う。どの段階の checkpoint でも
#     同じ難易度の世界に置くので、成長の比較になる。
#   - ai_server には --duration を渡して自己終了させ、WaitForExit で待つ。取りこぼした場合だけ
#     Stop-Process で落とす（PID 直叩きのみに頼らない）。
#   - 切替時に Godot 側は WebSocket 切断 → 再接続を自動でこなす
#     （WsClient.cs の AutoReconnectSeconds=2 で 2 秒以内に新サーバへ繋がる）。

param(
    [string]$Dir      = "output\training\fresh",
    [int]$Seconds     = 60,
    [switch]$All,
    [string]$Python   = ".venv\Scripts\python.exe",
    [string]$AiHost   = "127.0.0.1",
    [int]$AiPort      = 8765,
    [string]$Godot    = "D:\Godot_v4.4.1-stable_mono_win64\Godot_v4.4.1-stable_mono_win64.exe",
    [switch]$LaunchGodot
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- helpers

# 新形式: sac_YYYYMMDD-HHMMSS_<steps>_steps.zip → RunTs + Steps
# 旧形式: sac_<steps>_steps.zip (後方互換)      → RunTs = "00000000-000000" + Steps
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
                    $label = if ($ts -ne "00000000-000000") { "$ts / $steps steps" }
                             else { "$steps steps (legacy)" }
                    [PSCustomObject]@{
                        RunTs    = $ts
                        Steps    = $steps
                        Name     = $_.Name
                        FullName = $_.FullName
                        Label    = $label
                    }
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
        Write-Host "  WARN: ai_server が起動直後に終了 (exit=$($proc.ExitCode))" -ForegroundColor Yellow
        return $null
    }
    return $proc
}

# ================================================================ main

$checkpoints = @(Get-CheckpointsSorted -D $Dir)
if ($checkpoints.Count -eq 0) {
    Write-Host ""
    Write-Host "ERROR: $Dir に checkpoint がありません。" -ForegroundColor Red
    Write-Host "  先に学習を回してください:" -ForegroundColor Yellow
    Write-Host "  .venv\Scripts\python.exe -m block_stacker.training.train                              # プリセット生成 (Stage 3 のみ・5,000 steps)" -ForegroundColor Yellow
    Write-Host "  .venv\Scripts\python.exe -m block_stacker.training.train --start-stage 1 --target-stage 4   # フルカリキュラム" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "=== replay_checkpoints ===" -ForegroundColor Cyan
Write-Host "  dir    : $Dir"
Write-Host "  found  : $($checkpoints.Count) checkpoints（古い順）"
for ($i = 0; $i -lt $checkpoints.Count; $i++) {
    "{0,3}: {1,-28}  ({2})" -f $i, $checkpoints[$i].Label, $checkpoints[$i].Name | Write-Host
}

# ---- 再生対象の決定 ----
if ($All) {
    $selected = $checkpoints
} else {
    Write-Host ""
    Write-Host "番号を入力 (例: 5)、'all' で全部、'q' で終了" -ForegroundColor Yellow
    $answer = Read-Host
    if ($answer -eq "q") { exit 0 }
    if ($answer -eq "all") {
        $selected = $checkpoints
    } else {
        $idx = 0
        if (-not [int]::TryParse($answer, [ref]$idx) -or $idx -lt 0 -or $idx -ge $checkpoints.Count) {
            Write-Host "範囲外または数値でない入力: '$answer'" -ForegroundColor Red
            exit 1
        }
        $selected = @($checkpoints[$idx])
    }
}

Write-Host ""
Write-Host "  replay : $($selected.Count) モデル × ${Seconds}s（1 巡して終了）" -ForegroundColor Cyan
Write-Host ""

# ---- Godot 起動（任意）----
if ($LaunchGodot) {
    if (-not (Get-Process -Name "Godot_v4.4.1*" -ErrorAction SilentlyContinue)) {
        Write-Host "Godot を起動..." -ForegroundColor Yellow
        Start-Process -FilePath $Godot -ArgumentList @("--path", "client", "res://scenes/main.tscn")
        Start-Sleep 6
    }
}

Stop-AiServer   # 前回の残骸があれば片付ける

try {
    foreach ($ck in $selected) {
        Write-Host "  [$($ck.Label)] $($ck.Name) for ${Seconds}s" -ForegroundColor Green
        $proc = Start-AiServer -ModelPath $ck.FullName -Duration $Seconds
        if ($null -ne $proc) {
            $proc.WaitForExit(($Seconds + 10) * 1000) | Out-Null
            if (-not $proc.HasExited) {
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            }
        } else {
            Start-Sleep $Seconds
        }
        Start-Sleep 1   # WebSocket が完全に閉じるのを待つ
    }
} finally {
    Stop-AiServer
}

Write-Host ""
Write-Host "完了: $($selected.Count) モデルを再生しました。" -ForegroundColor Cyan

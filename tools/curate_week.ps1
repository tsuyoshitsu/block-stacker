# tools/curate_week.ps1
#
# Weekly model curation: train.py が生成した checkpoint をそのまま採用し
# output/weeks/<YYYY-WNN>/ に配置して manifest.json / state.json / active_week.txt を生成する。
#
# 【選出方針】
#   train.py は total_timesteps を checkpoint_splits(=5) 等分した地点で 5 本の checkpoint を生成する。
#   curate_week は生成された checkpoint をそのまま採用する（再分割・等間隔選出は行わない）。
#   - 5 本以下: そのまま全部採用（不足分は sac_final.zip でパディング）
#   - 5 本超  : 最新の 5 本（ステップ数が最大の方から 5 本）を採用
#               （--resume / カリキュラム卒業で checkpoint が混在・増加した場合の安全策）
#
# 使い方:
#   tools\curate_week.ps1
#   tools\curate_week.ps1 -Force
#   tools\curate_week.ps1 -WeekOverride 2026-W27 -Force
#   tools\curate_week.ps1 -MaxSteps 50000          # 5万ステップ以下のcheckpointだけを対象にする
#
# パラメータ:
#   -MaxSteps <int>   : 0 = 上限なし（既定）。指定するとステップ数が MaxSteps 以下の
#                       checkpoint だけを採用対象にする。
#                       ちょうど MaxSteps のファイルが無くても、それ以下で最大のものが
#                       step_05 に入る（自動追従）。
#                       例: -MaxSteps 50000 → 5万ステップ以下から採用
#
# ワークフロー:
#   日曜 学習終了後 -> tools\curate_week.ps1 -> active_week.txt が更新される
#   月-金 起動時   -> tools\advance_day.ps1  -> 今日の step で ai_server 起動

param(
    [string]$CheckpointsDir = "output\mvp2\checkpoints",
    [string]$WeeksDir       = "output\weeks",
    [string]$FinalModelPath = "output\mvp2\sac_final.zip",
    [string]$WeekOverride   = "",
    [int]$MaxSteps          = 0,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- ISO 8601 week 計算
# 木曜日の属する年・週番号が ISO 週番号を決める（1/4 は常に第 1 週）
function Get-ISOWeekId {
    param([datetime]$Date = (Get-Date))
    $dow        = [int]$Date.DayOfWeek
    $isoDow     = if ($dow -eq 0) { 7 } else { $dow }
    $thursday   = $Date.AddDays(4 - $isoDow)
    $year       = $thursday.Year
    $jan4       = [datetime]::new($year, 1, 4)
    $jan4Dow    = [int]$jan4.DayOfWeek
    $jan4IsoDow = if ($jan4Dow -eq 0) { 7 } else { $jan4Dow }
    $week1Mon   = $jan4.AddDays(1 - $jan4IsoDow)
    $weekNum    = [int][Math]::Floor(($thursday - $week1Mon).TotalDays / 7) + 1
    return "$year-W$($weekNum.ToString('D2'))"
}

# ---------------------------------------------------------------- checkpoint 列挙
function Get-Checkpoints {
    param([string]$Dir)
    if (-not (Test-Path $Dir)) { return @() }
    $results = @(
        Get-ChildItem $Dir -Filter "sac_*_steps.zip" |
            ForEach-Object {
                if ($_.Name -match "^sac_(\d+)_steps\.zip$") {
                    [PSCustomObject]@{
                        Steps    = [int]$Matches[1]
                        Name     = $_.Name
                        FullName = $_.FullName
                    }
                }
            } | Sort-Object Steps
    )
    return $results
}

# ================================================================ main

$weekId = if ($WeekOverride -ne "") { $WeekOverride } else { Get-ISOWeekId }
Write-Host ""
Write-Host "=== curate_week ===" -ForegroundColor Cyan
Write-Host "  week : $weekId"

$weekDir = Join-Path $WeeksDir $weekId
if ((Test-Path $weekDir) -and (-not $Force)) {
    Write-Host ""
    Write-Host "WARN: $weekDir already exists. Use -Force to overwrite." -ForegroundColor Yellow
    exit 1
}
New-Item -ItemType Directory -Force -Path $weekDir  | Out-Null
New-Item -ItemType Directory -Force -Path $WeeksDir | Out-Null

# checkpoint 取得
$checkpoints = @(Get-Checkpoints -Dir $CheckpointsDir)
Write-Host "  checkpoints found : $($checkpoints.Count)  ($CheckpointsDir)"

if ($MaxSteps -gt 0) {
    $checkpoints = @($checkpoints | Where-Object { $_.Steps -le $MaxSteps })
    Write-Host "  MaxSteps filter   : <= $MaxSteps  ($($checkpoints.Count) remaining)"
}

if ($checkpoints.Count -eq 0) {
    Write-Host ""
    Write-Host "ERROR: No checkpoints found. Run training first:" -ForegroundColor Red
    Write-Host "  .venv\Scripts\python.exe -m block_stacker.mvp2.train --n-envs 6 --total-timesteps 4000" -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path $FinalModelPath)) {
    Write-Host "ERROR: $FinalModelPath not found." -ForegroundColor Red
    exit 1
}
$finalAbsPath = (Resolve-Path $FinalModelPath).Path

# 採用 checkpoint 決定（5 本以下はそのまま、5 本超は最新 5 本を採用）
$availableCount = $checkpoints.Count
if ($checkpoints.Count -gt 5) {
    $checkpoints = @($checkpoints[-5..-1])
    Write-Host ("  NOTE: {0} checkpoints; using newest 5 ({1} oldest discarded)" -f $availableCount, ($availableCount - 5)) -ForegroundColor Yellow
}
$selected = @($checkpoints)

# 5 本未満は sac_final.zip でパディング
$padded = $false
while ($selected.Count -lt 5) {
    $selected += [PSCustomObject]@{ Steps = -1; Name = "sac_final.zip"; FullName = $finalAbsPath }
    $padded    = $true
}
if ($padded) {
    Write-Host "  WARN: fewer than 5 checkpoints - padding with sac_final.zip" -ForegroundColor Yellow
}

# コピー & manifest 生成
Write-Host ""
$manifestSteps = @()
for ($i = 0; $i -lt 5; $i++) {
    $src      = $selected[$i]
    $dayNum   = $i + 1
    $destName = "step_{0:D2}.zip" -f $dayNum
    $destPath = Join-Path $weekDir $destName
    Copy-Item -Path $src.FullName -Destination $destPath -Force
    $label = if ($src.Steps -lt 0) { "sac_final (pad)" } else { "$($src.Steps) steps" }
    Write-Host ("  day {0} : {1,-25}  ->  {2}" -f $dayNum, $label, $destName)
    $manifestSteps += [ordered]@{
        day   = $dayNum
        step  = $src.Steps
        file  = $destName
        label = $label
    }
}

# manifest.json
$manifest = [ordered]@{
    week              = $weekId
    created_at        = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz")
    checkpoints_dir   = $CheckpointsDir
    total_checkpoints = $availableCount
    steps             = $manifestSteps
}
$manifestJson = $manifest | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText(
    (Join-Path $weekDir "manifest.json"),
    $manifestJson,
    [System.Text.UTF8Encoding]::new($false)
)
Write-Host ""
Write-Host "  manifest.json  -> created"

# state.json (current_day=1 で初期化)
$stateJson = ([ordered]@{
    week          = $weekId
    current_day   = 1
    last_advanced = $null
} | ConvertTo-Json)
[System.IO.File]::WriteAllText(
    (Join-Path $weekDir "state.json"),
    $stateJson,
    [System.Text.UTF8Encoding]::new($false)
)
Write-Host "  state.json     -> current_day=1"

# active_week.txt
[System.IO.File]::WriteAllText(
    (Join-Path $WeeksDir "active_week.txt"),
    $weekId,
    [System.Text.UTF8Encoding]::new($false)
)
Write-Host "  active_week.txt -> $weekId"

Write-Host ""
Write-Host "Done: $weekDir" -ForegroundColor Green
Write-Host "Next: tools\advance_day.ps1 -DryRun"

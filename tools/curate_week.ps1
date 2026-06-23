# tools/curate_week.ps1
#
# Weekly model curation: checkpoints から等間隔で 5 本を選び
# output/weeks/<YYYY-WNN>/ に配置して manifest.json / state.json / active_week.txt を生成する。
#
# 使い方:
#   tools\curate_week.ps1
#   tools\curate_week.ps1 -Force
#   tools\curate_week.ps1 -WeekOverride 2026-W27 -Force
#   tools\curate_week.ps1 -MaxSteps 50000          # 5万ステップ以下のcheckpointだけを対象に選出
#
# パラメータ:
#   -MaxSteps <int>   : 0 = 上限なし（既定）。指定するとステップ数が MaxSteps 以下の
#                       checkpoint だけを対象に等間隔選出する。
#                       ちょうど MaxSteps のファイルが無くても、それ以下で最大のものが
#                       step_05 に入る（自動追従）。
#                       例: -MaxSteps 50000 → 5万ステップ以下の中から 5 本選ぶ
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

# ---------------------------------------------------------------- 等間隔で N 本選出
# 先頭・末尾を必ず含み、間を均等割り（floor）
function Select-EquallySpaced {
    param([array]$Items, [int]$Count = 5)
    $n = $Items.Count
    if ($n -eq 0)      { return @() }
    if ($n -le $Count) { return $Items }
    $selected = @()
    for ($i = 0; $i -lt $Count; $i++) {
        $idx = [int][Math]::Floor($i * ($n - 1) / ($Count - 1))
        $selected += $Items[$idx]
    }
    return $selected
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
    Write-Host "  .venv\Scripts\python.exe -m block_stacker.mvp2.train --n-envs 6 --total-timesteps 100000" -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path $FinalModelPath)) {
    Write-Host "ERROR: $FinalModelPath not found." -ForegroundColor Red
    exit 1
}
$finalAbsPath = (Resolve-Path $FinalModelPath).Path

# 等間隔で 5 本選出（不足分は sac_final.zip で補完）
$selected = @(Select-EquallySpaced -Items $checkpoints -Count 5)
$padded   = $false
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
    total_checkpoints = $checkpoints.Count
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

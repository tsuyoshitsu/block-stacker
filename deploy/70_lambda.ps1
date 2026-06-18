# Step 70: Lambda (scale_up / scale_down) + EventBridge Scheduler (4 系統)。
#
# スケジュール構成（lambda/handler.py / infra-terraform/scheduler.tf と一致）:
#   bs-learner-start  cron(0 5 ? * SAT#2,SAT#4 *)  payload {asg_names: [learner]}
#   bs-learner-stop   cron(0 13 ? * SAT#2,SAT#4 *) 同上
#   bs-demo-start     cron(0 5 ? * MON-FRI *)       payload {asg_names: [demo, streamer]}
#   bs-demo-stop      cron(0 13 ? * MON-FRI *)      同上

. $PSScriptRoot/common.ps1

$accountId = $script:BS.AccountId
$region    = $script:BS.Region

$lambdaRole = Get-State lambda_role_arn
$schedRole  = Get-State scheduler_role_arn
$asgNames   = Get-State asg_names
if (-not ($lambdaRole -and $schedRole -and $asgNames)) {
    throw "40_iam / 60_ec2 を先に実行してください"
}

$asgList = @($asgNames.streamer, $asgNames.demo, $asgNames.learner)
$asgEnvJson = ($asgList | ConvertTo-Json -Compress).Replace('"', '\"')

# --------------------------------------------------------------------
# Lambda ZIP をビルド (lambda/build.ps1 を呼ぶ)
# --------------------------------------------------------------------

Write-Step "Lambda ZIP (jpholiday 同梱) をビルド"
$lambdaDir = Join-Path (Split-Path -Parent $PSScriptRoot) "lambda"
$zipPath = Join-Path $PSScriptRoot "lambda_scheduler.zip"
if (-not (Test-Path $zipPath)) {
    Push-Location $lambdaDir
    ./build.ps1
    Pop-Location
    # build.ps1 は infra/ に zip を吐くので deploy/ にコピー
    $built = Join-Path (Split-Path -Parent $PSScriptRoot) "infra/lambda_scheduler.zip"
    if (Test-Path $built) {
        Move-Item $built $zipPath -Force
    } else {
        throw "lambda_scheduler.zip が生成されませんでした"
    }
}
Write-Done "$zipPath"

# --------------------------------------------------------------------
# Lambda 関数 (scale_up / scale_down)
# --------------------------------------------------------------------

function Deploy-Lambda {
    param([string]$Name, [string]$Handler)

    $existing = aws lambda get-function --function-name $Name 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Step "$Name: コード更新"
        aws lambda update-function-code --function-name $Name `
            --zip-file "fileb://$zipPath" | Out-Null
        aws lambda update-function-configuration --function-name $Name `
            --environment "Variables={ASG_NAMES=$asgEnvJson}" | Out-Null
    } else {
        Write-Step "$Name: 新規作成"
        aws lambda create-function --function-name $Name `
            --runtime python3.12 --role $lambdaRole --handler $Handler `
            --zip-file "fileb://$zipPath" --timeout 60 `
            --environment "Variables={ASG_NAMES=$asgEnvJson}" | Out-Null
    }
    $arn = aws lambda get-function --function-name $Name --query "Configuration.FunctionArn" --output text
    Write-Done "$Name -> $arn"
    return $arn
}

$arnUp   = Deploy-Lambda "bs-scale-up"   "handler.scale_up"
$arnDown = Deploy-Lambda "bs-scale-down" "handler.scale_down"
Set-State lambda_scale_up_arn $arnUp
Set-State lambda_scale_down_arn $arnDown

# --------------------------------------------------------------------
# EventBridge Scheduler (4 系統)
# --------------------------------------------------------------------

function Deploy-Schedule {
    param(
        [string]$Name,
        [string]$Cron,
        [string]$LambdaArn,
        [string[]]$AsgList
    )

    $inputJson = (@{asg_names = $AsgList} | ConvertTo-Json -Compress)
    $target = @{
        Arn     = $LambdaArn
        RoleArn = $schedRole
        Input   = $inputJson
    } | ConvertTo-Json -Compress

    $existing = aws scheduler get-schedule --name $Name 2>&1
    if ($LASTEXITCODE -eq 0) {
        aws scheduler update-schedule --name $Name `
            --schedule-expression $Cron `
            --schedule-expression-timezone UTC `
            --flexible-time-window "Mode=OFF" `
            --target $target | Out-Null
        Write-Done "$Name (updated) $Cron"
    } else {
        aws scheduler create-schedule --name $Name `
            --schedule-expression $Cron `
            --schedule-expression-timezone UTC `
            --flexible-time-window "Mode=OFF" `
            --target $target | Out-Null
        Write-Done "$Name (created) $Cron"
    }
}

# 学習: 隔週土曜 14-22 JST (= 05:00-13:00 UTC, SAT#2 & SAT#4)
Deploy-Schedule "bs-learner-start" "cron(0 5 ? * SAT#2,SAT#4 *)"   $arnUp   @($asgNames.learner)
Deploy-Schedule "bs-learner-stop"  "cron(0 13 ? * SAT#2,SAT#4 *)"  $arnDown @($asgNames.learner)

# デモ + 配信: 平日 14-22 JST (= 05:00-13:00 UTC, MON-FRI)
Deploy-Schedule "bs-demo-start" "cron(0 5 ? * MON-FRI *)"  $arnUp   @($asgNames.demo, $asgNames.streamer)
Deploy-Schedule "bs-demo-stop"  "cron(0 13 ? * MON-FRI *)" $arnDown @($asgNames.demo, $asgNames.streamer)

# 旧スケジュール (bs-start / bs-stop) が残っていたら削除
foreach ($old in "bs-start", "bs-stop") {
    $exists = aws scheduler get-schedule --name $old 2>&1
    if ($LASTEXITCODE -eq 0) {
        aws scheduler delete-schedule --name $old | Out-Null
        Write-Done "$old (旧スケジュール削除)"
    }
}

Write-Host ""
Write-Host "[bs] 70_lambda 完了" -ForegroundColor Green
Write-Host "  学習     : 隔週土曜 14-22 JST (月 16h)" -ForegroundColor DarkGray
Write-Host "  デモ+配信: 平日   14-22 JST (月 176h, 祝日除く)" -ForegroundColor DarkGray

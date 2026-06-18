# Step 80: CloudWatch Log Groups + SNS topic + アラーム最小構成。

. $PSScriptRoot/common.ps1

$accountId = $script:BS.AccountId
$region    = $script:BS.Region

Write-Step "Log Groups を作成"
foreach ($g in "/aws/ec2/bs-streamer", "/aws/ec2/bs-demo", "/aws/ec2/bs-learner",
               "/aws/lambda/bs-scale-up", "/aws/lambda/bs-scale-down") {
    $exists = aws logs describe-log-groups --log-group-name-prefix $g `
        --query "logGroups[?logGroupName=='$g'] | length(@)" --output text
    if ($exists -eq "0") {
        aws logs create-log-group --log-group-name $g | Out-Null
        aws logs put-retention-policy --log-group-name $g --retention-in-days 14 | Out-Null
        Write-Done $g
    } else {
        Write-Done "既存 $g"
    }
}

Write-Step "SNS topic bs-alarms を作成"
$topicArn = aws sns create-topic --name bs-alarms --query "TopicArn" --output text
Set-State sns_topic_arn $topicArn
Write-Done $topicArn

Write-Step "Lambda エラーアラーム"
aws cloudwatch put-metric-alarm `
    --alarm-name "bs-lambda-scale-errors" `
    --metric-name Errors --namespace AWS/Lambda --statistic Sum `
    --period 300 --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold `
    --evaluation-periods 1 `
    --dimensions "Name=FunctionName,Value=bs-scale-up" `
    --alarm-actions $topicArn --treat-missing-data notBreaching | Out-Null
Write-Done "bs-lambda-scale-errors"

Write-Host ""
Write-Host "[bs] 80_cloudwatch 完了" -ForegroundColor Green
Write-Host "[bs] アラート通知先メールを SNS Topic ($topicArn) に subscribe してください:" -ForegroundColor Yellow
Write-Host "     aws sns subscribe --topic-arn $topicArn --protocol email --notification-endpoint your@email"

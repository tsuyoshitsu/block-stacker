# Step 90: 動作確認チェック。手動 scale-up → リソース確認 → wss 接続テスト。

. $PSScriptRoot/common.ps1

$asgNames = Get-State asg_names
if (-not $asgNames) {
    throw "60_ec2.ps1 が未実行です"
}

Write-Step "全 ASG を desired_capacity=1 に手動 scale-up"
foreach ($n in $asgNames.streamer, $asgNames.demo, $asgNames.learner) {
    aws autoscaling update-auto-scaling-group --auto-scaling-group-name $n --desired-capacity 1
    Write-Done "$n -> 1"
}

Write-Step "EC2 起動を待機 (3〜5 分)"
Start-Sleep -Seconds 30  # 起動リクエスト反映

for ($i = 0; $i -lt 30; $i++) {
    $running = aws ec2 describe-instances `
        --filters "Name=tag:Project,Values=block-stacker" "Name=instance-state-name,Values=running" `
        --query "Reservations[].Instances[].InstanceId" --output text
    $count = if ($running) { ($running -split "\s+").Count } else { 0 }
    Write-Host "  running instances: $count / 3 ..."
    if ($count -ge 3) { break }
    Start-Sleep -Seconds 15
}

Write-Step "起動済みインスタンス一覧"
aws ec2 describe-instances `
    --filters "Name=tag:Project,Values=block-stacker" "Name=instance-state-name,Values=running" `
    --query 'Reservations[].Instances[].[InstanceId, InstanceType, PrivateIpAddress, PublicIpAddress, Tags[?Key==`Name`].Value | [0]]' `
    --output table

Write-Step "wss:// 接続テスト (Python test_client、15 秒)"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
uv run python -m block_stacker.serving.test_client --uri "wss://$($script:BS.DomainName)/" --seconds 15
Pop-Location

Write-Host ""
Write-Host "[bs] 90_verify 完了" -ForegroundColor Green

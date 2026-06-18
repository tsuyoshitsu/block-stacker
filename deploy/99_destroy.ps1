# Step 99: 全リソース削除。state.json の逆順で消す。
# 危険なので確認プロンプト付き。

. $PSScriptRoot/common.ps1

$state = Load-State

Write-Host ""
Write-Host "==========  以下のリソースを削除します ==========" -ForegroundColor Yellow
$state.Keys | Sort-Object | ForEach-Object { Write-Host "  $_ = $($state[$_])" }
Write-Host ""

$confirm = Read-Host "Type 'destroy' to confirm"
if ($confirm -ne "destroy") {
    Write-Host "Aborted." -ForegroundColor Red
    exit 1
}

# --------------------------------------------------------------------
# 逆順削除
# --------------------------------------------------------------------

function Try-Run {
    param([scriptblock]$Block)
    try { & $Block } catch { Write-Host "  (warning) $_" -ForegroundColor DarkYellow }
}

# 1) EventBridge schedules
Write-Step "EventBridge schedules"
foreach ($s in "bs-start", "bs-stop") {
    Try-Run { aws scheduler delete-schedule --name $s 2>&1 | Out-Null }
}

# 2) Lambda functions
Write-Step "Lambda functions"
foreach ($f in "bs-scale-up", "bs-scale-down") {
    Try-Run { aws lambda delete-function --function-name $f 2>&1 | Out-Null }
}

# 3) ASG (terminate instances) -> Launch Templates
Write-Step "Auto Scaling Groups"
if ($state.asg_names) {
    foreach ($n in $state.asg_names.streamer, $state.asg_names.demo, $state.asg_names.learner) {
        Try-Run { aws autoscaling update-auto-scaling-group --auto-scaling-group-name $n --min-size 0 --max-size 0 --desired-capacity 0 2>&1 | Out-Null }
        Try-Run { aws autoscaling delete-auto-scaling-group --auto-scaling-group-name $n --force-delete 2>&1 | Out-Null }
    }
}

Write-Step "Launch Templates"
foreach ($k in "lt_streamer", "lt_demo", "lt_learner") {
    if ($state[$k]) {
        Try-Run { aws ec2 delete-launch-template --launch-template-id $state[$k] 2>&1 | Out-Null }
    }
}

# 4) CloudWatch alarms + SNS
Write-Step "CloudWatch / SNS"
Try-Run { aws cloudwatch delete-alarms --alarm-names "bs-lambda-scale-errors" 2>&1 | Out-Null }
if ($state.sns_topic_arn) {
    Try-Run { aws sns delete-topic --topic-arn $state.sns_topic_arn 2>&1 | Out-Null }
}

# 5) Log groups
Write-Step "Log Groups"
foreach ($g in "/aws/ec2/bs-streamer", "/aws/ec2/bs-demo", "/aws/ec2/bs-learner",
               "/aws/lambda/bs-scale-up", "/aws/lambda/bs-scale-down") {
    Try-Run { aws logs delete-log-group --log-group-name $g 2>&1 | Out-Null }
}

# 6) Route 53 record + EIP
Write-Step "Route 53 + EIP"
if ($state.route53_zone_id -and $state.eip_public_ip) {
    $change = @"
{"Changes":[{"Action":"DELETE","ResourceRecordSet":{"Name":"$($script:BS.DomainName)","Type":"A","TTL":300,"ResourceRecords":[{"Value":"$($state.eip_public_ip)"}]}}]}
"@
    $tmp = New-TemporaryFile; Set-Content $tmp -Value $change -Encoding utf8
    Try-Run { aws route53 change-resource-record-sets --hosted-zone-id $state.route53_zone_id --change-batch "file://$($tmp.FullName)" 2>&1 | Out-Null }
    Remove-Item $tmp
}
if ($state.eip_alloc_id) {
    Try-Run { aws ec2 release-address --allocation-id $state.eip_alloc_id 2>&1 | Out-Null }
}

# 7) IAM (Instance Profile -> Role -> Policy detach -> delete)
Write-Step "IAM Roles + Instance Profile"
Try-Run { aws iam remove-role-from-instance-profile --instance-profile-name bs-ec2-profile --role-name bs-ec2-role 2>&1 | Out-Null }
Try-Run { aws iam delete-instance-profile --instance-profile-name bs-ec2-profile 2>&1 | Out-Null }
foreach ($r in "bs-ec2-role", "bs-lambda-role", "bs-scheduler-role") {
    Try-Run {
        # detach managed policies + delete inline policies
        $managed = aws iam list-attached-role-policies --role-name $r --query "AttachedPolicies[].PolicyArn" --output text 2>&1
        if ($LASTEXITCODE -eq 0 -and $managed) {
            foreach ($m in $managed -split "\s+") {
                if ($m) { aws iam detach-role-policy --role-name $r --policy-arn $m 2>&1 | Out-Null }
            }
        }
        $inline = aws iam list-role-policies --role-name $r --query "PolicyNames" --output text 2>&1
        if ($LASTEXITCODE -eq 0 -and $inline) {
            foreach ($p in $inline -split "\s+") {
                if ($p) { aws iam delete-role-policy --role-name $r --policy-name $p 2>&1 | Out-Null }
            }
        }
        aws iam delete-role --role-name $r 2>&1 | Out-Null
    }
}

# 8) SSM Parameters
Write-Step "SSM Parameters"
Try-Run { aws ssm delete-parameter --name "/bs/demo/private_ip" 2>&1 | Out-Null }

# 9) VPC + 関連 (S3 + ECR endpoint, route table, subnet, IGW, SG, VPC)
Write-Step "VPC 関連"
foreach ($k in "s3_endpoint_id", "ecr_api_endpoint_id", "ecr_dkr_endpoint_id", "logs_endpoint_id") {
    if ($state[$k]) {
        Try-Run { aws ec2 delete-vpc-endpoints --vpc-endpoint-ids $state[$k] 2>&1 | Out-Null }
    }
}
if ($state.public_rt_id) {
    Try-Run { aws ec2 delete-route-table --route-table-id $state.public_rt_id 2>&1 | Out-Null }
}
if ($state.private_rt_id) {
    Try-Run { aws ec2 delete-route-table --route-table-id $state.private_rt_id 2>&1 | Out-Null }
}
foreach ($k in "sg_streamer_id", "sg_demo_id", "sg_learner_id", "sg_ecr_endpoint_id") {
    if ($state[$k]) {
        Try-Run { aws ec2 delete-security-group --group-id $state[$k] 2>&1 | Out-Null }
    }
}
foreach ($k in "public_subnet_id", "private_subnet_id") {
    if ($state[$k]) {
        Try-Run { aws ec2 delete-subnet --subnet-id $state[$k] 2>&1 | Out-Null }
    }
}
if ($state.igw_id -and $state.vpc_id) {
    Try-Run { aws ec2 detach-internet-gateway --internet-gateway-id $state.igw_id --vpc-id $state.vpc_id 2>&1 | Out-Null }
    Try-Run { aws ec2 delete-internet-gateway --internet-gateway-id $state.igw_id 2>&1 | Out-Null }
}
if ($state.vpc_id) {
    Try-Run { aws ec2 delete-vpc --vpc-id $state.vpc_id 2>&1 | Out-Null }
}

# 11) S3 bucket は手動。中身を消すか確認:
Write-Host ""
Write-Host "[bs] S3 バケット $($script:BS.AppBucket) は手動削除してください:" -ForegroundColor Yellow
Write-Host "     aws s3 rm s3://$($script:BS.AppBucket) --recursive"
Write-Host "     aws s3api delete-bucket --bucket $($script:BS.AppBucket)"
Write-Host ""

Remove-Item $script:BS.StateFile -Force -ErrorAction SilentlyContinue
Write-Host "[bs] state.json を削除しました。完了。" -ForegroundColor Green

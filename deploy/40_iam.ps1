# Step 40: IAM ロール (EC2 / Lambda / EventBridge Scheduler) と Instance Profile。

. $PSScriptRoot/common.ps1

$accountId = $script:BS.AccountId
$region    = $script:BS.Region
$bucket    = $script:BS.AppBucket

# --------------------------------------------------------------------
# EC2 ロール
# --------------------------------------------------------------------

$ec2Assume = @'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
'@

Write-Step "EC2 ロール bs-ec2-role"
$existing = aws iam get-role --role-name bs-ec2-role 2>&1
if ($LASTEXITCODE -ne 0) {
    aws iam create-role --role-name bs-ec2-role `
        --assume-role-policy-document $ec2Assume | Out-Null
    Write-Done "created"
} else {
    Write-Done "既存"
}

# S3 アクセス
$s3Policy = @"
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:ListBucket","s3:DeleteObject"],"Resource":["arn:aws:s3:::$bucket","arn:aws:s3:::$bucket/*"]}]}
"@
aws iam put-role-policy --role-name bs-ec2-role --policy-name s3-access --policy-document $s3Policy | Out-Null

# ECR / Logs / 動的 EC2 操作 (EIP associate, SSM put-parameter)
$miscPolicy = @"
{"Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Action":["ecr:GetAuthorizationToken","ecr:BatchGetImage","ecr:GetDownloadUrlForLayer","ecr:BatchCheckLayerAvailability"],"Resource":"*"},
  {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogStreams"],"Resource":"arn:aws:logs:$region:$accountId:*"},
  {"Effect":"Allow","Action":["ec2:AssociateAddress","ec2:DescribeAddresses","ec2:DescribeInstances"],"Resource":"*"},
  {"Effect":"Allow","Action":["ssm:GetParameter","ssm:PutParameter"],"Resource":"arn:aws:ssm:$region:$accountId:parameter/bs/*"}
]}
"@
aws iam put-role-policy --role-name bs-ec2-role --policy-name misc --policy-document $miscPolicy | Out-Null

# SSM Session Manager 経由でログインしたい場合
aws iam attach-role-policy --role-name bs-ec2-role `
    --policy-arn "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" 2>&1 | Out-Null

# Instance Profile
Write-Step "EC2 Instance Profile bs-ec2-profile"
$existing = aws iam get-instance-profile --instance-profile-name bs-ec2-profile 2>&1
if ($LASTEXITCODE -ne 0) {
    aws iam create-instance-profile --instance-profile-name bs-ec2-profile | Out-Null
    aws iam add-role-to-instance-profile --instance-profile-name bs-ec2-profile --role-name bs-ec2-role | Out-Null
    Write-Done "created"
} else {
    Write-Done "既存"
}
Set-State ec2_instance_profile "bs-ec2-profile"

# --------------------------------------------------------------------
# Lambda ロール
# --------------------------------------------------------------------

$lambdaAssume = @'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
'@

Write-Step "Lambda ロール bs-lambda-role"
$existing = aws iam get-role --role-name bs-lambda-role 2>&1
if ($LASTEXITCODE -ne 0) {
    aws iam create-role --role-name bs-lambda-role `
        --assume-role-policy-document $lambdaAssume | Out-Null
}

$lambdaAsgPolicy = @'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["autoscaling:UpdateAutoScalingGroup","autoscaling:DescribeAutoScalingGroups"],"Resource":"*"}]}
'@
aws iam put-role-policy --role-name bs-lambda-role --policy-name asg --policy-document $lambdaAsgPolicy | Out-Null

aws iam attach-role-policy --role-name bs-lambda-role `
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>&1 | Out-Null

$lambdaRoleArn = aws iam get-role --role-name bs-lambda-role --query "Role.Arn" --output text
Set-State lambda_role_arn $lambdaRoleArn
Write-Done "lambda role arn: $lambdaRoleArn"

# --------------------------------------------------------------------
# EventBridge Scheduler ロール
# --------------------------------------------------------------------

$schedAssume = @'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}
'@

Write-Step "Scheduler ロール bs-scheduler-role"
$existing = aws iam get-role --role-name bs-scheduler-role 2>&1
if ($LASTEXITCODE -ne 0) {
    aws iam create-role --role-name bs-scheduler-role `
        --assume-role-policy-document $schedAssume | Out-Null
}

# Lambda invoke (具体的な Lambda ARN は 70_lambda.ps1 実行後に更新)
$schedInvokeStub = @"
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["lambda:InvokeFunction"],"Resource":["arn:aws:lambda:$region:$accountId:function:bs-scale-up","arn:aws:lambda:$region:$accountId:function:bs-scale-down"]}]}
"@
aws iam put-role-policy --role-name bs-scheduler-role --policy-name invoke --policy-document $schedInvokeStub | Out-Null

$schedRoleArn = aws iam get-role --role-name bs-scheduler-role --query "Role.Arn" --output text
Set-State scheduler_role_arn $schedRoleArn
Write-Done "scheduler role arn: $schedRoleArn"

Write-Host ""
Write-Host "[bs] 40_iam 完了" -ForegroundColor Green

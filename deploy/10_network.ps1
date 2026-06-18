# Step 10: VPC + subnets + IGW + route tables + security groups + S3 VPC endpoint。
# 既に作成済みリソースは state.json から拾うので冪等に再実行可能。

. $PSScriptRoot/common.ps1

$tags = Get-DefaultTags
$tagArgs = Tag-Args $tags
$tagsForCli = ($tagArgs -join " ")

Write-Step "VPC を作成"
$state = Load-State
if (-not $state.vpc_id) {
    $vpcId = aws ec2 create-vpc `
        --cidr-block $script:BS.VpcCidr `
        --region $script:BS.Region `
        --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=bs-vpc},{Key=Project,Value=block-stacker}]" `
        --query "Vpc.VpcId" --output text
    Set-State -Key vpc_id -Value $vpcId

    aws ec2 modify-vpc-attribute --vpc-id $vpcId --enable-dns-hostnames "{`"Value`":true}" | Out-Null
    aws ec2 modify-vpc-attribute --vpc-id $vpcId --enable-dns-support   "{`"Value`":true}" | Out-Null
    Write-Done "vpc_id = $vpcId"
} else {
    Write-Done "既存 vpc_id = $($state.vpc_id)"
}

$vpcId = (Get-State vpc_id)

# --------------------- IGW ---------------------
Write-Step "Internet Gateway を作成"
if (-not (Get-State igw_id)) {
    $igwId = aws ec2 create-internet-gateway `
        --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=Name,Value=bs-igw},{Key=Project,Value=block-stacker}]" `
        --query "InternetGateway.InternetGatewayId" --output text
    aws ec2 attach-internet-gateway --internet-gateway-id $igwId --vpc-id $vpcId | Out-Null
    Set-State igw_id $igwId
    Write-Done "igw_id = $igwId"
} else {
    Write-Done "既存 igw_id = $(Get-State igw_id)"
}

# --------------------- Subnets ---------------------
Write-Step "Public / Private Subnet を作成"

if (-not (Get-State public_subnet_id)) {
    $sid = aws ec2 create-subnet `
        --vpc-id $vpcId --cidr-block $script:BS.PublicCidr `
        --availability-zone $script:BS.Az `
        --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=bs-public},{Key=Project,Value=block-stacker}]" `
        --query "Subnet.SubnetId" --output text
    Set-State public_subnet_id $sid
    Write-Done "public_subnet_id = $sid"
}

if (-not (Get-State private_subnet_id)) {
    $sid = aws ec2 create-subnet `
        --vpc-id $vpcId --cidr-block $script:BS.PrivateCidr `
        --availability-zone $script:BS.Az `
        --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=bs-private},{Key=Project,Value=block-stacker}]" `
        --query "Subnet.SubnetId" --output text
    Set-State private_subnet_id $sid
    Write-Done "private_subnet_id = $sid"
}

# --------------------- Route Tables ---------------------
Write-Step "Route Tables を作成"

if (-not (Get-State public_rt_id)) {
    $rt = aws ec2 create-route-table --vpc-id $vpcId `
        --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=bs-public-rt}]" `
        --query "RouteTable.RouteTableId" --output text
    aws ec2 create-route --route-table-id $rt --destination-cidr-block "0.0.0.0/0" --gateway-id (Get-State igw_id) | Out-Null
    aws ec2 associate-route-table --route-table-id $rt --subnet-id (Get-State public_subnet_id) | Out-Null
    Set-State public_rt_id $rt
    Write-Done "public_rt_id = $rt"
}

if (-not (Get-State private_rt_id)) {
    $rt = aws ec2 create-route-table --vpc-id $vpcId `
        --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=bs-private-rt}]" `
        --query "RouteTable.RouteTableId" --output text
    aws ec2 associate-route-table --route-table-id $rt --subnet-id (Get-State private_subnet_id) | Out-Null
    Set-State private_rt_id $rt
    Write-Done "private_rt_id = $rt"
}

# --------------------- S3 VPC Endpoint (Gateway, 無料) ---------------------
Write-Step "S3 VPC Endpoint (Gateway) を作成"
if (-not (Get-State s3_endpoint_id)) {
    $svc = "com.amazonaws.$($script:BS.Region).s3"
    $epId = aws ec2 create-vpc-endpoint `
        --vpc-id $vpcId --service-name $svc `
        --vpc-endpoint-type Gateway `
        --route-table-ids (Get-State public_rt_id) (Get-State private_rt_id) `
        --query "VpcEndpoint.VpcEndpointId" --output text
    Set-State s3_endpoint_id $epId
    Write-Done "s3_endpoint_id = $epId"
}

# --------------------- Security Groups ---------------------
Write-Step "Security Groups を作成"

function New-Sg([string]$Name, [string]$Description) {
    $stateKey = "sg_${Name}_id"
    if (Get-State $stateKey) {
        Write-Done "既存 $Name SG: $(Get-State $stateKey)"
        return (Get-State $stateKey)
    }
    $sgId = aws ec2 create-security-group `
        --group-name "bs-$Name" --description $Description `
        --vpc-id $vpcId `
        --tag-specifications "ResourceType=security-group,Tags=[{Key=Name,Value=bs-$Name}]" `
        --query "GroupId" --output text
    Set-State $stateKey $sgId
    Write-Done "$Name SG: $sgId"
    return $sgId
}

$streamerSg = New-Sg "streamer" "Caddy + WS reverse proxy"
$demoSg     = New-Sg "demo"     "Demo server (ai_server :8765)"
$learnerSg  = New-Sg "learner"  "Learner (S3 only, no inbound)"
$vpceSg     = New-Sg "vpce"     "VPC Interface Endpoints (HTTPS from VPC CIDR)"

# VPC Endpoint SG: 443 from VPC CIDR
Write-Step "VPCE SG: 443 from VPC CIDR ($($script:BS.VpcCidr))"
$existing = aws ec2 describe-security-groups --group-ids $vpceSg --query "SecurityGroups[0].IpPermissions" --output json | ConvertFrom-Json
if (-not ($existing | Where-Object { $_.FromPort -eq 443 })) {
    aws ec2 authorize-security-group-ingress --group-id $vpceSg `
        --protocol tcp --port 443 --cidr $script:BS.VpcCidr | Out-Null
}

# Streamer: 443 + 80 from world
Write-Step "Streamer SG: 443/80 inbound from 0.0.0.0/0"
$existing = aws ec2 describe-security-groups --group-ids $streamerSg --query "SecurityGroups[0].IpPermissions" --output json | ConvertFrom-Json
$has443 = $existing | Where-Object { $_.FromPort -eq 443 }
$has80  = $existing | Where-Object { $_.FromPort -eq 80 }
if (-not $has443) {
    aws ec2 authorize-security-group-ingress --group-id $streamerSg `
        --protocol tcp --port 443 --cidr "0.0.0.0/0" | Out-Null
}
if (-not $has80) {
    aws ec2 authorize-security-group-ingress --group-id $streamerSg `
        --protocol tcp --port 80 --cidr "0.0.0.0/0" | Out-Null
}

# Demo: 8765 from streamer SG
Write-Step "Demo SG: 8765 from streamer SG"
$existing = aws ec2 describe-security-groups --group-ids $demoSg --query "SecurityGroups[0].IpPermissions" --output json | ConvertFrom-Json
$has8765 = $existing | Where-Object { $_.FromPort -eq 8765 }
if (-not $has8765) {
    aws ec2 authorize-security-group-ingress --group-id $demoSg `
        --protocol tcp --port 8765 --source-group $streamerSg | Out-Null
}

# --------------------- ECR / Logs Interface Endpoints (NAT 代替) ---------------------
# Private Subnet の EC2 が ECR pull / CW Logs 送信を IGW 無しで行うため必須。
# Endpoint 1個あたり ~$7.3/月 + 0.01/GB データ処理料。NAT Gateway ($35/月) より安い。
Write-Step "ECR / Logs Interface Endpoints を作成"

function New-InterfaceEndpoint {
    param([string]$ShortName, [string]$ServiceSuffix)
    $stateKey = "${ShortName}_endpoint_id"
    if (Get-State $stateKey) {
        Write-Done "既存 $ShortName endpoint: $(Get-State $stateKey)"
        return
    }
    $svc = "com.amazonaws.$($script:BS.Region).$ServiceSuffix"
    $epId = aws ec2 create-vpc-endpoint `
        --vpc-id $vpcId --service-name $svc `
        --vpc-endpoint-type Interface `
        --subnet-ids (Get-State private_subnet_id) `
        --security-group-ids $vpceSg `
        --private-dns-enabled `
        --query "VpcEndpoint.VpcEndpointId" --output text
    Set-State $stateKey $epId
    Write-Done "$ShortName endpoint: $epId"
}

New-InterfaceEndpoint -ShortName "ecr_api" -ServiceSuffix "ecr.api"
New-InterfaceEndpoint -ShortName "ecr_dkr" -ServiceSuffix "ecr.dkr"
New-InterfaceEndpoint -ShortName "logs"    -ServiceSuffix "logs"

Write-Host ""
Write-Host "[bs] 10_network 完了" -ForegroundColor Green

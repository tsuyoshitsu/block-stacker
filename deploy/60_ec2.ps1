# Step 60: Launch Templates + Auto Scaling Groups (全 Spot, 100% capacity-optimized)。
# 各 ASG は desired_capacity=0 で作成。EventBridge が稼働時間に 1 へ。

. $PSScriptRoot/common.ps1

$accountId   = $script:BS.AccountId
$region      = $script:BS.Region
$bucket      = $script:BS.AppBucket
$ecrRegistry = $script:BS.EcrRegistry

$publicSubnet  = Get-State public_subnet_id
$privateSubnet = Get-State private_subnet_id
$streamerSg    = Get-State sg_streamer_id
$demoSg        = Get-State sg_demo_id
$learnerSg     = Get-State sg_learner_id
$eipAlloc      = Get-State eip_alloc_id
$profile       = Get-State ec2_instance_profile

if (-not ($publicSubnet -and $privateSubnet -and $streamerSg -and $demoSg -and $learnerSg -and $eipAlloc -and $profile)) {
    throw "前段 (10〜50) の state が不足しています。"
}

# --------------------------------------------------------------------
# 最新 AMI を解決
# --------------------------------------------------------------------

Write-Step "AMI を解決 (al2023 arm64 / x86_64)"

$amiArm = aws ec2 describe-images --owners amazon `
    --filters "Name=name,Values=al2023-ami-2023.*-arm64" "Name=virtualization-type,Values=hvm" `
    --query "sort_by(Images, &CreationDate)[-1].ImageId" --output text
$amiX86 = aws ec2 describe-images --owners amazon `
    --filters "Name=name,Values=al2023-ami-2023.*-x86_64" "Name=virtualization-type,Values=hvm" `
    --query "sort_by(Images, &CreationDate)[-1].ImageId" --output text

Write-Done "arm64 = $amiArm"
Write-Done "x86_64 = $amiX86 (demo + learner 共用)"

# --------------------------------------------------------------------
# Launch Templates
# --------------------------------------------------------------------

function New-Lt {
    param(
        [string]$Name,
        [string]$Ami,
        [string]$InstanceType,
        [string]$SgId,
        [string]$SubnetId,
        [string]$UserDataB64,
        [int]$DiskGb = 30
    )

    $existing = aws ec2 describe-launch-templates --launch-template-names $Name 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Done "既存 LT: $Name"
        return (aws ec2 describe-launch-templates --launch-template-names $Name `
            --query "LaunchTemplates[0].LaunchTemplateId" --output text)
    }

    $bdm = @"
[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":$DiskGb,"VolumeType":"gp3","DeleteOnTermination":true}}]
"@

    $ltData = @{
        ImageId      = $Ami
        InstanceType = $InstanceType
        IamInstanceProfile = @{Name = $profile}
        NetworkInterfaces = @(@{
            DeviceIndex                = 0
            SubnetId                   = $SubnetId
            Groups                     = @($SgId)
            AssociatePublicIpAddress   = $false
        })
        UserData = $UserDataB64
        BlockDeviceMappings = @(@{
            DeviceName = "/dev/xvda"
            Ebs = @{VolumeSize = $DiskGb; VolumeType = "gp3"; DeleteOnTermination = $true}
        })
        TagSpecifications = @(@{
            ResourceType = "instance"
            Tags = @(
                @{Key="Name"; Value=$Name -replace "-lt$",""},
                @{Key="Project"; Value="block-stacker"}
            )
        })
    } | ConvertTo-Json -Depth 10 -Compress

    $tmp = New-TemporaryFile
    Set-Content $tmp -Value $ltData -Encoding utf8

    $ltId = aws ec2 create-launch-template --launch-template-name $Name `
        --launch-template-data "file://$($tmp.FullName)" `
        --query "LaunchTemplate.LaunchTemplateId" --output text
    Remove-Item $tmp
    Write-Done "$Name -> $ltId"
    return $ltId
}

Write-Step "user-data を生成 (streamer / demo / learner)"

$udStreamer = Expand-Userdata "streamer.sh" @{
    REGION     = $region
    DOMAIN     = $script:BS.DomainName
    EIP_ALLOC  = $eipAlloc
    APP_BUCKET = $bucket
}

$udDemo = Expand-Userdata "demo.sh" @{
    REGION       = $region
    APP_BUCKET   = $bucket
    ECR_REGISTRY = $ecrRegistry
}

$udLearner = Expand-Userdata "learner.sh" @{
    REGION       = $region
    APP_BUCKET   = $bucket
    ECR_REGISTRY = $ecrRegistry
}

Write-Step "Launch Templates 作成"

$ltStreamer = New-Lt -Name "bs-streamer-lt" -Ami $amiArm `
    -InstanceType $script:BS.StreamerType -SgId $streamerSg `
    -SubnetId $publicSubnet -UserDataB64 $udStreamer
Set-State lt_streamer $ltStreamer

$ltDemo = New-Lt -Name "bs-demo-lt" -Ami $amiX86 `
    -InstanceType $script:BS.DemoType -SgId $demoSg `
    -SubnetId $privateSubnet -UserDataB64 $udDemo -DiskGb 50
Set-State lt_demo $ltDemo

$ltLearner = New-Lt -Name "bs-learner-lt" -Ami $amiX86 `
    -InstanceType $script:BS.LearnerType -SgId $learnerSg `
    -SubnetId $privateSubnet -UserDataB64 $udLearner -DiskGb 100
Set-State lt_learner $ltLearner

# --------------------------------------------------------------------
# Auto Scaling Groups (全 Spot, capacity-optimized)
# --------------------------------------------------------------------

function New-Asg {
    param(
        [string]$Name,
        [string]$LtId,
        [string]$SubnetId,
        [string[]]$Overrides = @()
    )

    $existing = aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names $Name 2>&1
    if ($LASTEXITCODE -eq 0 -and ($existing | ConvertFrom-Json).AutoScalingGroups.Count -gt 0) {
        Write-Done "既存 ASG: $Name"
        return
    }

    $overrideJson = if ($Overrides.Count -gt 0) {
        ($Overrides | ForEach-Object { @{InstanceType=$_} }) | ConvertTo-Json -Compress
    } else { "[]" }

    $mixed = @"
{
  "LaunchTemplate": {
    "LaunchTemplateSpecification": {"LaunchTemplateId":"$LtId","Version":"`$Latest"},
    "Overrides": $overrideJson
  },
  "InstancesDistribution": {
    "OnDemandBaseCapacity": 0,
    "OnDemandPercentageAboveBaseCapacity": 0,
    "SpotAllocationStrategy": "capacity-optimized"
  }
}
"@
    $tmp = New-TemporaryFile
    Set-Content $tmp -Value $mixed -Encoding utf8

    aws autoscaling create-auto-scaling-group `
        --auto-scaling-group-name $Name `
        --min-size 0 --max-size 1 --desired-capacity 0 `
        --vpc-zone-identifier $SubnetId `
        --health-check-type EC2 --health-check-grace-period 300 `
        --mixed-instances-policy "file://$($tmp.FullName)" `
        --tags "Key=Project,Value=block-stacker,PropagateAtLaunch=true" | Out-Null
    Remove-Item $tmp
    Write-Done "$Name (desired=0)"
}

Write-Step "Auto Scaling Groups 作成"

New-Asg -Name "bs-streamer-asg" -LtId $ltStreamer -SubnetId $publicSubnet
New-Asg -Name "bs-demo-asg"     -LtId $ltDemo     -SubnetId $privateSubnet
New-Asg -Name "bs-learner-asg"  -LtId $ltLearner  -SubnetId $privateSubnet `
    -Overrides $script:BS.LearnerFallback

Set-State asg_names @{
    streamer = "bs-streamer-asg"
    demo     = "bs-demo-asg"
    learner  = "bs-learner-asg"
}

Write-Host ""
Write-Host "[bs] 60_ec2 完了 (desired=0、稼働は 70_lambda の EventBridge で)" -ForegroundColor Green

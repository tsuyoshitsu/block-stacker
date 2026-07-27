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

# ASG は撤去した（min=0/max=1 でスケールしておらず、実態は「起動/停止スイッチ」だった）。
# 単一 EC2 を作って即 stop し、以降は Lambda が start/stop する方式に置き換えている。
# 経緯と ASG 版との差分は docs/design_change_record.md を参照。
#
# ASG を失って手放した機能（許容と判断。手動対応する）:
#   - Spot 在庫切れ時の複数インスタンスタイプ自動フォールバック
#   - Spot 中断後の自動再起動
#   → 対処手順は docs/aws_deployment.md §8 トラブルシューティングに記載。

function New-StoppedInstance {
    param(
        [string]$Name,
        [string]$LtId,
        [string]$SubnetId
    )

    # 既存インスタンス（terminated 以外）があれば再利用する。
    $existing = aws ec2 describe-instances `
        --filters "Name=tag:Name,Values=$Name" "Name=instance-state-name,Values=pending,running,stopping,stopped" `
        --query "Reservations[0].Instances[0].InstanceId" --output text 2>&1
    if ($LASTEXITCODE -eq 0 -and $existing -and $existing -ne "None") {
        Write-Done "既存インスタンス: $Name ($existing)"
        return $existing
    }

    # Spot で 1 台起動。--instance-market-options で spot を指定する
    # （ASG の mixed-instances-policy と違い、タイプのフォールバックは無い）。
    $iid = aws ec2 run-instances `
        --launch-template "LaunchTemplateId=$LtId,Version=`$Latest" `
        --subnet-id $SubnetId `
        --instance-market-options "MarketType=spot" `
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$Name},{Key=Project,Value=block-stacker}]" `
        --query "Instances[0].InstanceId" --output text
    if ($LASTEXITCODE -ne 0 -or -not $iid) {
        throw "run-instances に失敗しました: $Name（Spot 在庫切れの可能性。common.ps1 のインスタンスタイプを変えて再実行）"
    }

    # 起動しっぱなしにしないよう、running になり次第すぐ停止する。
    # 実際の稼働開始は Lambda（EventBridge スケジュール）が start する時点。
    aws ec2 wait instance-running --instance-ids $iid
    aws ec2 stop-instances --instance-ids $iid | Out-Null
    Write-Done "$Name ($iid, stopped)"
    return $iid
}

Write-Step "EC2 インスタンス作成（作成後すぐ stop）"

$idStreamer = New-StoppedInstance -Name "bs-streamer" -LtId $ltStreamer -SubnetId $publicSubnet
$idDemo     = New-StoppedInstance -Name "bs-demo"     -LtId $ltDemo     -SubnetId $privateSubnet
$idLearner  = New-StoppedInstance -Name "bs-learner"  -LtId $ltLearner  -SubnetId $privateSubnet

Set-State instance_ids @{
    streamer = $idStreamer
    demo     = $idDemo
    learner  = $idLearner
}

Write-Host ""
Write-Host "[bs] 60_ec2 完了 (全て stopped、稼働は 70_lambda の EventBridge で start)" -ForegroundColor Green

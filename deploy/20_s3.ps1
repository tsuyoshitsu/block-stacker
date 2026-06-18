# Step 20: app S3 bucket の作成 + 設定 (バージョニング、暗号化、Public Access Block)。

. $PSScriptRoot/common.ps1

$bucket = $script:BS.AppBucket
$region = $script:BS.Region

Write-Step "App S3 バケット ($bucket) を確認/作成"

$exists = aws s3api head-bucket --bucket $bucket 2>&1
if ($LASTEXITCODE -ne 0) {
    aws s3api create-bucket --bucket $bucket --region $region `
        --create-bucket-configuration LocationConstraint=$region | Out-Null
    Write-Done "bucket created"
} else {
    Write-Done "既存 bucket を使う"
}
Set-State app_bucket $bucket

Write-Step "バージョニング有効化"
aws s3api put-bucket-versioning --bucket $bucket `
    --versioning-configuration "Status=Enabled" | Out-Null

Write-Step "Public Access Block を有効化"
aws s3api put-public-access-block --bucket $bucket --public-access-block-configuration `
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" | Out-Null

Write-Step "SSE-S3 暗号化"
$sseJson = '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-encryption --bucket $bucket --server-side-encryption-configuration $sseJson | Out-Null

Write-Step "プレフィックスを初期化 (models/, world_state/, state/, configs/)"
foreach ($p in "models/", "world_state/", "state/", "configs/") {
    aws s3api put-object --bucket $bucket --key $p | Out-Null
}

Write-Step "configs/*.yaml をアップロード"
$cfgDir = Join-Path (Split-Path -Parent $PSScriptRoot) "configs"
foreach ($f in "world.yaml", "physics.yaml", "training.yaml", "reward.yaml") {
    $src = Join-Path $cfgDir $f
    if (Test-Path $src) {
        aws s3 cp $src "s3://$bucket/configs/$f" | Out-Null
        Write-Done "configs/$f"
    }
}

Write-Host ""
Write-Host "[bs] 20_s3 完了 (bucket=$bucket)" -ForegroundColor Green

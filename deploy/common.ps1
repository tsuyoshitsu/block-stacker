# block-stacker CLI デプロイ共通モジュール。
#
# 役割:
#   - 環境変数の集中定義（リージョン、ドメイン、リソース命名）
#   - 作成済みリソース ID を deploy/state.json に永続化（tfstate の代替）
#   - 各スクリプトが Get-State / Set-State で読み書きする
#
# 各 step スクリプトの先頭で:
#   . $PSScriptRoot/common.ps1
# として読み込む。

$ErrorActionPreference = "Stop"

# --------------------------------------------------------------------
# 基本設定（必要に応じてここを書き換える）
# --------------------------------------------------------------------

$script:BS = @{
    Region        = "ap-northeast-1"
    Az            = "ap-northeast-1a"
    Project       = "block-stacker"
    Env           = "prod"

    # ドメイン（要書き換え）
    DomainZone    = "example.com"
    DomainName    = "bs.example.com"

    # S3 (事前作成想定。docs/aws_deployment.md §2.3 参照)
    AppBucketPrefix = "bs-app"

    # 命名規約
    VpcCidr       = "10.10.0.0/16"
    PublicCidr    = "10.10.1.0/24"
    PrivateCidr   = "10.10.2.0/24"

    # EC2
    StreamerType  = "t4g.small"
    DemoType      = "c6i.xlarge"
    # 学習: AMD EPYC CPU-only (8 物理コア)。GPU 不要なため g4dn から差替で約 40% コスト減。
    LearnerType   = "c6a.4xlarge"
    LearnerFallback = @("c6a.4xlarge", "c6i.4xlarge", "c7a.4xlarge", "m6a.4xlarge")

    StateFile     = Join-Path $PSScriptRoot "state.json"
    UserdataDir   = Join-Path $PSScriptRoot "userdata"
}

# 動的に解決する
$script:BS.AccountId  = (aws sts get-caller-identity --query Account --output text)
$script:BS.AppBucket  = "$($script:BS.AppBucketPrefix)-$($script:BS.AccountId)"
$script:BS.EcrRegistry = "$($script:BS.AccountId).dkr.ecr.$($script:BS.Region).amazonaws.com"

# --------------------------------------------------------------------
# state.json の I/O
# --------------------------------------------------------------------

function Load-State {
    if (Test-Path $script:BS.StateFile) {
        return Get-Content $script:BS.StateFile -Raw | ConvertFrom-Json -AsHashtable
    }
    return @{}
}

function Save-State {
    param([hashtable]$State)
    $State | ConvertTo-Json -Depth 10 | Set-Content -Path $script:BS.StateFile -Encoding utf8
}

function Get-State {
    param([string]$Key)
    $s = Load-State
    return $s[$Key]
}

function Set-State {
    param([string]$Key, $Value)
    $s = Load-State
    $s[$Key] = $Value
    Save-State $s
}

# --------------------------------------------------------------------
# 便利関数
# --------------------------------------------------------------------

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Done {
    param([string]$Message)
    Write-Host "    ✓ $Message" -ForegroundColor Green
}

function Tag-Args {
    # `aws ec2 create-tags` 用のキー=値ペア生成
    param([hashtable]$Tags)
    $args = @()
    foreach ($k in $Tags.Keys) {
        $args += "Key=$k,Value=$($Tags[$k])"
    }
    return $args
}

function Expand-Userdata {
    # deploy/userdata/<file> の <<KEY>> プレースホルダを Map で置換し、
    # base64 文字列を返す。EC2 user-data に渡す形式。
    param([string]$FileName, [hashtable]$Vars)

    $path = Join-Path $script:BS.UserdataDir $FileName
    $content = Get-Content $path -Raw
    foreach ($k in $Vars.Keys) {
        $content = $content.Replace("<<$k>>", [string]$Vars[$k])
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($content)
    return [Convert]::ToBase64String($bytes)
}

# --------------------------------------------------------------------
# 共通タグ
# --------------------------------------------------------------------

function Get-DefaultTags {
    return @{
        Project   = $script:BS.Project
        Env       = $script:BS.Env
        ManagedBy = "cli-script"
    }
}

# 読み込んだことが分かるように軽くログ
Write-Host "[bs] common loaded. Region=$($script:BS.Region) Account=$($script:BS.AccountId) Bucket=$($script:BS.AppBucket)" -ForegroundColor DarkGray

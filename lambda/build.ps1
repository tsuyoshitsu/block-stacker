# Lambda デプロイパッケージ (lambda_scheduler.zip) をビルドする PowerShell スクリプト。
#
# 使い方:
#   cd lambda
#   ./build.ps1
#
# 結果: 親ディレクトリの infra/lambda_scheduler.zip を生成。
#       infra/scheduler.tf の filename = "lambda_scheduler.zip" と整合。

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$buildDir = Join-Path $here "_build"
$zipPath = Join-Path (Split-Path -Parent $here) "infra/lambda_scheduler.zip"

if (Test-Path $buildDir) { Remove-Item -Recurse -Force $buildDir }
New-Item -ItemType Directory -Path $buildDir | Out-Null

# 依存をターゲットディレクトリにインストール
pip install --no-cache-dir --target $buildDir -r requirements.txt

# Lambda ハンドラ
Copy-Item handler.py $buildDir/

# ZIP に固める
if (Test-Path $zipPath) { Remove-Item $zipPath }
Push-Location $buildDir
Compress-Archive -Path * -DestinationPath $zipPath
Pop-Location

Write-Host "Built: $zipPath"

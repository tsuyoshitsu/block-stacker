# AWS デプロイ手順書（block-stacker / CLI 版）

このドキュメントは block-stacker を AWS にデプロイする手順書です。
**AWS CLI** + **PowerShell スクリプト**（`deploy/`）で実施します。
設計書 §8（AWS構成・運用）の確定構成に対応。

> 旧 Terraform 版は `infra-terraform/` に参照用として保持してあります。

## 目次

- [0. 構成サマリ](#0-構成サマリ)
- [1. 前提条件](#1-前提条件)
- [2. 事前準備](#2-事前準備)
- [3. ECR にイメージを Push](#3-ecr-にイメージを-push)
- [4. CLI スクリプトでデプロイ](#4-cli-スクリプトでデプロイ)
- [5. 動作確認](#5-動作確認)
- [6. 運用](#6-運用)
- [7. 削除](#7-削除)
- [8. トラブルシューティング](#8-トラブルシューティング)
- [付録 A. deploy/ のディレクトリ構成](#付録-a-deploy-のディレクトリ構成)
- [付録 B. リソース ID の手動確認](#付録-b-リソース-id-の手動確認)
- [付録 C. コスト管理](#付録-c-コスト管理)
- [付録 D. Redis を再導入すべき条件](#付録-d-redis-を再導入すべき条件)
- [付録 E. 主要な設計上の決定](#付録-e-主要な設計上の決定)
- [付録 F. 今後の検討事項（未実装アイデア集）](#付録-f-今後の検討事項未実装アイデア集)
- [付録 G. ローカル開発ワークフロー](#付録-g-ローカル開発ワークフロー)

> **関連ドキュメント**:
> - [`docs/local_demo.md`](local_demo.md) — ローカル試運転 + checkpoint 比較の手順書
> - [`client/README.md`](../client/README.md) — Godot 4.4.1 .NET クライアントのセットアップ

---

## 0. 構成サマリ

| 項目 | 内容 |
|---|---|
| リージョン | ap-northeast-1 (Tokyo) |
| 稼働日時 | **学習: 隔週土曜 14-22 JST (月 16h)** / **デモ+配信: 平日 14-22 JST (月 176h、祝日除く)** |
| 学習 EC2 | `c6a.4xlarge` Spot（AMD EPYC, CPU-only, 8 物理コア） |
| デモ EC2 | `c6i.xlarge` Spot |
| 配信 EC2 | `t4g.small` Spot + Caddy（自動 TLS） |
| クライアント | Godot 4.4.1 .NET 版 + C# (`WsClient.cs`)、視聴者の PC で実行 |
| ロードバランサ | なし（EC2 + EIP + Caddy） |
| 永続化 | S3 のみ（`models/` / `world_state/` / `configs/` / `state/`） |
| 記憶構成 | **3 層**: 勘 + 短期記憶（観測内 5 手）+ 重みつき長期記憶（リプレイバッファ）。詳細は付録 E §1 |
| 積み木形状 | **4 種**: cube / cuboid / triangular_prism / cylinder。カリキュラムで難易度順に追加（付録 E §1.1） |
| キャッシュ | なし（現状 Redis 不要。導入条件は付録 D） |
| スケジューラ | EventBridge × 4 + Lambda 1 ペア（payload で対象 ASG 切替、`jpholiday` で祝日判定） |
| Private 通信 | S3 Gateway + ECR/Logs Interface Endpoint × 3（NAT 不使用） |
| 想定月額 | **約 ¥7,765**（年 ¥93,000、§コスト管理参照） |

### アーキテクチャ

```
                                  Internet
                                      │
                                      ▼
                               Route 53 → EIP
                                      │
                                      ▼
   ┌──────────────────────────────────────────────────┐
   │ Public Subnet (10.10.1.0/24)                     │
   │   ┌────────────────────────────────────────┐    │
   │   │ 配信 EC2 t4g.small Spot                 │    │
   │   │   - Caddy（自動 TLS）                   │    │
   │   │   - reverse_proxy 443 → demo:8765       │    │
   │   └────────┬───────────────────────────────┘    │
   └────────────┼─────────────────────────────────────┘
                │ SG: streamer → demo:8765 (VPC 内クロス SG)
                ▼
   ┌──────────────────────────────────────────────────┐
   │ Private Subnet (10.10.2.0/24)                    │
   │   ┌────────────────────────────────┐             │
   │   │ デモ EC2 c6i.xlarge Spot        │             │
   │   │   - ai_server.py (Docker)       │             │
   │   │   - WebSocket :8765 (in-process)│             │
   │   └────────────────────────────────┘             │
   │                                                  │
   │   ┌────────────────────────────────┐             │
   │   │ 学習 EC2 c6a.4xlarge Spot       │             │
   │   │   - SAC 訓練 (Docker)           │             │
   │   │   - 5 分毎 S3 へ checkpoint     │             │
   │   └────────────────────────────────┘             │
   │                                                  │
   │   ┌──────────────────────────────────────────┐  │
   │   │ VPC Endpoints (この Subnet 内に生やす)    │  │
   │   │  ┌──────────────┐                        │  │
   │   │  │ S3 Gateway   │ → S3（コンテナ層 / state / configs / models） │
   │   │  │ (無料)        │                        │  │
   │   │  ├──────────────┤                        │  │
   │   │  │ ecr.api      │ → ECR auth & manifest │  │
   │   │  │ (Interface)  │                        │  │
   │   │  ├──────────────┤                        │  │
   │   │  │ ecr.dkr      │ → ECR docker pull API │  │
   │   │  │ (Interface)  │                        │  │
   │   │  ├──────────────┤                        │  │
   │   │  │ logs         │ → CloudWatch Logs     │  │
   │   │  │ (Interface)  │                        │  │
   │   │  └──────────────┘                        │  │
   │   └──────────────────────────────────────────┘  │
   └──────────────────────────────────────────────────┘
```

**設計上のポイント:**
- Private Subnet は **IGW・NAT を一切持たない**。インターネット境界はEndpoint だけ。
- Endpoint は **AWS PrivateLink で AWS API 内部に閉じる**。外部通信は発生しない。
- ECR pull のイメージレイヤは S3 経由（無料の Gateway Endpoint で完結）、API/manifest だけ Interface Endpoint を使う。
- 全 EC2 は Spot + ASG（`max_size=1`）。EventBridge が稼働時間に `desired=1` へ。

---

## 1. 前提条件

### 1.1 ローカル環境

- **AWS CLI v2**: `aws --version`
- **PowerShell 7+** (Windows ネイティブ。`pwsh --version`)
- **Docker Desktop** (イメージビルド用)
- **uv** + Python 3.12 (Lambda zip ビルドと配信テスト用)
- **jq** 任意

### 1.2 AWS アカウント

- 課金有効
- サービスクォータ（デフォルト範囲で OK）:
  - c6a / c6i Spot vCPU: 計 24 程度（学習 16 + デモ 4 + フォールバック余裕）
  - t4g Spot vCPU: 8
  - Interface VPC Endpoint: 3 (ecr.api + ecr.dkr + logs)

### 1.3 ドメイン

Route 53 でホストゾーンを作成し、レジストラ側で NS 委任済みであること。
例: `example.com`

---

## 2. 事前準備

### 2.1 AWS CLI のセットアップ

```powershell
aws configure
# Access Key / Secret / region=ap-northeast-1 / format=json
aws sts get-caller-identity   # 確認
```

### 2.2 deploy/common.ps1 の編集

`deploy/common.ps1` 冒頭の `$script:BS` ハッシュテーブルを自分の環境に合わせて編集：

```powershell
DomainZone   = "example.com"      # ← ホストゾーン名
DomainName   = "bs.example.com"   # ← 配信用 DNS
AppBucketPrefix = "bs-app"        # → "bs-app-<ACCOUNT_ID>" がフルバケット名に
```

### 2.3 S3 バケットの命名と作成

`common.ps1` がカレント AWS アカウント ID を取得して `bs-app-<ACCOUNT_ID>` を組み立てます。バケット作成は `20_s3.ps1` がやるので手動準備は不要。

---

## 3. ECR にイメージを Push

### 3.1 ECR リポジトリを作成

```powershell
aws ecr create-repository --repository-name block-stacker/demo
aws ecr create-repository --repository-name block-stacker/learner
# 配信用は Caddy をホストインストールで使うのでイメージ不要
```

### 3.2 Docker イメージビルドと Push

```powershell
$REGION = "ap-northeast-1"
$ACCOUNT = aws sts get-caller-identity --query Account --output text
$REGISTRY = "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REGISTRY

# デモ用 (CPU)
docker build -t block-stacker/demo:latest .
docker tag block-stacker/demo:latest "$REGISTRY/block-stacker/demo:latest"
docker push "$REGISTRY/block-stacker/demo:latest"

# 学習用 (CPU torch, AMD EPYC)
docker build -f Dockerfile.learner -t block-stacker/learner:latest .
docker tag block-stacker/learner:latest "$REGISTRY/block-stacker/learner:latest"
docker push "$REGISTRY/block-stacker/learner:latest"
```

### 3.3 学習済みモデルを S3 に上げる（任意、初回はスキップ可）

```powershell
aws s3 cp output/mvp2/sac_final.zip s3://bs-app-$ACCOUNT/models/latest.pt
```

未アップロードの場合、デモ EC2 起動時にモデルが無いのでエラーログが出ます。
初回は「インフラを立ち上げ、後でモデルを upload」で OK。

---

## 4. CLI スクリプトでデプロイ

各スクリプトは冪等（state.json で既存リソースを検知）です。順番に実行：

```powershell
cd C:\Users\iii03\block-stacker\deploy

./10_network.ps1       # VPC / subnets / IGW / SG / S3 Gateway / ECR & Logs Interface Endpoints
./20_s3.ps1            # App bucket + configs upload
./40_iam.ps1           # IAM roles + Instance Profile (ECR pull / S3 / CW Logs)
./50_eip.ps1           # EIP + Route 53 A record
./60_ec2.ps1           # Launch Templates + ASG x3 (desired=0)
./70_lambda.ps1        # Lambda + EventBridge (要 lambda/build.ps1 事前実行)
./80_cloudwatch.ps1    # Log groups + SNS topic + alarm
```

すべての作成済みリソース ID は `deploy/state.json` に書かれます。

### 4.1 依存関係マップ

| ステップ | 依存 | 作るもの | 後段が読む state キー |
|---------|------|---------|---------------------|
| 10_network | — | VPC / Subnet / SG / S3+ECR+Logs Endpoint | `vpc_id`, `public_subnet_id`, `private_subnet_id`, `sg_*_id`, `*_endpoint_id` |
| 20_s3 | (AWS API のみ) | S3 バケット + 4 プレフィックス | `app_bucket` (確認用) |
| 40_iam | — | EC2/Lambda Role + Instance Profile | `ec2_instance_profile` |
| 50_eip | 10 | EIP + Route 53 A レコード | `eip_alloc_id`, `eip_public_ip`, `route53_zone_id` |
| 60_ec2 | 10, 40, 50 | LT × 3 + ASG × 3 (desired=0) | `lt_*`, `asg_names` |
| 70_lambda | 60 (`asg_names`) | scale_up/down Lambda + EventBridge | `lambda_arn`, `schedule_arn` |
| 80_cloudwatch | 60, 70 | Log groups + SNS Topic + Alarm | `sns_topic_arn` |

### 4.2 Lambda ZIP の事前ビルド

`70_lambda.ps1` の前に Lambda パッケージをビルドしておく必要があります：

```powershell
cd C:\Users\iii03\block-stacker\lambda
./build.ps1
# deploy/lambda_scheduler.zip が生成される（70_lambda.ps1 が拾います）
```

### 4.3 EC2 起動シーケンス（user-data の中身）

`./60_ec2.ps1` までで Launch Template ができても、ASG `desired=0` のため EC2 は起動しません。スケジュール起動 or 手動 scale-up の際、AL2023 AMI で以下が実行されます。

**デモ EC2 (`demo.sh`)** の起動フロー：

```
[1] dnf install docker awscli amazon-cloudwatch-agent
[2] systemctl enable --now docker
[3] private IP を SSM /bs/demo/private_ip に登録（配信 EC2 が読む）
[4] aws ecr get-login-password | docker login   ← Endpoint (ecr.api) 経由
[5] aws s3 sync s3://bs-app-*/world_state/ /opt/bs/state/   ← S3 Gateway 経由
[6] aws s3 sync s3://bs-app-*/models/      /opt/bs/models/
[7] aws s3 sync s3://bs-app-*/configs/     /opt/bs/configs/
[8] docker run ... block-stacker/demo ... ai_server      ← Endpoint (ecr.dkr) + S3 経由でレイヤ pull
[9] CloudWatch Agent 設定 → /var/log/userdata.log を集約   ← Endpoint (logs) 経由
[10] spot interrupt 監視 systemd service を常駐
```

**学習 EC2 (`learner.sh`)** も同様の流れで、`docker run ... mvp2.train` が走り、5 分毎に `aws s3 sync` で checkpoint を S3 に書き戻します。

> **オートカリキュラム（既定で有効）**: カリキュラムは `mvp2.train` の**デフォルト ON**
> （`Dockerfile.learner` の `CMD` でも明示）。Stage 1→5 を自動進行する。`--total-timesteps`=1M は
> **全ステージ合計の上限（グローバル予算）**で、**総手数は必ず 1M 以下**＝起動時間・コストが見積もれる。
> 各ステージは **散布0で即卒業**または**目標高さ到達の成功率 0.6**で卒業し、早く卒業した残りは次へ回る。
> 使い切ったら中断。成果は **最終 `sac_final.zip` のみ**（＋ checkpoints/）を S3 に保存。
> Stage 1 のみに戻すなら `CMD` に `--no-curriculum` を渡す（既定 ON なので `--curriculum` を外すだけでは無効化されない）。
> **デモ EC2 の `ai_server` は常に最終ステージ（全形状）でモデルを動かし**、既定モデルは
> `sac_final.zip`→`sac_stage1_final.zip` の順で自動選択する。
>
> **卒業条件はコンテナ環境変数で上書き可**（優先順位: env var > training.yaml > 既定）。
> ECS タスク定義 / `docker run -e` で渡す：
> | 環境変数 | 意味 | 既定 |
> |---|---|---|
> | `BS_GRADUATION_RATIO` | 目標高さ = 在庫満積み高さ × ratio | 0.6 |
> | `BS_GRADUATION_THRESHOLD` | 「目標高さ到達」の卒業成功率（散布0は別途・即卒業） | 0.6 |
> | `BS_GRADUATION_WINDOW` | 成功率を見る直近エピソード数 | 30 |
>
> 例: `docker run -e BS_GRADUATION_RATIO=0.7 -e BS_GRADUATION_THRESHOLD=0.8 ... block-stacker/learner`

**Endpoint 依存度:**
- `ecr.api` Endpoint → step [4] で必須
- `ecr.dkr` Endpoint → step [8] で必須
- S3 Gateway → step [5-7] と [8] のイメージレイヤ pull で必須
- `logs` Endpoint → step [9] 以降の CW Logs 書き込みで必須

これらが揃っていないと EC2 boot が途中で詰まります。`./10_network.ps1` 完了後に §5.0 で Endpoint 疎通を確認することを推奨。

---

## 5. 動作確認

### 5.0 VPC Endpoint の疎通確認（推奨、`./10_network.ps1` 直後に）

3 つの Interface Endpoint が `available` 状態になっているか確認：

```powershell
aws ec2 describe-vpc-endpoints `
    --filters "Name=vpc-id,Values=$((Get-Content deploy/state.json | ConvertFrom-Json).vpc_id)" `
    --query "VpcEndpoints[].{Service:ServiceName,State:State,DNS:PrivateDnsEnabled}" --output table
```

期待する出力（4 行）:

```
ServiceName                                    State       PrivateDnsEnabled
com.amazonaws.ap-northeast-1.s3                available   false   (Gateway: PrivateDNS は使わない)
com.amazonaws.ap-northeast-1.ecr.api           available   true
com.amazonaws.ap-northeast-1.ecr.dkr           available   true
com.amazonaws.ap-northeast-1.logs              available   true
```

`State=available` かつ Interface 3 つで `PrivateDnsEnabled=true` であれば、EC2 起動時の ECR pull / CW Logs 送信が VPC 内で完結します。

### 5.1 手動 scale-up + 接続テスト

```powershell
cd C:\Users\iii03\block-stacker\deploy
./90_verify.ps1
```

このスクリプトは以下を実行：

1. 全 ASG `desired_capacity=1` に
2. 3 台が running になるまで polling
3. インスタンス一覧表示
4. `wss://bs.example.com/` に Python テストクライアントで 15 秒接続

### 5.2 SNS に通知先メールを追加

```powershell
aws sns subscribe `
    --topic-arn (Get-Content deploy/state.json | ConvertFrom-Json).sns_topic_arn `
    --protocol email --notification-endpoint "you@example.com"
```

届いたメールから confirm リンクを踏むこと。

### 5.3 EventBridge スケジュール動作確認

手動 invoke で Lambda が動くか確認：

```powershell
aws lambda invoke --function-name bs-scale-up --payload '{}' /tmp/out.json
Get-Content /tmp/out.json
# 期待: {"scaled_up": [...]} または祝日なら {"skipped": true}

aws lambda invoke --function-name bs-scale-down --payload '{}' /tmp/out.json
Get-Content /tmp/out.json
```

### 5.4 Godot クライアントから接続

`client/scripts/ws_client.gd` の `server_uri` を `wss://bs.example.com/` に変更し、Godot で F5。

---

## 6. 運用

### 6.1 通常運用

EventBridge Scheduler 4 系統で、ASG ごとに別スケジュール：

| Scheduler | 発火 (UTC) | JST 時刻 | 対象 ASG | payload |
|----------|---------|---------|---------|---------|
| `bs-learner-start` | `cron(0 5 ? * SAT#2,SAT#4 *)` | 第 2/4 土曜 14:00 | bs-learner-asg | `{"asg_names": ["bs-learner-asg"]}` |
| `bs-learner-stop`  | `cron(0 13 ? * SAT#2,SAT#4 *)`| 第 2/4 土曜 22:00 | 同上 | 同上 |
| `bs-demo-start`    | `cron(0 5 ? * MON-FRI *)` | 月〜金 14:00 | bs-demo-asg + bs-streamer-asg | `{"asg_names": ["bs-demo-asg", "bs-streamer-asg"]}` |
| `bs-demo-stop`     | `cron(0 13 ? * MON-FRI *)`| 月〜金 22:00 | 同上 | 同上 |

Lambda は 1 ペア（`bs-scale-up` / `bs-scale-down`）を 4 スケジュールが共有し、payload で対象 ASG を切り替える設計。

祝日は `bs-scale-up` 内の `jpholiday.is_holiday()` で skip（学習側にも適用される。第 2/4 土曜が祝日になった場合はその週の学習はスキップ）。

**運用パターン:**
- 平日 14-22 JST: デモ + 配信のみ稼働、誰でも観られる時間帯
- 隔週土曜 14-22 JST: 学習のみ稼働（デモは停止）
- 学習で生成したモデルは S3 に保存、翌週月曜のデモ起動時に取り込まれる
- ローカル学習をメインにする場合、`bs-learner-*` の Scheduler を無効化または削除可（学習 EC2 は手動 invoke で気が向いたタイミングだけ起動）

### 6.2 設定変更を反映

```powershell
# configs/training.yaml を変更したあと
aws s3 cp configs/training.yaml s3://bs-app-$ACCOUNT/configs/training.yaml
# 次回起動時にユーザーデータの aws s3 sync で読み込まれる
# 即時反映が欲しい場合は ASG を一度 desired=0 → 1 で再起動
```

### 6.3 学習モデル更新

```powershell
# ローカルで再訓練
uv run python -m block_stacker.mvp2.train --total-timesteps 100000 --n-envs 4

# S3 にアップロード
aws s3 cp output/mvp2/sac_final.zip s3://bs-app-$ACCOUNT/models/latest.pt
# デモ EC2 は次回 collapse 時 (or 再起動時) に S3 からモデル再ロード
```

### 6.4 手動停止 / 再開

```powershell
# 即停止
foreach ($n in "bs-streamer-asg", "bs-demo-asg", "bs-learner-asg") {
    aws autoscaling update-auto-scaling-group --auto-scaling-group-name $n --desired-capacity 0
}

# 即起動
foreach ($n in "bs-streamer-asg", "bs-demo-asg", "bs-learner-asg") {
    aws autoscaling update-auto-scaling-group --auto-scaling-group-name $n --desired-capacity 1
}
```

---

## 7. 削除

```powershell
cd C:\Users\iii03\block-stacker\deploy
./99_destroy.ps1
# プロンプトで "destroy" と打って確認
```

このスクリプトは state.json の逆順でリソースを削除します。S3 バケットだけは中身もろとも消すのは危険なので手動指示が出ます：

```powershell
aws s3 rm s3://bs-app-$ACCOUNT --recursive
aws s3api delete-bucket --bucket bs-app-$ACCOUNT
```

---

## 8. トラブルシューティング

| 症状 | 確認 | 対処 |
|---|---|---|
| Spot で c6a が取れない | `aws ec2 describe-spot-price-history --instance-types c6a.4xlarge --max-results 5` | `common.ps1` の `LearnerFallback` (c6i / c7a / m6a) が自動 fallback。さらに別タイプを追加可 |
| **EC2 で `docker login` が失敗 / timeout** | `aws logs tail /aws/ec2/bs-demo --since 10m`（CW Logs 経由） or SSM Session で `/var/log/userdata.log` | (1) `./10_network.ps1` で endpoint 3 個が available か（§5.0 参照） (2) `vpce` SG の inbound 443 が VPC CIDR から許可されているか (3) `PrivateDnsEnabled=true` か |
| **EC2 で `docker pull` が `manifest unknown`** | ECR コンソールでイメージタグ確認 | §3.2 の build/push を再実行。`block-stacker/demo:latest` と `block-stacker/learner:latest` が両方 push されているか |
| **CloudWatch Logs に何も流れない** | EC2 内で `journalctl -u amazon-cloudwatch-agent` | `logs` Interface Endpoint が available か。Endpoint 無いと CW Agent が静かに失敗 |
| Endpoint の DNS 解決失敗（`ssm-agent` 等が timeout） | EC2 内で `nslookup ecr.ap-northeast-1.amazonaws.com` | `PrivateDnsEnabled` を true にし忘れていると Public エンドポイントを引いてしまい IGW 不在で詰む。`aws ec2 modify-vpc-endpoint --vpc-endpoint-id ... --private-dns-enabled` |
| Caddy が TLS 取得失敗 | `aws logs tail /aws/ec2/bs-streamer --filter-pattern acme --since 10m` | Port 80 が 0.0.0.0/0 から到達可、A レコードが EIP を指す |
| WebSocket 接続できない | `curl -v https://bs.example.com/` | Caddy 起動、Demo SG の 8765 inbound、SSM `/bs/demo/private_ip` |
| Lambda が動かない | `aws logs tail /aws/lambda/bs-scale-up --since 1h` | jpholiday import、ASG_NAMES env var |
| state.json と AWS が乖離 | `aws ec2 describe-vpcs --filters Name=tag:Project,Values=block-stacker` 等で確認 | 該当 step スクリプトを再実行（冪等） |
| S3 同期で permission denied | `aws sts get-caller-identity` で実行ロール確認 | IAM ロールで `bs-app-*` パスに access あるか |

### よくある詰まりどころ

- **PowerShell 5.1 だと動かない**: PowerShell 7 (`pwsh`) を使う。`ConvertFrom-Json -AsHashtable` が必要。
- **EIP 関連付け失敗**: IAM の `bs-ec2-role` に `ec2:AssociateAddress` がある（`40_iam.ps1` で付与済）。
- **Lambda jpholiday ImportError**: `lambda/build.ps1` で再ビルド → `70_lambda.ps1` で update-function-code。
- **Endpoint 作成直後の EC2 起動で ECR pull がタイミング的に失敗**: Interface Endpoint の DNS 伝搬に 1〜2 分かかる。`./10_network.ps1` 直後すぐに `./60_ec2.ps1` を実行する場合、ASG `desired=1` は後で（§5.0 確認後に）行う。
- **`vpce` SG の 443 inbound を VPC CIDR で許可していない**: Endpoint への DNS は引けても TLS が握れずタイムアウト。`10_network.ps1` で自動付与しているが、手動で SG を編集してしまうと壊れる。

---

## 付録 A. deploy/ のディレクトリ構成

```
deploy/
├── common.ps1              ← 全 step が読む。リージョン・ドメイン・state I/O
├── state.json              ← 実行時に自動生成 (リソース ID 一覧)
├── 10_network.ps1          ← VPC / subnets / SG / IGW / S3 Gateway / ECR・Logs Interface Endpoints
├── 20_s3.ps1               ← S3 バケット + バージョニング + 暗号化 + configs upload
├── 40_iam.ps1              ← EC2 / Lambda / Scheduler の IAM ロール
├── 50_eip.ps1              ← EIP + Route 53 A レコード
├── 60_ec2.ps1              ← Launch Templates + ASG x3
├── 70_lambda.ps1           ← Lambda 2 個 + EventBridge スケジュール
├── 80_cloudwatch.ps1       ← Log groups + SNS topic + Alarm
├── 90_verify.ps1           ← 手動 scale-up + 接続テスト
├── 99_destroy.ps1          ← 逆順削除（要確認入力）
├── lambda_scheduler.zip    ← 70_lambda.ps1 がビルドして配置
└── userdata/
    ├── streamer.sh         ← <<KEY>> プレースホルダを 60_ec2 が置換
    ├── demo.sh
    └── learner.sh
```

---

## 付録 B. リソース ID の手動確認

`state.json` を直接見る：

```powershell
Get-Content deploy/state.json | ConvertFrom-Json -AsHashtable
```

特定キーだけ：

```powershell
(Get-Content deploy/state.json | ConvertFrom-Json -AsHashtable).private_subnet_id
```

AWS 側からも確認：

```powershell
# このプロジェクトのリソース一覧
aws resourcegroupstaggingapi get-resources `
    --tag-filters "Key=Project,Values=block-stacker" `
    --query "ResourceTagMappingList[].ResourceARN" --output text
```

---

## 付録 C. コスト管理

### Budgets でアラート

```powershell
$ACCOUNT = aws sts get-caller-identity --query Account --output text
$budget = @'
{"BudgetName":"bs-monthly","BudgetLimit":{"Amount":"70","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST"}
'@
$tmp = New-TemporaryFile; Set-Content $tmp -Value $budget -Encoding utf8
aws budgets create-budget --account-id $ACCOUNT --budget "file://$($tmp.FullName)"
Remove-Item $tmp
```

想定 $51 (¥7,650) に対し $70 (¥10,500) で警告。

### 内訳

為替前提: 1 USD = ¥150（円換算の参考値、実請求は時点レート × USD で確定）。

稼働時間: 学習 16h/月（隔週土曜 14-22 JST）、デモ + 配信 176h/月（平日 14-22 JST、祝日除く）

| 項目 | USD/月 | ¥/月 |
|---|---|---|
| 学習 c6a.4xlarge Spot (16h × $0.20) | $3.2 | ¥480 |
| デモ c6i.xlarge Spot (176h × $0.07) | $12.3 | ¥1,848 |
| 配信 t4g.small Spot (176h × $0.007) | $1.2 | ¥185 |
| EBS gp3 180GB（稼働時間プロレート） | $2.1 | ¥314 |
| ECR Interface Endpoint × 2 + Logs × 1 | $22 | ¥3,300 |
| EIP (アイドル課金、配信停止時間 544h) | $2.7 | ¥408 |
| Route 53 hosted zone | $0.5 | ¥75 |
| S3 + CloudWatch Logs | $1.7 | ¥255 |
| データ転送（視聴時間 2.6 倍想定） | $6 | ¥900 |
| **合計** | **約 $51** | **約 ¥7,765** |

旧スケジュール（土日 14-22 で全 ASG 一括）から **学習 1/4、デモ+配信 2.6 倍** に再配分した結果。
月額はほぼ据置だが、視聴機会が **2.6 倍** に増加。学習頻度は減るので、ローカルでの学習併用が前提。

> 注: Interface Endpoint 3 個（ecr.api + ecr.dkr + logs）は **稼働時間外も 24/7 課金される**ため、月額の約 43% を占める最大費用項目。設計上の意図的な選択（付録 E §3 参照）。
> 将来コストを更に下げたい場合は (a) Public Subnet 移動で Endpoint 撤去（-$22/月、SG で防御）、(b) `logs` だけ捨てて CloudWatch ログを諦める（-$7/月）、(c) Endpoint を稼働時間限定で動的 create/destroy（Lambda 拡張、-$18/月） の選択肢があるが、今回は安全性と簡潔さを優先して常設。

---

## 付録 D. Redis を再導入すべき条件

現状は Redis 不使用（S3 で世界状態と model checkpoint を永続化、WebSocket セッションは ai_server in-process）。
**以下のいずれかが発生したら** ElastiCache Redis または Redis on EC2 の追加を検討：

1. **同時接続クライアント数が 15 を超える**
   `src/block_stacker/streaming/server.py` のレビューノートで言及。SFU 化または Redis pub/sub による fan-out が必要になる。

2. **デモ EC2 を複数台 (ASG max_size > 1) で運用する**
   現状 `max_size = 1` 固定。複数化するなら WebSocket セッション情報を Redis に外出しする必要がある。

3. **学習→推論のモデル更新を 5 分以内のレイテンシで行いたい**
   現状は S3 経由で 5 分毎 sync。online learning 的に 1 分以内で反映したいなら Redis pub/sub で通知 + メモリ上スワップ。

4. **観戦統計・ランキング等のリアルタイム共有状態が必要になる**
   視聴者数カウンタ、最高タワー高ランキング等。CloudWatch Custom Metrics でも代替可能だが、ms オーダーの read が必要なら Redis。

5. **マルチリージョン展開**
   ap-northeast-1 限定設計を崩す時。Global Datastore で state を共有する場合。

### 再導入手順の概要

- `infra-terraform/redis.tf` を git history から復元、または以下を新規作成:
  - `aws_elasticache_subnet_group`、`aws_elasticache_cluster` (cache.t4g.micro)
  - `aws_security_group "redis"`（6379 from streamer + demo）
- `aws_ssm_parameter "/bs/redis_endpoint"` を作成、demo.sh から `aws ssm get-parameter` で動的取得
- `pyproject.toml` に `redis-py` 追加、`ai_server.py` で接続

---

## 付録 E. 主要な設計上の決定

このプロジェクトでは複数の設計トレードオフを検討した結果、以下を採用しています。後から「なぜこうなっているのか」を辿るための記録。

### 1. 強化学習アルゴリズム: SAC + 3 層記憶アーキテクチャ

設計過程で SAC → PPO に一度切り替えたが、「**子供っぽい記憶のダイナミクス**」を
表現するために最終的に SAC に戻し、独自の記憶機能を上乗せした。**「人間の記憶
構造をそのまま AI に持たせる」**という発想で、3 層の記憶を組み合わせている。

#### 3 層記憶アーキテクチャ

```
┌──────────────────────────────────────────────────────┐
│ 1. 勘  =  ニューラルネットの重み                          │
│           (体に染み込んだ感覚)                            │
│   実装: HybridFeatureExtractor + SAC policy/value heads │
│   特徴: 約 75 万パラメータに体験が圧縮される              │
│         明示的に「思い出す」ものではない                    │
└──────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────┐
│ 2. 短期記憶  =  観測辞書に直近 N 手の履歴を同梱            │
│              「**さっき**何をやったか」を鮮明に保持        │
│   実装: env/env.py の `_stm_*` deque                    │
│         observation["recent_actions"] 等                │
│         policy/feature_extractor.py の STM ストリーム    │
│   保持: action / reward / result_score / mask           │
│   保持期間: 1 エピソード内（reset でクリア）              │
│   重みづけ: なし（直近 5 手は等しく扱う）                 │
└──────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────┐
│ 3. 長期記憶  =  WeightedReplayBuffer                    │
│              「何日も前のあの崩落」を覚えている            │
│   実装: policy/weighted_replay_buffer.py                │
│   保持: (state, action, reward, next_state) +           │
│         初期重み + 生まれた時刻                          │
│   保持期間: エピソード横断、最大 buffer_size 件           │
│   重みづけ: イベント種別で初期値が違う、時間で減衰         │
└──────────────────────────────────────────────────────┘
```

#### 長期記憶 (WeightedReplayBuffer) の 5 つの重みづけ仕様

| # | メカニズム | 内容 | 設定キー |
|---|--------|------|--------|
| 1 | **イベント種別の初期重み** | 崩落 1.0 > 失敗 0.7 > 新記録 0.5 > 成功 0.3 > 無駄手 0.1 | `memory_system.initial_weights` |
| 2 | **直前タワー高さ補正** | 初期重み ×= clip(1 + coef×(height_before/reference), 1, max_factor)。高いタワーで起きた経験ほど強い記憶に | `memory_system.height_weighting` |
| 3 | **時間で薄れる重み** | 1 step ごとに ×0.9999、半減期 ~6,900 step | `memory_system.decay_rate` |
| 4 | **重みつき抽選** | 学習時、重みに比例した確率で記憶を引く | （sampling ロジック） |
| 5 | **重みで変わる読み出しブレ** | blur = max_blur × (1 - 現在重み) | `memory_system.recall_noise.coordinate_sigma` |

重み 2（高さ補正）は重み 1（event 別初期重み）に**掛け算**で乗るので、event 種別の相対順位
（崩落>失敗>…）は保たれたまま、高いタワーでの経験だけが底上げされる。`enabled: false` で
従来（event 種別のみ）に戻せる。`reference` は典型的な到達高さの目安（既定 0.10）。
env.py が `info["height_before"]`（行動前タワー高さ）を出し、buffer の `add()` で適用する。

#### Event type の判定（env.py で実装）

ステップごとに以下の優先順位で 1 つの event_type を決定し、info dict に乗せる：

| 優先度 | Event | 条件 | 初期重み |
|------|-------|------|--------|
| 1 (最高) | `collapse` | 崩落判定が成立 | 1.0 |
| 2 | `failure` | 進歩なし truncate (max_actions_without_progress 到達) | 0.7 |
| 3 | `height_record` | タワー高さの新記録 | 0.5 |
| 4 | `success` | placement 成功（記録更新せず） | 0.3 |
| 5 | `no_progress` | 上記いずれにも該当しない | 0.1 |

#### 子供メタファーとの対応

| メタファー | 実装層 |
|----------|------|
| 体に染み込んだ感覚（明示的に思い出せない） | 1. 勘 |
| 「**ついさっき**こうしたら失敗したな」 | 2. 短期記憶 |
| 「**何日も前のあの強烈な崩落**を覚えてる」 | 3. 長期記憶 |
| 強烈な体験ほど鮮明に残る | 3 の重み 1 |
| 「**高く積めた時のこと**ほどよく覚えてる」 | 3 の重み 2 |
| 古い記憶はだんだん曖昧になる | 3 の重み 3 + 5 |
| 強い記憶ほどよく思い出す | 3 の重み 4 |

#### PPO 経由の設計探索の意義

PPO に一度切り替えた経緯は無駄ではなく、その過程で **「短期記憶を観測に明示的に
入れる」**というアイデアが固まった。最終的に SAC + 長期記憶 + 短期記憶 という
3 層構成に統合された。

#### 関連ファイル

| ファイル | 役割 |
|--------|------|
| `src/block_stacker/policy/weighted_replay_buffer.py` | 重みつき長期記憶（カスタム実装） |
| `src/block_stacker/policy/feature_extractor.py` | NN（勘 + 短期記憶処理） |
| `src/block_stacker/env/env.py` | 短期記憶 deque, event_type 判定 |
| `src/block_stacker/env/observation.py` | 観測 dict の組み立て |
| `src/block_stacker/mvp3/ai_server.py` | 推論時の `ShortTermMemory` クラス |
| `configs/training.yaml` | `sac:` + `memory_system:` + `short_term_memory:` |

### 1.1 ブロック形状とカリキュラム

積み木は **4 形状** をサポート（[`configs/world.yaml`](../configs/world.yaml)）：

| 形状 | shape.type | dims | 特徴 |
|------|----------|------|------|
| **立方体** (cube) | `box` | `[w, h, d]` | 最も基本、回転対称 |
| **直方体** (cuboid) | `box` | `[w, h, d]` (非等比) | 縦置き / 横置きで安定性が変わる |
| **直角二等辺三角柱** (triangular_prism) | `triangular_prism` | `[leg_length, prism_length]` | 平面が多く安定だが斜面あり、向き重要 |
| **円柱** (cylinder) | `cylinder` | `[radius, height]` | 転がるので最も扱いが難しい |

実装メモ:
- 三角柱は PyBullet 組込プリミティブが無いので、`GEOM_MESH` で凸包メッシュとして実装
  （`sim/blocks.py` の `_triangular_prism_vertices/_indices`）
- 安定姿勢は **y-leg 面（leg-rectangle）が下**。直角コーナーは下辺、斜面は上
- centroid は幾何中心からずれる（local z 範囲 `[-leg/3, +2 leg/3]`）ため、
  spawn 時の `_spawn_height` は `leg/3` を返す

#### カリキュラム順序（5 ステージ）

形状を **難易度順に段階追加**:

目標高さは固定値ではなく「在庫満積み高さ × `graduation.ratio`（既定 0.6）」で動的算出。

| Stage | 形状 | 在庫（合計） | 目標高さ（満積み×0.6） | 難易度ポイント |
|------|------|------|--------|------------|
| 1 | cube | 8（8） | 0.240 m | ウォームアップ |
| 2 | cube | 15（15） | 0.450 m | スケール拡大 |
| 3 | cube + cuboid | 8 + 7（15） | 0.408 m | 2 形状目（向きが意味を持つ） |
| 4 | + **triangular_prism** | 5 + 5 + 5（15） | 0.420 m | 3 形状、平面ベースで扱いやすい |
| 5 | + **cylinder** | 4 + 4 + 4 + 3（15） | 0.444 m | 4 形状、**最難（円柱は転がる）** |

> 在庫（合計最大15個）は観測枠 `max_blocks=8` より多い。観測は「拾える散布ブロックの近い順
> トップ8」だけを映し（積まれた分は heightmap 表現）、合計が枠を超えても破綻しない設計
> （= 子供の狭い視野。設計書 §観測空間 を参照）。

順序の根拠: **「平面で安定する形状 → 転がる形状」**。円柱を最後にすることで、
カリキュラム後半で「これまで使ってきた形状の上に転がりやすい円柱を載せる」という
高度な戦略が求められるようになる。

### 2. 学習インスタンス: GPU → CPU

| 項目 | 採用前 (g4dn.4xlarge, T4) | 採用後 (c6a.4xlarge, AMD EPYC) |
|------|--------------------------|------------------------------|
| 月額 (68h) | ¥3,600 | ¥2,040 |
| 物理コア | 8 + T4 GPU | 8 |
| ボトルネック | PyBullet (CPU) | PyBullet (CPU) |
| GPU 使用率 | 5〜15%（過剰投資） | — |

**判断理由:** NN が小さく（数万〜十万パラメータ）、PyBullet 物理シムが CPU bound のため GPU が無意味。
**復活条件:** heightmap 解像度大幅増、Set Transformer の depth/heads 倍増、画像観測導入 等で NN が重くなったら再評価。

### 3. Private Subnet 接続: NAT Gateway 不採用 → Interface Endpoint 採用

| 選択肢 | 月額 | 採否 |
|--------|------|------|
| NAT Gateway 1 個 | $35 + データ転送料 | ❌ 高すぎる |
| Interface Endpoint × 3 (ecr.api + ecr.dkr + logs) | $22 | **✅ 採用** |
| EC2 を Public Subnet に移動 (SG で防御) | $1 (Public IPv4 課金のみ) | ❌ 不採用（後述） |
| Endpoint を稼働時間限定で create/destroy | $4 + Lambda 複雑度 | ❌ 自動化コストが利得を超える |

**判断理由:** Private Subnet を維持しつつ最小コストで AWS API に到達できる選択肢。SG ベースの防御に加えて「外部到達経路そのものが無い」という二重防御を取った。
**コスト感:** 全体の 43% を占める最大費用だが、月額 ¥3,300 で運用シンプル性を買っていると解釈する。

### 4. ElastiCache Redis: 撤去

| 項目 | 撤去前 | 撤去後 |
|------|--------|--------|
| 月額 | ¥2,460 (24/7) | ¥0 |
| 実装での使用 | 無し（コードに `import redis` 無し） | 無し |
| 設計上の役割 | 図にはあったが実体無し | 不要 |

**判断理由:** 構成図と実装の乖離。世界状態と model checkpoint は S3 で永続化済み、WebSocket セッションは in-process で管理。
**再導入条件:** 付録 D 参照。

### 5. 配信は EC2 + Caddy（ALB / API Gateway 不採用）

| 選択肢 | 月額 | 採否 |
|--------|------|------|
| EC2 + Caddy (現状) | t4g.small Spot $0.50 + EIP | **✅ 採用** |
| ALB | $18 (低トラフィックでも固定) | ❌ 過剰 |
| API Gateway WebSocket | $1/M msg + データ転送 | ❌ 低頻度なら安いが、connection 制約と Caddy 自動 TLS の手軽さに勝てず |

**判断理由:** トラフィックが少なく（同接 1〜5 人想定）、固定費の安いセルフホスト Caddy で TLS まで自動化できる。

### 6. クライアント: Godot 4.4.1 .NET + C# 単一ファイル

#### アーキテクチャ

```
[視聴者の PC]
   ↓ ws://bs.example.com/ (TLS)
[配信 EC2: Caddy] → reverse_proxy → [デモ EC2: ai_server :8765]
                                       ↓ 物理シム
                                    [PyBullet (Z-up)]

サーバ側（PyBullet）の (x, y, z) は Z-up。
WsClient.cs が ReadPoseTransform で Godot Y-up に変換: (x, y, z) → (x, z, -y)、quat も同等。
```

#### 単一ファイル設計

| ファイル | 行数 | 責務 |
|--------|----|------|
| [`client/scripts/WsClient.cs`](../client/scripts/WsClient.cs) | 約 400 | WebSocket 接続 + プロトコルデコード + Mesh 構築 + 描画 + UI |
| [`client/scenes/main.tscn`](../client/scenes/main.tscn) | 約 25 | Node3D + Camera + DirectionalLight + WorldEnvironment |
| [`client/block-stacker-client.csproj`](../client/block-stacker-client.csproj) | 約 10 | `Godot.NET.Sdk/4.4.1`, .NET 8 ターゲット |

#### 主要機能

| 機能 | 実装 |
|------|------|
| WebSocket 接続 | `WebSocketPeer`, auto-reconnect 2秒、Open 後に hello 送信 |
| プロトコルデコード | 7 メッセージ種別: WORLD_CONFIG / INITIAL_STATE / SNAPSHOT / SLEEP / WAKE / HEARTBEAT / COLLAPSE |
| 4 形状描画 | `BoxMesh`（cube/cuboid）, `CylinderMesh`（Y軸補正済み）, `ArrayMesh`（三角柱、サーバと同じ頂点） |
| 地面描画 | `BoxMesh` 3m × 0.02m × 3m, 灰色 |
| マテリアル | `StandardMaterial3D`、`AlbedoColor` でブロック色、roughness 0.6 |
| 環境光 | `Environment` で ambient 0.5 を加えて暗い面も視認可能に |
| 座標系変換 | `ReadPoseTransform` で Z-up→Y-up、cylinder のみ追加 +90° X 軸回転 |
| 接続状態 UI | `CanvasLayer` + `Label`、未接続時に「サーバとの通信を試行中...」アニメ表示 |

#### 判断ポイント

| 観点 | 採用 | 不採用 |
|------|------|--------|
| 言語 | C# | GDScript（最初は GDScript で書いたが C# 化を選択） |
| Godot 版 | .NET 版 4.4.1 | 標準 Godot 版 |
| 描画 | MultiMesh (per-shape) | MeshInstance3D 個別生成（数十のノード爆発を避ける） |
| 三角柱 Mesh | 自前 ArrayMesh | PrismMesh（中心がずれる、PyBullet と頂点不一致）|
| 円柱の軸補正 | per-instance Transform | 自前 ArrayMesh（per-instance のほうが簡潔） |

---

## 付録 F. 今後の検討事項（未実装アイデア集）

設計議論の中で出てきたが、現時点では実装していないアイデアを記録しておく。
**「今は不要だが、将来必要になったら参照する」**ためのバックログ。

### F.1 記憶バッファの永続化（save_replay_buffer / load_replay_buffer）

**何ができるか:** 長期記憶（WeightedReplayBuffer）の中身を pickle で S3 に保存し、
次回起動時に復元する。「**先週の続きの子供**」として AI を再開できる。

**期待効果:**
- 週末稼働ごとに記憶リセットされる現状から、**累積成長する AI** に進化
- Spot 中断時の記憶復旧
- 配信視聴者にとっての「**だんだん上手くなる**」感の演出強化

**実装規模:** SB3 標準の `save_replay_buffer()` / `load_replay_buffer()` が使える。
追加コード約 25〜50 行（圧縮対応含めて）。

**ファイルサイズ目安:** 50,000 件で約 1.7 GB（gzip 圧縮で 500 MB 程度）。
S3 ストレージコスト微少、転送料も微少。

**設計判断ポイント:**
- 保存頻度: 学習終了時のみ / 5 分毎 / Spot 中断時
- ストレージ: S3 (`bs-app-*/replay_buffers/`)
- バッファサイズが大きいので、容量や heightmap 解像度の見直しも検討余地

**関連議論:** 「**この長期記憶って保存できる？**」（チャット履歴）

---

### F.2 起動オフ期間中の重み減衰（sleep decay）

**何ができるか:** EC2 が止まっている時間（金曜夜〜土曜昼など）も、**現実時間の
経過に応じて長期記憶の重みを減衰**させる。

**現状の挙動:**
- 重み減衰は「ステップ数ベース」（global_step ベース）
- EC2 が止まっている間は時間が止まる
- リアルじゃない（「**休んでも記憶は何時間か薄れる**」が表現できない）

**提案する方式:**
- `WeightedReplayBuffer` に `last_save_timestamp` を持たせる
- ロード時に経過時間（Unix epoch 差分）から sleep_decay を計算
- 全エントリの重みを一括減衰（実装的には `global_step` を進めるだけ）

**設定例:**
```yaml
memory_system:
  decay_rate: 0.9999          # 学習中の 1 step あたり
  sleep_decay_per_hour: 0.99  # 停止中の 1 時間あたり
```

**実装規模:** 約 25 行（F.1 の永続化と同時実装が自然）。

**子供メタファー的意義:** 「**人間の脳科学的にも正しい設計**」
- 起きてる時は普通に時間経過で記憶が薄れる
- 寝てる間も、ゆっくり薄れる（強烈な記憶ほど残る）
- 重要なイベントは何ヶ月経っても完全には消えない

**設定の応用:**
- イベント種別ごとに `sleep_decay_per_hour` を変えると、「**怖い夢で何度も思い出
  すから、休んでも忘れにくい崩落**」みたいなノリで「**睡眠中の記憶定着**」
  （メモリ・コンソリデーション）も模擬できる

**関連議論:** 「**記憶の重み減衰はタイムスタンプとかを用いて...**」（チャット履歴）

---

### F.3 関数近似器を MLP → LSTM / Transformer へ拡張

**何ができるか:** 現状は MLP（+ Set Transformer + CNN）で観測を処理しているが、
長い履歴を扱うために RNN 系（LSTM/GRU）や Transformer に置き換える。

**3 つのアプローチ:**

| 案 | 規模 | 効果 |
|---|------|------|
| A. `stm_proj` だけ LSTM に置き換え | 数十行 | 弱（5 手では LSTM の真価が出ない） |
| B. 観測を時系列化 + 真の recurrent SAC | 数百〜数千行 | 強だが大改修 |
| C. PPO に戻して `sb3_contrib.RecurrentPPO` | 中規模 | LSTM 入るが SAC 系の記憶機能を失う |

**現状の判断:** 不要。短期記憶（直近 5 手）+ 重みつき長期記憶で十分機能する見込み。

**いつ検討するか:**
- 「直近 5 手では足りない、もっと長い依存関係を扱いたい」と感じた時
- ステージが進んで（8 cube 以上）episode 内 step 数が大きく増えた時

**関連議論:** 「**後から MLP から LSTM に切り替えることは可能？**」「**昨今は RL と
LSTM を組み合わせるのが一般的？**」（チャット履歴）

---

### F.4 Decision Transformer 系の導入（オフライン RL ハイブリッド）

**何ができるか:** PPO/SAC で大量に集めたデータを使って、後段で Transformer を
教師あり学習（return-to-go を条件とした sequence modeling）。

**期待効果:**
- 長期文脈を踏まえた判断ができる Transformer 系の AI に進化
- 「子供が成長して、経験を整理した**落ち着いた大人**」のフェーズに移行する演出

**コスト:**
- 学習データが 30M timesteps クラス必要（現状の 10〜100 倍）
- DT 学習用の別パイプライン必要（HuggingFace Transformers or d3rlpy）
- 推論サーバの autoregressive 化が必要

**判断:** **当面不要**。SAC + 3 層記憶で「子供が学ぶ」フェーズを十分表現できる。
DT は「子供 → 大人」の演出が欲しくなったタイミングで検討。

**関連議論:** 「**PPO でデータを収集したのちに transform に切り替えるのはあり？**」
（チャット履歴）

---

### F.5 短期記憶側の重みづけ

**何ができるか:** 直近 5 手の中で、重要なイベント（崩落、新記録）を **より長く保持
する**仕組み。現状は単純 FIFO（5 手で押し出される）。

**実装方針:**
- 直近 5 手の枠を「**FIFO 3 枠 + 重要記憶優先 2 枠**」のように分割
- 重要記憶優先枠は重要度の高い順に保持、押し出されにくい

**評価:** 凝りすぎ。長期記憶側で重要度が反映されるので、短期記憶は単純 FIFO で OK
というのが現状の判断。

**関連議論:** 「**短期記憶にも重みづけしたい場合**」（付録 E §1）

---

### F.6 観測（state）にもブレを乗せる

**何ができるか:** 現状の recall ノイズは action だけに乗せている。観測（盤面情報）
にもブレを加えると、「**過去のあやふやな記憶**」がより人間的になる。

**メリット:** より子供っぽい挙動。
**デメリット:** 状態遷移 (s, a) → s' の整合性が壊れて、Q 学習が不安定化する可能性。

**判断:** リスク高、効果不明確なので **不採用**。研究目的なら試す価値あり。

---

### F.7 重みの可視化 / TensorBoard ロギング

**何ができるか:** バッファ内の重み分布、イベント種別ごとのカウントなどを定期的に
TensorBoard に出力。学習挙動のデバッグや、配信での演出データに使える。

**実装規模:** 軽量、約 30〜50 行。
**判断:** 学習を本格運用する段階で検討。

---

### F.8 重要度別の減衰速度（イベント別 decay_rate）

**何ができるか:** イベント種別ごとに減衰速度を変える。「崩落は薄れにくい、無駄手は
すぐ消える」を明示的に表現。

**設定例:**
```yaml
memory_system:
  decay_rate_per_event:
    collapse: 0.99995      # ゆっくり減る
    failure: 0.9999
    success: 0.9999
    no_progress: 0.999     # 速く消える
```

**判断:** 初期重みで差をつけることで実質同等の効果が得られるので、現状は **不採用**。
重みの差が物足りなくなったら導入を検討。

---

### F.9 学習頻度の変更（隔週学習など）

**何ができるか:** 現状の「土日 14:00〜22:00 毎週」を「隔週」または「月 1 回」に
変更。学習コストを半減〜1/4 にする。

**前提:** F.1（記憶永続化）が実装されていれば、間が空いても学習を引き継げる。
**判断:** F.1 が実装された後の運用判断として検討。

---

### F.10 SFU 化 / マルチ視聴者対応

**何ができるか:** WebSocket の fan-out 効率を上げ、同接 15+ に対応。

**いつ検討するか:** 視聴者が増えてからで OK。設計議論の付録 D（Redis 復活条件）
にも記載済み。

---

### 検討事項のマトリクス（実装優先度 vs 効果）

```
              低コスト                      高コスト
            ←────────────────────────────────→
       ┌───┬─────────────────────────────────┐
   高  │ F.1 永続化 ★★ │              F.4 DT 移行   │
       │ F.2 sleep decay★ │                          │
   効  ├─────────────────┼─────────────────────────┤
   果  │ F.7 ログ      │ F.3 LSTM 化           │
       │              │                          │
   低  │ F.5 短期重み │ F.6 観測ブレ ❌         │
       │ F.8 個別減衰 │                          │
       └─────────────────┴─────────────────────────┘
   ★★ = 次に実装するなら最有力候補（F.1 + F.2 をセットで）
   ★  = 単体でも価値あり
   ❌  = 試さない方が安全
```

**次回検討するなら:** **F.1（記憶永続化）+ F.2（sleep decay）のセット実装** が
最も自然な拡張。コストも小さく、子供メタファーとの相性も抜群。

---

## 付録 G. ローカル開発ワークフロー

クラウドデプロイの前後で「**手元で動くものを確認したい**」「**AI の成長過程を観察したい**」
ための workflow をまとめる。詳細は [`docs/local_demo.md`](local_demo.md) 参照。

### G.1 学習を回す（試運転モード）

```powershell
# 6 物理コアの PC を想定（i7-10750H 等）
.venv\Scripts\python.exe -m block_stacker.mvp2.train --n-envs 6 --total-timesteps 100000
```

- `--n-envs 6`: 物理コア数に合わせる（クラウドは 8、ローカルは 4〜6）
- `--total-timesteps 100000`: 約 25 分で 5 本の checkpoint（20/40/60/80/100% 地点）
- `output/mvp2/checkpoints/sac_<N>_steps.zip` が `total_timesteps` の等分地点（`checkpoint_splits=5`）で保存（ステージ番号はファイル名に含まれない）

### G.2 TensorBoard で学習曲線を見る（別ターミナル）

```powershell
.venv\Scripts\python.exe -m tensorboard.main --logdir output\mvp2\tb
# ブラウザで http://localhost:6006 を開く
```

### G.3 Godot クライアントで AI の動きを見る

```powershell
# 1. サーバ
.venv\Scripts\python.exe -m block_stacker.mvp3.ai_server `
    --model output\mvp2\sac_final.zip --host 127.0.0.1

# 2. Godot
& "D:\Godot_v4.4.1-stable_mono_win64\Godot_v4.4.1-stable_mono_win64.exe" `
    --path C:\Users\iii03\block-stacker\client res://scenes/main.tscn
```

### G.4 checkpoint 比較で「成長」を観察

[`tools/demo_checkpoints.ps1`](../tools/demo_checkpoints.ps1) を使うと、複数 checkpoint を
切り替えて再生できる：

```powershell
# 対話モード: 番号を選んで一つだけ再生
tools\demo_checkpoints.ps1

# Auto モード: 全 checkpoint を 30 秒ずつ自動再生（成長過程を 10〜30 分で見られる）
tools\demo_checkpoints.ps1 -Mode auto -Seconds 30

# Godot 起動も込み
tools\demo_checkpoints.ps1 -Mode auto -Seconds 30 -LaunchGodot
```

### G.5 ローカルモデルをクラウドへアップロード

ローカルで学習させた良いモデルをクラウドのデモで使いたい場合：

```powershell
$ACCOUNT = aws sts get-caller-identity --query Account --output text
aws s3 cp output\mvp2\sac_final.zip s3://bs-app-$ACCOUNT/models/latest.pt
```

→ 次回クラウドデモ起動時に S3 から自動取得して使用される。

### G.6 ローカル vs クラウドの役割分担

| 場面 | ローカル | クラウド |
|------|--------|--------|
| アルゴリズム実験 | ◎ 主力 | △ |
| ハイパラ調整 | ◎ tensorboard で即確認 | △ |
| Checkpoint 比較・成長観察 | ◎ tools/demo_checkpoints.ps1 | △ |
| 長時間連続学習 | △ 電気代・熱 | ○ 隔週土曜 8h を自動化 |
| 一般視聴者向け配信 | × wss / TLS が無い | ◎ Caddy で配信 |
| デモの長時間運転 | △ | ◎ 平日 8h × 22 日 = 176h/月 |

→ **「ローカルで仕込んだモデル」をクラウドで配信**、というのが基本パターン。

---

## 完了チェックリスト

### 事前準備

- [ ] AWS CLI 設定 (`aws sts get-caller-identity` 応答)
- [ ] PowerShell 7 (`pwsh --version`)
- [ ] `deploy/common.ps1` の DomainZone / DomainName 編集
- [ ] Route 53 ホストゾーン + NS 委任

### イメージとパッケージのビルド

- [ ] ECR 2 リポジトリ作成 (`block-stacker/demo`, `block-stacker/learner`)
- [ ] Docker イメージビルド + push (`Dockerfile`, `Dockerfile.learner`)
- [ ] `lambda/build.ps1` で zip ビルド済 (`lambda_scheduler.zip`)

### インフラデプロイ

- [ ] `10_network.ps1` 〜 `80_cloudwatch.ps1` を順に実行 (state.json が育つ)
- [ ] §5.0 で VPC Endpoint 3 個が `available` 確認
- [ ] §5.1 `90_verify.ps1` で wss 接続が成功
- [ ] SNS subscribe（メール届く）

### スケジュール動作確認

- [ ] `bs-learner-start`, `bs-learner-stop` の Scheduler 確認（aws scheduler list-schedules）
- [ ] `bs-demo-start`, `bs-demo-stop` の Scheduler 確認
- [ ] Lambda を手動 invoke して learner だけスケール: `aws lambda invoke --function-name bs-scale-up --payload '{"asg_names":["bs-learner-asg"]}' /tmp/out.json`
- [ ] Lambda を手動 invoke して demo+streamer だけスケール: `--payload '{"asg_names":["bs-demo-asg","bs-streamer-asg"]}'`
- [ ] 各 ASG が独立に desired=1/0 になることを確認

### モデルの準備

- [ ] ローカルで `mvp2.train` を回して `output/mvp2/sac_final.zip` 生成
- [ ] `aws s3 cp` で S3 にアップロード (`s3://bs-app-<ACCOUNT>/models/latest.pt`)
- [ ] クラウドデモ起動時に S3 から取り込まれることを確認

### ローカルクライアント

- [ ] Godot 4.4.1 .NET 版インストール (`D:\Godot_v4.4.1-stable_mono_win64\`)
- [ ] `dotnet restore` で C# 依存復元
- [ ] `dotnet build client/block-stacker-client.csproj` 成功
- [ ] `client/scenes/main.tscn` 起動 → ws 接続 → 4 形状 + 地面が描画

### 運用

- [ ] AWS Budgets 設定 (`docs/aws_deployment.md` §C 参照、$70 想定)
- [ ] desired=0 に戻して停止確認
- [ ] 翌平日に `bs-demo-start` が自動起動するのを確認
- [ ] 翌第 2 土曜に `bs-learner-start` が自動起動するのを確認

---

## 関連

- 設計書: [`docs/block_stacker_design.md`](block_stacker_design.md)（§8 AWS構成・運用）
- ローカル試運転手順書: [`docs/local_demo.md`](local_demo.md)
- ログ解読マニュアル: [`docs/log_reading.md`](log_reading.md)
- 旧 Terraform 版: `infra-terraform/`（参照用）
- アプリ実装: `src/block_stacker/`
- Godot クライアント: `client/`

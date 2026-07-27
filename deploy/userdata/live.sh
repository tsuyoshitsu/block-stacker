#!/bin/bash
# 配信EC2 / live (c6a.2xlarge Spot) ブート時スクリプト。
# live_server を Docker で起動し、private IP を SSM に登録（配信側が discover する）。
# live_server = 1x速の配信 + バックグラウンド学習の融合（docs/live_mode.md）。

set -euo pipefail
exec > >(tee -a /var/log/userdata.log) 2>&1

REGION="<<REGION>>"
APP_BUCKET="<<APP_BUCKET>>"
ECR_REGISTRY="<<ECR_REGISTRY>>"

dnf update -y
dnf install -y docker awscli amazon-cloudwatch-agent

systemctl enable --now docker

# private IP を SSM に書く（配信 EC2 が読む）
PRIVATE_IP=$(curl -s http://169.254.169.254/latest/meta-data/local-ipv4)
aws ssm put-parameter --region "$REGION" --name /bs/live/private_ip \
    --type String --value "$PRIVATE_IP" --overwrite

# ECR login
aws ecr get-login-password --region "$REGION" | \
    docker login --username AWS --password-stdin "$ECR_REGISTRY"

# S3 から世界状態 + モデル + configs をダウンロード
mkdir -p /opt/bs/state /opt/bs/models /opt/bs/configs
aws s3 sync s3://"$APP_BUCKET"/world_state/ /opt/bs/state/   || true
aws s3 sync s3://"$APP_BUCKET"/models/      /opt/bs/models/  || true
aws s3 sync s3://"$APP_BUCKET"/configs/     /opt/bs/configs/

# live_server を起動 (port 8765 を公開)。配信しながらバックグラウンドで学習を継続する。
#   --snapshot-dir: モデル + replay_buffer.pkl + resume_state.json の読み書き先。
#     前営業日のスナップショットをここから自動で引き継ぐ（--no-resume は初回のみ）。
#   --n-envs / --sync-every は既定（1 / 50）が運用値なので指定しない。
#     n_envs はスナップショット作成時と一致必須（不一致だと学習スレッドだけ落ちる）。
# --restart always（unless-stopped ではない）: 停止時に bs-flush.service が
#   `docker stop` するため、`unless-stopped` だと「手動停止扱い」になって
#   **翌営業日の start でコンテナが復帰しない**。`always` は daemon 再起動時に
#   手動停止したコンテナも起こすので、日次の stop/start ループが回る。
#   （user-data は初回ブートでしか実行されないので、日次復帰は docker の restart policy 頼み）
docker run -d --name live --restart always \
    -p 8765:8765 \
    -v /opt/bs/state:/app/state \
    -v /opt/bs/models:/app/models \
    -v /opt/bs/configs:/app/configs \
    -e APP_BUCKET="$APP_BUCKET" \
    "$ECR_REGISTRY/block-stacker/live:latest" \
    block_stacker.serving.live_server \
        --snapshot-dir /app/models \
        --port 8765 \
        --configs-dir /app/configs

# CloudWatch Logs
cat > /opt/aws/amazon-cloudwatch-agent/etc/cw.json <<EOF
{
  "logs": {"logs_collected": {"files": {"collect_list":[
    {"file_path":"/var/log/userdata.log","log_group_name":"/aws/ec2/bs-live","log_stream_name":"{instance_id}/userdata"}
  ]}}}
}
EOF
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config -m ec2 -c file:/opt/aws/amazon-cloudwatch-agent/etc/cw.json -s

# --------------------------------------------------------------------
# 退避スクリプト: コンテナを猶予付きで止めてから S3 へ書き戻す
# --------------------------------------------------------------------
# **必ず docker stop を先に**やること。live_server は SIGTERM を受けて初めて
# snapshot（fresh/*.zip + replay_buffer.pkl + resume_state.json）を
# /opt/bs/models へ書き出すので、先に sync しても古い内容しか上がらない。
#
# 退避対象は models/ と state/ の両方。models/ が抜けていると Spot 中断 terminate で
# **その日の学習（最大 8 時間分）が EBS ごと消える**。
# 復元はブート時の `aws s3 sync s3://$APP_BUCKET/models/ /opt/bs/models/`（上記）で、
# ここの sync 先と同じ prefix を使う。
#
# 時間予算（Spot 猶予 2 分）: 検知 <=5s + docker stop -t 60 + upload ~15s。
# `-t 60` は live_server の SNAPSHOT_SAVE_TIMEOUT_SEC と揃えること。
cat > /usr/local/bin/bs_flush_s3.sh <<FLUSH_EOF
#!/bin/bash
# 停止時と Spot 中断時の共通退避処理。失敗しても停止は続行させる。
set -u
logger "[bs] flush: stopping live container (grace 60s)"
docker stop -t 60 live || true
logger "[bs] flush: models + world_state -> S3"
aws s3 sync /opt/bs/models/ s3://${APP_BUCKET}/models/      || logger "[bs] flush: models sync FAILED"
aws s3 sync /opt/bs/state/  s3://${APP_BUCKET}/world_state/ || logger "[bs] flush: state sync FAILED"
logger "[bs] flush: done"
FLUSH_EOF
chmod +x /usr/local/bin/bs_flush_s3.sh

# 通常停止（Lambda の stop-instances → ACPI shutdown → systemd）でも退避する。
# EC2 stop なら EBS は残るが、terminate や作り直しに備えて S3 を最新にしておく。
cat > /etc/systemd/system/bs-flush.service <<'EOF'
[Unit]
Description=Flush live snapshot to S3 on shutdown
After=network-online.target docker.service
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/true
ExecStop=/usr/local/bin/bs_flush_s3.sh
TimeoutStopSec=180
[Install]
WantedBy=multi-user.target
EOF
systemctl enable --now bs-flush.service

# Spot 中断ハンドラ: 猶予 2 分の通知を検知したら同じ退避処理を走らせる
cat > /usr/local/bin/spot_handler.sh <<'HANDLER_EOF'
#!/bin/bash
while true; do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://169.254.169.254/latest/meta-data/spot/instance-action || echo 0)
    if [ "$STATUS" = "200" ]; then
        logger "[bs] spot interruption detected"
        /usr/local/bin/bs_flush_s3.sh
        break
    fi
    sleep 5
done
HANDLER_EOF
chmod +x /usr/local/bin/spot_handler.sh

cat > /etc/systemd/system/spot-handler.service <<'EOF'
[Unit]
Description=Spot interruption handler
After=multi-user.target
[Service]
ExecStart=/usr/local/bin/spot_handler.sh
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl enable --now spot-handler.service

echo "[bs] live ready: private_ip=$PRIVATE_IP, live_server on :8765"

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
docker run -d --name live --restart unless-stopped \
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

# Spot 中断ハンドラ: 世界状態を S3 に書き戻す
cat > /usr/local/bin/spot_handler.sh <<HANDLER_EOF
#!/bin/bash
while true; do
    STATUS=\$(curl -s -o /dev/null -w "%{http_code}" http://169.254.169.254/latest/meta-data/spot/instance-action || echo 0)
    if [ "\$STATUS" = "200" ]; then
        logger "[bs] spot interruption: flush state -> S3"
        aws s3 sync /opt/bs/state/ s3://${APP_BUCKET}/world_state/
        docker stop live
        sleep 90
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

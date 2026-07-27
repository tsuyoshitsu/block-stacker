#!/bin/bash
# 学習 EC2 (c6a.4xlarge Spot, AMD EPYC CPU-only) ブート時スクリプト。
# Amazon Linux 2023 x86_64 AMI 前提（Docker は dnf で導入）。

set -euo pipefail
exec > >(tee -a /var/log/userdata.log) 2>&1

REGION="<<REGION>>"
APP_BUCKET="<<APP_BUCKET>>"
ECR_REGISTRY="<<ECR_REGISTRY>>"

dnf update -y
dnf install -y docker awscli amazon-cloudwatch-agent

systemctl enable --now docker

aws ecr get-login-password --region "$REGION" | \
    docker login --username AWS --password-stdin "$ECR_REGISTRY"

mkdir -p /opt/bs/state /opt/bs/checkpoints /opt/bs/configs
aws s3 sync s3://"$APP_BUCKET"/state/    /opt/bs/state/         || true
aws s3 sync s3://"$APP_BUCKET"/models/   /opt/bs/checkpoints/
aws s3 sync s3://"$APP_BUCKET"/configs/  /opt/bs/configs/

# プリセット生成を Docker で起動（CPU-only）。
# train の既定がそのままプリセット生成（Stage 3 のみ・5,000 steps、n_envs=1）なので
# ステップ数・並列数は指定しない（configs/training.yaml が唯一の出所）。
#   n_envs は gradient_steps と揃える必要があるため CLI で上書きしないこと。
#   フルカリキュラムを回したい場合のみ --start-stage 1 --target-stage 4 を足す。
# 実測 約2.5 steps/秒 → 5,000 steps ≈ 35 分で終了する。
docker run -d --name learner --restart unless-stopped \
    -v /opt/bs/state:/app/state \
    -v /opt/bs/checkpoints:/app/checkpoints \
    -v /opt/bs/configs:/app/configs \
    -e APP_BUCKET="$APP_BUCKET" \
    "$ECR_REGISTRY/block-stacker/learner:latest" \
    block_stacker.training.train \
        --configs-dir /app/configs \
        --output-dir /app/checkpoints

# 生成物を S3 へ同期する。定期 checkpoint は撤去済みで、走破後にプリセット 1 本
# （+ replay_buffer.pkl / resume_state.json）が出るだけなので、下のタイマーは
# 「完了を取りこぼさないためのポーリング」であって細かい途中経過の収集ではない。
cat > /usr/local/bin/upload_checkpoint.sh <<EOF
#!/bin/bash
aws s3 sync /opt/bs/checkpoints/ s3://${APP_BUCKET}/models/ --exclude '.*'
EOF
chmod +x /usr/local/bin/upload_checkpoint.sh

cat > /etc/systemd/system/checkpoint-uploader.timer <<'EOF'
[Unit]
Description=Upload checkpoints to S3 every 5 min
[Timer]
OnBootSec=300
OnUnitActiveSec=300
Unit=checkpoint-uploader.service
[Install]
WantedBy=timers.target
EOF
cat > /etc/systemd/system/checkpoint-uploader.service <<'EOF'
[Unit]
Description=Upload checkpoints
[Service]
Type=oneshot
ExecStart=/usr/local/bin/upload_checkpoint.sh
EOF
systemctl enable --now checkpoint-uploader.timer

# Spot 中断: 最終 checkpoint を upload
cat > /usr/local/bin/spot_handler.sh <<HANDLER_EOF
#!/bin/bash
while true; do
    STATUS=\$(curl -s -o /dev/null -w "%{http_code}" http://169.254.169.254/latest/meta-data/spot/instance-action || echo 0)
    if [ "\$STATUS" = "200" ]; then
        logger "[bs] spot interruption: final checkpoint upload"
        /usr/local/bin/upload_checkpoint.sh
        docker stop learner
        sleep 90
        break
    fi
    sleep 5
done
HANDLER_EOF
chmod +x /usr/local/bin/spot_handler.sh

cat > /etc/systemd/system/spot-handler.service <<'EOF'
[Unit]
After=multi-user.target
[Service]
ExecStart=/usr/local/bin/spot_handler.sh
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl enable --now spot-handler.service

echo "[bs] learner ready"

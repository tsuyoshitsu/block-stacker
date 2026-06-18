#!/bin/bash
# 配信 EC2 ブート時スクリプト (Amazon Linux 2023 ARM)。
#
# 役割:
#   - EIP を関連付け (ASG が起動するインスタンスに毎回固定 EIP を再付与)
#   - Caddy を host にインストール (自動 TLS for ${domain})
#   - SSM Parameter Store から demo EC2 の private IP を取得し、
#     Caddyfile を生成して reverse_proxy ws://<demo_ip>:8765
#   - CloudWatch Logs Agent でログを集約
#   - Spot 中断ハンドラ (graceful shutdown)
set -euo pipefail
exec > >(tee -a /var/log/userdata.log) 2>&1

REGION="${region}"
DOMAIN="${domain}"
EIP_ALLOC="${eip_alloc}"
APP_BUCKET="${app_bucket}"

dnf update -y
dnf install -y awscli jq amazon-cloudwatch-agent

# 1) EIP 関連付け
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 associate-address \
    --region "$REGION" \
    --instance-id "$INSTANCE_ID" \
    --allocation-id "$EIP_ALLOC" \
    --allow-reassociation

# 2) Caddy インストール (ARM 用 static binary)
CADDY_VERSION="2.8.4"
curl -fsSL \
    "https://github.com/caddyserver/caddy/releases/download/v$${CADDY_VERSION}/caddy_$${CADDY_VERSION}_linux_arm64.tar.gz" \
    -o /tmp/caddy.tar.gz
tar -xzf /tmp/caddy.tar.gz -C /usr/local/bin caddy
chmod +x /usr/local/bin/caddy
setcap cap_net_bind_service=+ep /usr/local/bin/caddy

# 3) demo EC2 の private IP を SSM から取得 (デモ起動より後にこのインスタンスが
#    起動した場合に備えて 5 分まで polling)
DEMO_IP=""
for i in $(seq 1 30); do
    DEMO_IP=$(aws ssm get-parameter --region "$REGION" --name /bs/demo/private_ip \
                --query Parameter.Value --output text 2>/dev/null || echo "")
    if [ -n "$DEMO_IP" ] && [ "$DEMO_IP" != "None" ]; then
        break
    fi
    echo "[bs] waiting for demo private IP in SSM... ($i/30)"
    sleep 10
done
if [ -z "$DEMO_IP" ] || [ "$DEMO_IP" = "None" ]; then
    echo "[bs] WARN: demo IP not found; using placeholder localhost"
    DEMO_IP="127.0.0.1"
fi

# 4) Caddyfile (WebSocket 対応 reverse_proxy)
mkdir -p /etc/caddy /var/log/caddy /var/lib/caddy
cat > /etc/caddy/Caddyfile <<EOF
$${DOMAIN} {
    encode zstd gzip
    log {
        output file /var/log/caddy/access.log {
            roll_size 10mb
            roll_keep 3
        }
        format json
    }

    @ws {
        header Connection *Upgrade*
        header Upgrade    websocket
    }
    reverse_proxy @ws $${DEMO_IP}:8765 {
        flush_interval -1
        transport http {
            keepalive 5m
        }
    }

    reverse_proxy $${DEMO_IP}:8765
}
EOF

# 5) Caddy を systemd で起動
cat > /etc/systemd/system/caddy.service <<'EOF'
[Unit]
Description=Caddy
After=network.target

[Service]
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now caddy

# 6) CloudWatch Logs Agent
cat > /opt/aws/amazon-cloudwatch-agent/etc/cw.json <<EOF
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/userdata.log",
            "log_group_name": "/aws/ec2/bs-streamer",
            "log_stream_name": "{instance_id}/userdata"
          },
          {
            "file_path": "/var/log/caddy/access.log",
            "log_group_name": "/aws/ec2/bs-streamer",
            "log_stream_name": "{instance_id}/caddy"
          }
        ]
      }
    }
  }
}
EOF
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config -m ec2 -c file:/opt/aws/amazon-cloudwatch-agent/etc/cw.json -s

# 7) Spot 中断ハンドラ
cat > /usr/local/bin/spot_handler.sh <<'HANDLER_EOF'
#!/bin/bash
while true; do
    STATUS=$(curl -s -o /dev/null -w "%%{http_code}" http://169.254.169.254/latest/meta-data/spot/instance-action || echo 0)
    if [ "$STATUS" = "200" ]; then
        logger "[bs] spot interruption detected; stopping caddy"
        systemctl stop caddy
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

echo "[bs] streamer ready: https://$${DOMAIN} -> $${DEMO_IP}:8765"

#!/bin/bash
# 配信 EC2 (t4g.small ARM Spot) ブート時スクリプト。
# Step 60 から Expand-Userdata で <<...>> プレースホルダが置換される。

set -euo pipefail
exec > >(tee -a /var/log/userdata.log) 2>&1

REGION="<<REGION>>"
DOMAIN="<<DOMAIN>>"
EIP_ALLOC="<<EIP_ALLOC>>"
APP_BUCKET="<<APP_BUCKET>>"

dnf update -y
dnf install -y awscli jq amazon-cloudwatch-agent

# 1) EIP 関連付け
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 associate-address --region "$REGION" \
    --instance-id "$INSTANCE_ID" --allocation-id "$EIP_ALLOC" --allow-reassociation

# 2) Caddy インストール (ARM static binary)
CADDY_VERSION="2.8.4"
curl -fsSL \
    "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_arm64.tar.gz" \
    -o /tmp/caddy.tar.gz
tar -xzf /tmp/caddy.tar.gz -C /usr/local/bin caddy
chmod +x /usr/local/bin/caddy
setcap cap_net_bind_service=+ep /usr/local/bin/caddy

# 3) demo EC2 の private IP を SSM から待ち取得
DEMO_IP=""
for i in $(seq 1 30); do
    DEMO_IP=$(aws ssm get-parameter --region "$REGION" --name /bs/demo/private_ip \
                --query Parameter.Value --output text 2>/dev/null || echo "")
    [ -n "$DEMO_IP" ] && [ "$DEMO_IP" != "None" ] && break
    echo "[bs] waiting for demo private IP in SSM... ($i/30)"
    sleep 10
done
[ -z "$DEMO_IP" ] || [ "$DEMO_IP" = "None" ] && DEMO_IP="127.0.0.1"

# 4) Caddyfile (WebSocket 対応)
mkdir -p /etc/caddy /var/log/caddy /var/lib/caddy
cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
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
    reverse_proxy @ws ${DEMO_IP}:8765 {
        flush_interval -1
    }
    reverse_proxy ${DEMO_IP}:8765
}
EOF

# 5) systemd で Caddy 起動
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
  "logs": {"logs_collected": {"files": {"collect_list":[
    {"file_path":"/var/log/userdata.log","log_group_name":"/aws/ec2/bs-streamer","log_stream_name":"{instance_id}/userdata"},
    {"file_path":"/var/log/caddy/access.log","log_group_name":"/aws/ec2/bs-streamer","log_stream_name":"{instance_id}/caddy"}
  ]}}}
}
EOF
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config -m ec2 -c file:/opt/aws/amazon-cloudwatch-agent/etc/cw.json -s

# 7) Spot 中断ハンドラ
cat > /usr/local/bin/spot_handler.sh <<'HANDLER_EOF'
#!/bin/bash
while true; do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://169.254.169.254/latest/meta-data/spot/instance-action || echo 0)
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

echo "[bs] streamer ready: https://${DOMAIN} -> ${DEMO_IP}:8765"

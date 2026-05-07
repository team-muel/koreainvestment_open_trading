#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  KIS ICT 트레이딩 — GCP e2-standard-2 (asia-northeast3) 초기 설정
#  VM에 SSH 접속 후 한 번만 실행하세요
#  사용법: bash setup_gcp.sh
# ═══════════════════════════════════════════════════════════════
set -e

PROJECT_ID="tonal-benefit-495513-e1"
INSTANCE_NAME="kis-ict-trading-bot-free"
ZONE="asia-northeast3-a"
REPO_URL="https://github.com/team-muel/koreainvestment_open_trading.git"
APP_DIR="/opt/kis-trading"

echo "======================================================"
echo " KIS ICT 트레이딩 GCP VM 초기 설정"
echo " 프로젝트: $PROJECT_ID"
echo " 인스턴스: $INSTANCE_NAME ($ZONE)"
echo "======================================================"

# ── 1. 시스템 업데이트 ─────────────────────────────────────
echo ""
echo "[1/7] 시스템 패키지 업데이트..."
sudo apt-get update -qq && sudo apt-get upgrade -y -qq

# ── 2. 필수 패키지 설치 ────────────────────────────────────
echo "[2/7] 필수 패키지 설치..."
sudo apt-get install -y -qq git curl wget unzip nginx certbot python3-certbot-nginx python3-pip

# ── 3. KST 시간대 설정 ────────────────────────────────────
echo "[3/7] KST 시간대 설정..."
sudo timedatectl set-timezone Asia/Seoul
echo "  현재 시각: $(date)"

# ── 4. Docker 설치 ────────────────────────────────────────
echo "[4/7] Docker 설치..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sudo bash
    sudo usermod -aG docker $USER
    echo "  Docker 설치 완료 (재로그인 후 sudo 없이 사용 가능)"
else
    echo "  Docker 이미 설치됨"
fi

# ── 5. 프로젝트 클론 ──────────────────────────────────────
echo "[5/7] 프로젝트 설정..."
sudo mkdir -p $APP_DIR
sudo chown $USER:$USER $APP_DIR

if [ ! -d "$APP_DIR/.git" ]; then
    git clone $REPO_URL $APP_DIR
    echo "  저장소 클론 완료"
else
    cd $APP_DIR && git pull
    echo "  저장소 최신화 완료"
fi

# .env 파일 복사 안내
if [ ! -f "$APP_DIR/deploy/.env" ]; then
    cp $APP_DIR/deploy/.env.example $APP_DIR/deploy/.env
    echo ""
    echo "  ⚠️  .env 파일을 편집해주세요:"
    echo "      nano $APP_DIR/deploy/.env"
fi

# ── 6. systemd 서비스 등록 (부팅 시 자동 시작) ──────────────
echo "[6/7] systemd 서비스 등록..."
sudo tee /etc/systemd/system/kis-trading.service > /dev/null << EOF
[Unit]
Description=KIS ICT Trading System
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$APP_DIR/deploy
ExecStart=/usr/bin/docker compose up -d --remove-orphans
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=120
User=$USER
Group=docker

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable kis-trading.service
echo "  systemd 서비스 등록 완료 (부팅 시 자동 시작)"

# ── 7. Nginx 설정 ────────────────────────────────────────
echo "[7/7] Nginx 기본 설정..."
sudo tee /etc/nginx/sites-available/kis-trading > /dev/null << 'NGINX'
server {
    listen 80;
    server_name _;

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
NGINX

sudo ln -sf /etc/nginx/sites-available/kis-trading /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl enable nginx && sudo systemctl restart nginx

# ── 완료 메시지 ───────────────────────────────────────────
echo ""
echo "======================================================"
echo " ✅ 초기 설정 완료!"
echo "======================================================"
echo ""
echo " 다음 단계:"
echo "  1. .env 설정:   nano $APP_DIR/deploy/.env"
echo "  2. 서버 시작:   cd $APP_DIR/deploy && docker compose up -d"
echo "  3. 로그 확인:   docker compose logs -f trading-worker"
echo ""
echo " Cloud Scheduler 설정은 로컬 PC에서:"
echo "  bash deploy/schedule_gcp.sh"
echo ""

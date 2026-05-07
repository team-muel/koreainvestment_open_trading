#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  GCP VM 부팅 시 자동 실행 스크립트 (startup-script)
#  GCP Console → VM → 수정 → 메타데이터 → startup-script 에 붙여넣기
#  또는: gcloud compute instances add-metadata [VM명] --metadata-from-file startup-script=deploy/startup_script.sh
# ═══════════════════════════════════════════════════════════════

APP_DIR="/opt/kis-trading"
LOG_FILE="/var/log/kis-startup.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] $1" | tee -a $LOG_FILE; }

log "====== KIS 트레이딩 VM 부팅 시작 ======"

# ── Docker 준비 대기 ──────────────────────────────────────
log "Docker 서비스 대기 중..."
timeout 30 bash -c 'until docker info >/dev/null 2>&1; do sleep 2; done'
log "Docker 준비 완료"

# ── 최신 코드 pull ────────────────────────────────────────
if [ -d "$APP_DIR/.git" ]; then
    log "최신 코드 pull..."
    cd $APP_DIR
    git pull --quiet && log "코드 최신화 완료" || log "WARNING: git pull 실패 (기존 코드로 계속)"
fi

# ── Docker Compose 시작 ───────────────────────────────────
cd $APP_DIR/deploy
log "Docker Compose 시작..."

# .env 파일 확인
if [ ! -f ".env" ]; then
    log "ERROR: .env 파일 없음 — 서버를 시작할 수 없습니다"
    exit 1
fi

docker compose pull --quiet 2>/dev/null || true
docker compose up -d --remove-orphans

# ── 컨테이너 상태 확인 ─────────────────────────────────────
sleep 5
log "컨테이너 상태:"
docker compose ps --format "table {{.Name}}\t{{.Status}}" | tee -a $LOG_FILE

# ── Telegram 시작 알림 전송 ────────────────────────────────
TELEGRAM_TOKEN=$(grep TELEGRAM_BOT_TOKEN .env | cut -d= -f2)
TELEGRAM_CHAT=$(grep TELEGRAM_CHAT_ID .env | cut -d= -f2)

if [ -n "$TELEGRAM_TOKEN" ] && [ -n "$TELEGRAM_CHAT" ] && \
   [ "$TELEGRAM_TOKEN" != "여기에_봇_토큰_입력" ]; then
    INSTANCE_NAME=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/name" \
        -H "Metadata-Flavor: Google" 2>/dev/null || echo "kis-vm")
    KST_TIME=$(TZ=Asia/Seoul date '+%H:%M')
    MSG="🚀 KIS 트레이딩 VM 시작%0A인스턴스: ${INSTANCE_NAME}%0A시각: ${KST_TIME} KST%0A컨테이너: $(docker compose ps -q | wc -l)개 실행 중"
    curl -sf "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT}&text=${MSG}&parse_mode=HTML" \
        --max-time 10 >/dev/null 2>&1 || true
    log "Telegram 시작 알림 전송"
fi

log "====== VM 부팅 완료 ======"

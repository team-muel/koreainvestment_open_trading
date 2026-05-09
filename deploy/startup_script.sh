#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  GCP VM 부팅 시 자동 실행 스크립트
#  ⚠️  git pull 금지: 운영 서버는 검증된 코드만 실행해야 합니다.
#      코드 업데이트는 별도 배포 절차(deploy.sh)를 사용하세요.
# ═══════════════════════════════════════════════════════════════

APP_DIR="/opt/kis-trading"
LOG_FILE="/var/log/kis-startup.log"

log() { echo "[$(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S KST')] $1" | tee -a $LOG_FILE; }

log "====== KIS 트레이딩 VM 부팅 시작 ======"

# ── Docker 준비 대기 ──────────────────────────────────────
log "Docker 서비스 대기 중..."
timeout 60 bash -c 'until docker info >/dev/null 2>&1; do sleep 2; done'
log "Docker 준비 완료"

# ── Docker Compose 시작 ───────────────────────────────────
cd $APP_DIR/deploy

if [ ! -f ".env" ]; then
    log "ERROR: .env 파일 없음 — 서버를 시작할 수 없습니다"
    exit 1
fi

log "Docker Compose 시작..."
docker compose up -d --remove-orphans

# ── API 서버 준비 완료 확인 (health check polling) ──────────
# sleep 고정 대신 실제 응답 확인 방식 사용
log "API 서버 준비 확인 중..."
API_READY=false
for i in $(seq 1 24); do
    sleep 5
    if curl -fsS http://localhost:8000/api/health >/dev/null 2>&1; then
        API_READY=true
        log "API 서버 준비 완료 (${i}회 시도, $((i*5))초 소요)"
        break
    fi
    log "API 서버 대기 중... (${i}/24, $((i*5))초)"
done

if [ "$API_READY" = false ]; then
    log "WARNING: API 서버가 120초 내 준비되지 않음 — 장전 스캔 스킵"
fi

# ── 컨테이너 상태 확인 ─────────────────────────────────────
log "컨테이너 상태:"
docker compose ps --format "table {{.Name}}\t{{.Status}}" | tee -a $LOG_FILE

# ── 장전 스캔 자동 실행 (평일 06:00~10:00, API 준비된 경우만) ──
KST_HOUR=$(TZ=Asia/Seoul date '+%H')
KST_DOW=$(TZ=Asia/Seoul date '+%u')  # 1=월 ~ 7=일
KST_DATE=$(TZ=Asia/Seoul date '+%Y-%m-%d')

if [ "$API_READY" = true ] && [ "$KST_DOW" -le 5 ] && \
   [ "$KST_HOUR" -ge 6 ] && [ "$KST_HOUR" -lt 10 ]; then
    log "장전 스캔 워크플로 트리거..."
    SCAN_RESULT=$(curl -fsS -X POST http://localhost:8000/api/ict/workflow/premarket \
        -H "Content-Type: application/json" \
        --max-time 30 2>&1)
    log "장전 스캔 결과: $SCAN_RESULT"
else
    if [ "$API_READY" = false ]; then
        log "장전 스캔 스킵: API 서버 미준비"
    else
        log "장전 스캔 스킵: 평일 06~10시 외 (KST ${KST_HOUR}시, 요일 ${KST_DOW})"
    fi
fi

# ── Telegram 시작 알림 ────────────────────────────────────
TELEGRAM_TOKEN=$(grep TELEGRAM_BOT_TOKEN .env | cut -d= -f2)
TELEGRAM_CHAT=$(grep TELEGRAM_CHAT_ID .env | cut -d= -f2)

if [ -n "$TELEGRAM_TOKEN" ] && [ -n "$TELEGRAM_CHAT" ] && \
   [ "$TELEGRAM_TOKEN" != "여기에_봇_토큰_입력" ]; then
    INSTANCE_NAME=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/name" \
        -H "Metadata-Flavor: Google" 2>/dev/null || echo "kis-vm")
    KST_TIME=$(TZ=Asia/Seoul date '+%H:%M')
    CONTAINER_COUNT=$(docker compose ps -q 2>/dev/null | wc -l)
    MSG="🚀 KIS 트레이딩 VM 시작%0A인스턴스: ${INSTANCE_NAME}%0A시각: ${KST_TIME} KST%0A컨테이너: ${CONTAINER_COUNT}개 실행 중%0A날짜: ${KST_DATE}"
    curl -sf "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT}&text=${MSG}&parse_mode=HTML" \
        --max-time 10 >/dev/null 2>&1 || true
    log "Telegram 시작 알림 전송"
fi

log "====== VM 부팅 완료 ======"

#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  KIS ICT 트레이딩 — Cloud Scheduler 자동 시작/종료 설정
#  로컬 PC에서 실행 (gcloud CLI 필요)
#  사용법: bash deploy/schedule_gcp.sh
#
#  스케줄:
#    평일 08:45 KST → VM 시작  (장 시작 15분 전 웜업)
#    평일 15:40 KST → VM 종료  (강제청산 14:50 + 여유 50분)
# ═══════════════════════════════════════════════════════════════
set -e

PROJECT_ID="tonal-benefit-495513-e1"
INSTANCE_NAME="kis-ict-trading-bot-free"
ZONE="asia-northeast3-a"
REGION="asia-northeast3"
SA_NAME="kis-scheduler-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "======================================================"
echo " KIS ICT 트레이딩 Cloud Scheduler 설정"
echo " 프로젝트: $PROJECT_ID"
echo " VM: $INSTANCE_NAME ($ZONE)"
echo "======================================================"

# ── gcloud 로그인 확인 ────────────────────────────────────
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | grep -q "@"; then
    echo "gcloud 로그인이 필요합니다..."
    gcloud auth login
fi

gcloud config set project $PROJECT_ID

# ── 필요한 API 활성화 ─────────────────────────────────────
echo ""
echo "[1/4] 필요한 GCP API 활성화..."
gcloud services enable \
    cloudscheduler.googleapis.com \
    compute.googleapis.com \
    iam.googleapis.com \
    --quiet
echo "  ✅ API 활성화 완료"

# ── 서비스 계정 생성 ──────────────────────────────────────
echo ""
echo "[2/4] Cloud Scheduler 전용 서비스 계정 생성..."
if ! gcloud iam service-accounts describe $SA_EMAIL --quiet 2>/dev/null; then
    gcloud iam service-accounts create $SA_NAME \
        --display-name="KIS Scheduler Service Account" \
        --quiet
    echo "  서비스 계정 생성: $SA_EMAIL"
else
    echo "  서비스 계정 이미 존재: $SA_EMAIL"
fi

# VM 시작/종료 권한 부여
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/compute.instanceAdmin.v1" \
    --quiet > /dev/null

echo "  ✅ 서비스 계정 권한 설정 완료"

# ── Cloud Scheduler Job 생성 ──────────────────────────────
echo ""
echo "[3/4] Cloud Scheduler Job 생성..."

COMPUTE_API="https://compute.googleapis.com/compute/v1"
INSTANCE_URL="$COMPUTE_API/projects/$PROJECT_ID/zones/$ZONE/instances/$INSTANCE_NAME"

# 기존 Job 삭제 (재실행 시 중복 방지)
gcloud scheduler jobs delete kis-trading-start \
    --location=$REGION --quiet 2>/dev/null || true
gcloud scheduler jobs delete kis-trading-stop \
    --location=$REGION --quiet 2>/dev/null || true

# ── VM 시작 Job: 평일 08:45 KST ──────────────────────────
gcloud scheduler jobs create http kis-trading-start \
    --location=$REGION \
    --schedule="45 8 * * 1-5" \
    --time-zone="Asia/Seoul" \
    --uri="${INSTANCE_URL}/start" \
    --http-method=POST \
    --oauth-service-account-email=$SA_EMAIL \
    --description="KIS 트레이딩 VM 시작 (평일 08:45 KST)" \
    --attempt-deadline=3m \
    --quiet

echo "  ✅ VM 시작 Job 등록: 평일 08:45 KST"

# ── VM 종료 Job: 평일 15:40 KST ──────────────────────────
gcloud scheduler jobs create http kis-trading-stop \
    --location=$REGION \
    --schedule="40 15 * * 1-5" \
    --time-zone="Asia/Seoul" \
    --uri="${INSTANCE_URL}/stop" \
    --http-method=POST \
    --oauth-service-account-email=$SA_EMAIL \
    --description="KIS 트레이딩 VM 종료 (평일 15:40 KST)" \
    --attempt-deadline=3m \
    --quiet

echo "  ✅ VM 종료 Job 등록: 평일 15:40 KST"

# ── 결과 확인 ─────────────────────────────────────────────
echo ""
echo "[4/4] 설정 확인..."
gcloud scheduler jobs list --location=$REGION \
    --filter="name:kis-trading" \
    --format="table(name,schedule,timeZone,state)"

# ── 완료 ─────────────────────────────────────────────────
echo ""
echo "======================================================"
echo " ✅ Cloud Scheduler 설정 완료!"
echo "======================================================"
echo ""
echo " 스케줄 요약:"
echo "  🟢 시작: 평일 08:45 KST (장 시작 15분 전)"
echo "  🔴 종료: 평일 15:40 KST (강제청산 14:50 + 50분)"
echo ""
echo " 예상 월간 VM 실행 시간:"
echo "  7시간/일 × 22거래일 = 154시간/월"
echo "  e2-standard-2 비용: 약 $10~12/월"
echo ""
echo " 수동 제어 (긴급 시):"
echo "  시작: gcloud compute instances start $INSTANCE_NAME --zone=$ZONE"
echo "  종료: gcloud compute instances stop  $INSTANCE_NAME --zone=$ZONE"
echo ""
echo " 즉시 테스트:"
echo "  gcloud scheduler jobs run kis-trading-start --location=$REGION"
echo ""

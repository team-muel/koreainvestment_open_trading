# ═══════════════════════════════════════════════════════════════
#  KIS ICT 트레이딩 — GCP 퀵스타트 (Windows PowerShell용)
#  사용법: cd C:\Users\User\KIS && .\deploy\gcp_quickstart.ps1
# ═══════════════════════════════════════════════════════════════

$PROJECT_ID    = "tonal-benefit-495513-e1"
$INSTANCE_NAME = "kis-ict-trading-bot-free"
$ZONE          = "asia-northeast3-a"
$REGION        = "asia-northeast3"
$SA_NAME       = "kis-scheduler-sa"
$SA_EMAIL      = "$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
$SCRIPT_DIR    = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host " KIS ICT 트레이딩 GCP 퀵스타트" -ForegroundColor Cyan
Write-Host " 프로젝트 : $PROJECT_ID" -ForegroundColor Cyan
Write-Host " VM       : $INSTANCE_NAME ($ZONE)" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan

# ── gcloud 설치 확인 ──────────────────────────────────────────
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "❌ gcloud CLI가 설치되어 있지 않습니다." -ForegroundColor Red
    Write-Host "   https://cloud.google.com/sdk/docs/install 에서 설치 후 다시 실행하세요." -ForegroundColor Yellow
    exit 1
}

gcloud config set project $PROJECT_ID | Out-Null

# ── [1/4] 필요한 GCP API 활성화 ──────────────────────────────
Write-Host ""
Write-Host "[1/4] GCP API 활성화..." -ForegroundColor Yellow
gcloud services enable cloudscheduler.googleapis.com compute.googleapis.com iam.googleapis.com --quiet
Write-Host "  ✅ API 활성화 완료" -ForegroundColor Green

# ── [2/4] startup-script VM 메타데이터 등록 ──────────────────
Write-Host ""
Write-Host "[2/4] VM startup-script 등록..." -ForegroundColor Yellow
$startupScript = Join-Path $SCRIPT_DIR "startup_script.sh"
if (Test-Path $startupScript) {
    gcloud compute instances add-metadata $INSTANCE_NAME `
        --zone=$ZONE `
        --metadata-from-file "startup-script=$startupScript" `
        --quiet
    Write-Host "  ✅ startup-script 등록 완료 (VM 시작 시 자동 실행)" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  startup_script.sh 파일을 찾을 수 없습니다: $startupScript" -ForegroundColor Yellow
}

# ── [3/4] Cloud Scheduler 서비스 계정 + Job 생성 ─────────────
Write-Host ""
Write-Host "[3/4] Cloud Scheduler 설정..." -ForegroundColor Yellow

# 서비스 계정 생성
$saExists = gcloud iam service-accounts describe $SA_EMAIL --quiet 2>$null
if (-not $saExists) {
    gcloud iam service-accounts create $SA_NAME `
        --display-name="KIS Scheduler SA" --quiet
    Write-Host "  서비스 계정 생성: $SA_EMAIL" -ForegroundColor Gray
} else {
    Write-Host "  서비스 계정 이미 존재: $SA_EMAIL" -ForegroundColor Gray
}

# VM 시작/종료 권한 부여
gcloud projects add-iam-policy-binding $PROJECT_ID `
    --member="serviceAccount:$SA_EMAIL" `
    --role="roles/compute.instanceAdmin.v1" `
    --quiet | Out-Null
Write-Host "  ✅ 서비스 계정 권한 설정 완료" -ForegroundColor Green

# 기존 Job 삭제 (중복 방지)
gcloud scheduler jobs delete kis-trading-start --location=$REGION --quiet 2>$null
gcloud scheduler jobs delete kis-trading-stop  --location=$REGION --quiet 2>$null

$INSTANCE_URL = "https://compute.googleapis.com/compute/v1/projects/$PROJECT_ID/zones/$ZONE/instances/$INSTANCE_NAME"

# VM 시작 Job (평일 08:45 KST)
gcloud scheduler jobs create http kis-trading-start `
    --location=$REGION `
    --schedule="45 8 * * 1-5" `
    --time-zone="Asia/Seoul" `
    --uri="$INSTANCE_URL/start" `
    --http-method=POST `
    --oauth-service-account-email=$SA_EMAIL `
    --description="KIS VM 시작 - 평일 08:45 KST" `
    --attempt-deadline=3m `
    --quiet
Write-Host "  ✅ 시작 Job 등록: 평일 08:45 KST" -ForegroundColor Green

# VM 종료 Job (평일 15:40 KST)
gcloud scheduler jobs create http kis-trading-stop `
    --location=$REGION `
    --schedule="40 15 * * 1-5" `
    --time-zone="Asia/Seoul" `
    --uri="$INSTANCE_URL/stop" `
    --http-method=POST `
    --oauth-service-account-email=$SA_EMAIL `
    --description="KIS VM 종료 - 평일 15:40 KST" `
    --attempt-deadline=3m `
    --quiet
Write-Host "  ✅ 종료 Job 등록: 평일 15:40 KST" -ForegroundColor Green

# ── [4/4] .env 파일 VM에 업로드 ──────────────────────────────
Write-Host ""
Write-Host "[4/4] .env 파일 VM에 업로드..." -ForegroundColor Yellow
$envFile = Join-Path $SCRIPT_DIR ".env"
if (Test-Path $envFile) {
    gcloud compute scp $envFile "${INSTANCE_NAME}:/tmp/trading.env" --zone=$ZONE --quiet
    gcloud compute ssh $INSTANCE_NAME --zone=$ZONE --quiet --command="sudo mkdir -p /opt/kis-trading/deploy && sudo mv /tmp/trading.env /opt/kis-trading/deploy/.env && sudo chmod 600 /opt/kis-trading/deploy/.env && echo done"
    Write-Host "  ✅ .env 업로드 완료" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  deploy/.env 없음 — 나중에 수동 업로드 필요" -ForegroundColor Yellow
}

# ── 완료 메시지 ───────────────────────────────────────────────
Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host " ✅ 로컬 설정 완료!" -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host " 다음 단계 — VM에 SSH 접속해서 초기 설정:" -ForegroundColor White
Write-Host "   gcloud compute ssh $INSTANCE_NAME --zone=$ZONE" -ForegroundColor Yellow
Write-Host ""
Write-Host " VM 내부에서 실행:" -ForegroundColor White
Write-Host "   curl -fsSL https://raw.githubusercontent.com/team-muel/koreainvestment_open_trading/main/deploy/setup_gcp.sh | bash" -ForegroundColor Yellow
Write-Host ""
Write-Host " 스케줄 확인:" -ForegroundColor White
Write-Host "   gcloud scheduler jobs list --location=$REGION" -ForegroundColor Yellow
Write-Host ""
Write-Host " 즉시 시작 테스트:" -ForegroundColor White
Write-Host "   gcloud scheduler jobs run kis-trading-start --location=$REGION" -ForegroundColor Yellow
Write-Host ""

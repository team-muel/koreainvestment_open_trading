#!/bin/bash
set -e

PROJECT_ID="tonal-benefit-495513-e1"
INSTANCE_NAME="kis-ict-trading-bot-free"
ZONE="asia-northeast3-a"
REGION="asia-northeast3"
SA_NAME="kis-scheduler-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
EXTERNAL_IP=""

echo "======================================================"
echo " KIS ICT Trading Cloud Scheduler Setup"
echo "======================================================"

gcloud config set project $PROJECT_ID

echo ""
echo "[1/4] Enabling GCP APIs..."
gcloud services enable cloudscheduler.googleapis.com compute.googleapis.com iam.googleapis.com --quiet
echo "  OK: APIs enabled"

echo ""
echo "[2/4] Setting up service account..."
if ! gcloud iam service-accounts describe $SA_EMAIL --quiet 2>/dev/null; then
    gcloud iam service-accounts create $SA_NAME --display-name="KIS Scheduler SA" --quiet
fi
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/compute.instanceAdmin.v1" \
    --quiet > /dev/null
echo "  OK: Service account ready"

echo ""
echo "[3/4] Cleaning up old jobs..."
for job in kis-trading-start kis-trading-stop kis-premarket-scan kis-postmarket-report kis-shutdown-check; do
    gcloud scheduler jobs delete $job --location=$REGION --quiet 2>/dev/null && echo "  Deleted: $job" || true
done

COMPUTE_API="https://compute.googleapis.com/compute/v1"
INSTANCE_URL="$COMPUTE_API/projects/$PROJECT_ID/zones/$ZONE/instances/$INSTANCE_NAME"

echo ""
echo "[4/4] Creating Cloud Scheduler jobs..."

gcloud scheduler jobs create http kis-trading-start \
    --location=$REGION \
    --schedule="35 8 * * 1-5" \
    --time-zone="Asia/Seoul" \
    --uri="${INSTANCE_URL}/start" \
    --http-method=POST \
    --oauth-service-account-email=$SA_EMAIL \
    --description="KIS VM start - 08:35 KST" \
    --attempt-deadline=3m \
    --quiet
echo "  OK: VM start at 08:35 KST"

gcloud scheduler jobs create http kis-trading-stop \
    --location=$REGION \
    --schedule="10 16 * * 1-5" \
    --time-zone="Asia/Seoul" \
    --uri="${INSTANCE_URL}/stop" \
    --http-method=POST \
    --oauth-service-account-email=$SA_EMAIL \
    --description="KIS VM stop - 16:10 KST" \
    --attempt-deadline=3m \
    --quiet
echo "  OK: VM stop at 16:10 KST"

if [ -n "$EXTERNAL_IP" ]; then
    gcloud scheduler jobs create http kis-premarket-scan \
        --location=$REGION \
        --schedule="55 8 * * 1-5" \
        --time-zone="Asia/Seoul" \
        --uri="http://${EXTERNAL_IP}:8000/api/ict/workflow/premarket" \
        --http-method=POST \
        --description="KIS premarket scan - 08:55 KST" \
        --attempt-deadline=5m \
        --quiet
    echo "  OK: Premarket scan at 08:55 KST"
else
    echo "  SKIP: Premarket scan (startup_script handles it)"
fi

if [ -n "$EXTERNAL_IP" ]; then
    gcloud scheduler jobs create http kis-postmarket-report \
        --location=$REGION \
        --schedule="42 15 * * 1-5" \
        --time-zone="Asia/Seoul" \
        --uri="http://${EXTERNAL_IP}:8000/api/ict/workflow/postmarket" \
        --http-method=POST \
        --description="KIS postmarket report - 15:42 KST" \
        --attempt-deadline=5m \
        --quiet
    echo "  OK: Postmarket report at 15:42 KST"
else
    echo "  SKIP: Postmarket report (daily_report_worker handles it)"
fi

if [ -n "$EXTERNAL_IP" ]; then
    gcloud scheduler jobs create http kis-shutdown-check \
        --location=$REGION \
        --schedule="5 16 * * 1-5" \
        --time-zone="Asia/Seoul" \
        --uri="http://${EXTERNAL_IP}:8000/api/ict/workflow/shutdown-check" \
        --http-method=GET \
        --description="KIS shutdown check - 16:05 KST" \
        --attempt-deadline=2m \
        --quiet
    echo "  OK: Shutdown check at 16:05 KST"
fi

echo ""
gcloud scheduler jobs list --location=$REGION --filter="name:kis-" --format="table(name,schedule,timeZone,state)"

echo ""
echo "======================================================"
echo " OK: Setup complete"
echo "======================================================"
echo ""
echo " Daily flow: 08:35 start -> 08:55 premarket -> 09:00 market opens"
echo " -> 14:50 force exit -> 15:40 report -> 16:05 check -> 16:10 stop"

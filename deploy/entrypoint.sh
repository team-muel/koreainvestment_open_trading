#!/bin/bash
# 컨테이너 시작 시 환경변수로 kis_devlp.yaml 자동 생성
set -e

CONFIG_DIR="/home/kisbot/KIS/config"
mkdir -p "$CONFIG_DIR"

cat > "$CONFIG_DIR/kis_devlp.yaml" << EOF
my_app: "${KIS_APP_KEY:-}"
my_sec: "${KIS_APP_SECRET:-}"

paper_app: "${KIS_PAPER_APP_KEY:-}"
paper_sec: "${KIS_PAPER_APP_SECRET:-}"

my_htsid: "${KIS_HTS_ID:-}"

my_acct_stock: "${KIS_ACCOUNT_NO:-}"
my_acct_future: ""
my_paper_stock: "${KIS_PAPER_ACCOUNT_NO:-}"
my_paper_future: ""

my_prod: "01"

prod: "https://openapi.koreainvestment.com:9443"
ops: "ws://ops.koreainvestment.com:21000"
vps: "https://openapivts.koreainvestment.com:29443"
vops: "ws://ops.koreainvestment.com:31000"

my_token: ""

my_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
EOF

chmod 600 "$CONFIG_DIR/kis_devlp.yaml"
echo "[entrypoint] kis_devlp.yaml 생성 완료 (모드: ${KIS_MODE:-vps})"

# KIS_MODE 파일 생성 (vps/prod)
echo "${KIS_MODE:-vps}" > "$CONFIG_DIR/KIS_MODE"

exec "$@"

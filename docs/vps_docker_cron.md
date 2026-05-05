# VPS Docker/Cron 운영 가이드

이 문서는 한국투자증권 모의투자(`vps`) 기반 ICT 장전 스캔과 장후 피드백을 VPS에서 자동 실행하기 위한 배포 절차입니다.

## 1. 서버 준비

Ubuntu 기준:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin git cron
sudo systemctl enable --now docker cron
sudo usermod -aG docker "$USER"
```

Docker 그룹 권한은 재로그인 후 적용됩니다.

## 2. 코드 배치

```bash
sudo mkdir -p /opt/kis-ict
sudo chown "$USER:$USER" /opt/kis-ict
git clone https://github.com/team-muel/koreainvestment_open_trading.git /opt/kis-ict
cd /opt/kis-ict
```

이미 clone 되어 있다면:

```bash
cd /opt/kis-ict
git pull --ff-only
```

## 3. KIS 설정 배치

KIS API 키, 시크릿, 계좌번호가 들어 있는 `kis_devlp.yaml`은 Git에 올리지 않습니다. 서버의 별도 디렉터리에 저장하고 컨테이너에 마운트합니다.

```bash
mkdir -p ~/KIS/config
chmod 700 ~/KIS ~/KIS/config
```

`kis_devlp.yaml`을 `~/KIS/config/kis_devlp.yaml`에 넣은 뒤 권한을 제한합니다.

```bash
chmod 600 ~/KIS/config/kis_devlp.yaml
```

## 4. 환경 파일

```bash
cp .env.example .env
nano .env
```

예시:

```dotenv
TZ=Asia/Seoul
COMPOSE_PROJECT_NAME=kis_ict
BACKEND_HOST=127.0.0.1
FRONTEND_HOST=127.0.0.1
KIS_CONFIG_DIR=/home/ubuntu/KIS/config
NOTION_TOKEN=ntn_xxx
NOTION_DAILY_REPORTS_DB_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
GOOGLE_CALENDAR_API_KEY=
GOOGLE_CALENDAR_ID=primary
```

주의: Google Calendar API key만으로는 개인 캘린더에 서버가 직접 일정을 쓰기 어렵습니다. 현재 VPS cron은 리포트 생성과 Notion 발행을 담당하고, Google Calendar 알림은 Codex/Google Calendar 연결 또는 별도 OAuth 구성이 필요합니다.

## 5. 빌드와 실행

```bash
docker compose build
docker compose up -d backend frontend
docker compose ps
```

기본 포트는 로컬 바인딩입니다.

- Backend: `127.0.0.1:8000`
- Frontend: `127.0.0.1:3000`

외부에서 접속하려면 SSH 터널, VPN, 또는 인증이 걸린 reverse proxy를 사용하십시오. v1에서는 자동주문 API를 인터넷에 직접 공개하지 않는 것을 원칙으로 합니다.

## 6. 수동 작업 확인

```bash
docker compose run --rm ict-job python scripts/ict_daily_workflow.py premarket-scan --mode vps --max-scan-symbols 10 --watchlist-limit 5 --request-delay 1.0
docker compose run --rm ict-job python scripts/ict_daily_workflow.py postmarket-feedback --mode vps --limit 5 --request-delay 1.0
```

리포트는 아래에 저장됩니다.

```text
/opt/kis-ict/strategy_builder/data/reports/ict
```

Notion 환경변수가 설정되어 있고 DB 권한이 연결되어 있으면 같은 리포트가 Notion DB에도 발행됩니다.

## 7. Cron 등록

```bash
mkdir -p /opt/kis-ict/logs
crontab deploy/cron/ict-crontab
crontab -l
```

등록되는 작업:

- 평일 08:30 KST: KOSPI/KOSDAQ 장전 스캔
- 평일 15:45 KST: 장후 피드백 리포트

로그 확인:

```bash
tail -f /opt/kis-ict/logs/cron.log
```

## 8. 자동 부트스트랩

서버 준비, Docker 빌드, cron 등록까지 한 번에 실행하려면:

```bash
bash deploy/vps/bootstrap.sh
```

스크립트는 `.env`와 `kis_devlp.yaml`을 자동 생성하지 않습니다. 민감정보는 사용자가 직접 입력해야 합니다.

## 9. 운영 원칙

- v1은 한국투자 모의투자 `vps`만 사용합니다.
- 자동매매 기본 한도는 보수적으로 1포지션, 일 1회 신규 진입입니다.
- `.env`, `kis_devlp.yaml`, 토큰 파일은 Git에 커밋하지 않습니다.
- 장전 스캔 결과와 장후 피드백을 먼저 검토하고, 자동주문 시작은 별도로 판단합니다.

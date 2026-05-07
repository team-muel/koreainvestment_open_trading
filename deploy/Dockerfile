FROM python:3.11-slim

# 시스템 의존성
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# KST 시간대 설정
ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# Python 경로 설정 - strategy_builder 하위 모듈들이 'core', 'kis_auth' 등으로 임포트 가능하게
ENV PYTHONPATH=/app:/app/strategy_builder

# 의존성 먼저 복사 (캐시 활용)
COPY pyproject.toml ./
COPY strategy_builder/pyproject.toml ./strategy_builder/

# pip 설치
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
    fastapi>=0.115.0 \
    "uvicorn[standard]>=0.30.0" \
    pydantic>=2 \
    pyyaml>=6.0.2 \
    requests>=2.32.4 \
    websockets>=15.0.1 \
    python-dotenv>=1.0.0 \
    python-multipart>=0.0.20 \
    httpx>=0.27.0 \
    pandas>=2.3.1 \
    numpy>=1.26.0 \
    pycryptodome>=3.23.0 \
    aiosqlite>=0.20.0

# 소스 복사
COPY . .

# 데이터 디렉토리 생성
RUN mkdir -p /app/data /app/logs

# 비루트 사용자로 실행 (보안)
RUN useradd -m -u 1000 kisbot && chown -R kisbot:kisbot /app
USER kisbot

EXPOSE 8000

CMD ["uvicorn", "strategy_builder.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

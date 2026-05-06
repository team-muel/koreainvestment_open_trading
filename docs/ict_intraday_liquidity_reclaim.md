# KIS ICT Intraday Liquidity Reclaim

이 전략은 국내주식 현물 데이트레이딩용 롱 전용 전략입니다. 기존 4H/1H POI 기반 스윙형 ICT 전략보다 장 시작 후 유동성, VWAP, Opening Range를 우선합니다.

## 운영 목표

- 하루 1~2회 이하만 진입합니다.
- 목표수익에 도달하면 당일 신규 진입을 중단합니다.
- 장전 스캔, 장 초반 관찰, 장중 진입, 장후 피드백 구조로 운용합니다.
- 한국투자증권 v1 자동주문은 모의투자(`vps`)에서만 실행합니다.

## 장전 스캔

08:30~08:55 사이 KOSPI/KOSDAQ 종목을 추립니다.

기본 필터:

- 전일 거래대금 상위 후보
- 2,000원 이상
- 전일 거래대금과 20일 평균 거래대금 기준 통과
- 전일 등락률 +1%~+8%
- 상대 거래량 2배 이상
- 전일 고점/저점 또는 최근 5일 고점/저점과의 거리 기록
- 후보별 `scan_reason`, `priority`, `invalidation_level`, `plan` 저장
- ETF, ETN, ELW, SPAC, REIT, 우선주성 종목 제외

목표는 “많이 오른 종목” 전체를 잡는 것이 아니라, 장중 깔끔한 유동성 sweep 구조가 나올 가능성이 있는 종목만 watchlist에 올리는 것입니다.

## Opening Range

- 09:00~09:05: 관찰만 합니다.
- 09:05~09:15: 초기 고점/저점이 형성되는 구간으로 봅니다.
- Opening Range는 09:00~09:15의 고가/저가입니다.

```text
OR High = 09:00~09:15 최고가
OR Low  = 09:00~09:15 최저가
```

## 롱 진입 조건

아래 조건이 모두 충족되어야 합니다.

1. 장전 watchlist에 포함된 종목
2. 현재 시간이 09:15 이후, 10:30 이전
3. 가격이 OR Low 또는 전일 저점을 최소 0.1% wick으로 하향 이탈한 뒤 회복
4. sweep 이후 3~5분 안에 기준 레벨 위로 회복
5. VWAP 위로 재진입
6. 1분봉 2개 연속 종가가 VWAP 위에서 마감
7. 최근 5분 구간 종가가 VWAP 위에서 마감
8. 직전 1분봉 고점을 종가 돌파하여 MSS 확인
9. 현재 거래량이 직전 5분 평균의 1.5배 이상

v1은 국내 현물 롱만 자동 실행합니다. 숏 또는 인버스/선물형 반대 전략은 별도 확장 범위입니다.

## 진입

첫 자동매매 단계에서는 보수형 진입을 사용합니다.

- VWAP reclaim 이후
- 1분봉 2개 연속 VWAP 위 마감과 5분봉 VWAP 위 마감 확인
- 직전 1분봉 고점을 돌파하면 시장가 또는 최우선 매수 성격으로 진입합니다.

초기 자동매매에서는 신규 진입 시간대를 `09:15~10:30`으로 제한합니다. 10:30 이후에는 신규 진입하지 않고 기존 포지션만 관리합니다.

기존 5M OB 50% 지정가 진입은 이 전략에서는 기본 진입 방식이 아닙니다.

## 손절

진입 전 손절을 반드시 계산합니다.

```text
structural_stop = sweep low 아래
vwap_stop       = VWAP 아래
fixed_stop      = entry * 0.995
stop            = min(structural_stop, vwap_stop, fixed_stop)
```

단, 손절폭이 진입가 대비 1%를 넘으면 진입하지 않습니다.

## 익절과 종료

```text
R = entry - stop

1차 익절: entry + 1R
2차 익절: entry + 2R
강제 종료: VWAP 이탈 또는 14:50 이후
```

시스템 기본값:

- 일일 목표수익: 계좌 대비 +0.5%
- 일일 최대손실: 계좌 대비 -0.5%
- 1회 허용 손실: 계좌 대비 0.3%
- 하루 최대 신규 진입: 2회
- 최대 동시 포지션: 1개
- 목표수익 도달 후 당일 신규 진입 금지

포지션 사이징은 고정 금액이 아니라 R 기준입니다.

```text
risk_amount = account_equity * 0.003
risk_per_share = entry - stop
quantity = risk_amount / risk_per_share
```

## 기록 기준

자동매매 판단은 네 단계로 분리합니다.

- Setup: 오늘 볼 만한 종목인가
- Trigger: 지금 진입해도 되는가
- Execution: 실제로 어떻게 주문할 것인가
- Exit: 언제 나갈 것인가

SQLite 원장:

- `premarket_scan`: 장전 후보와 scan reason
- `signal_log`: trigger 발생/비발생, action taken, reason not taken
- `trade_journal`: 주문, 체결 후보, 청산 이벤트
- `daily_report`: 일일 성과 요약
- `strategy_change_log`: 규칙 변경 이력

Notion:

- 장전 스캔 이유
- 실제 매매 요약
- 장후 피드백
- 하루 리포트

## 구현 상태

- `ict_core.intraday.IntradayLiquidityReclaimBuilder`가 신호 생성 기준입니다.
- `ICTTradingEngine`은 이 builder를 라이브와 백테스트에 사용합니다.
- TP는 +1R, +2R 분할 지정가 주문으로 제출합니다.
- SL은 synthetic stop으로 감시하며, TP 주문 취소 확인 후 시장가 청산합니다.
- `strategy_builder/core/ict_journal.py`가 SQLite 운영 원장을 관리합니다.

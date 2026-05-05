# KIS ICT 자동매매 준비 우선순위

## P0: 실전 차단 및 모의 운용

- v1 자동주문은 `vps` 모의투자에서만 시작한다.
- `prod` 모드에서는 `/api/ict/start`가 실패해야 한다.
- 실전 전환은 별도 설계와 승인 전까지 범위에서 제외한다.

## P1: 주문 안전장치

- TP 취소가 실패하거나 취소 상태를 재확인할 수 없으면 synthetic SL 시장가 매도를 내지 않는다.
- SL 시장가 매도는 주문 접수만으로 포지션을 닫지 않고, 잔고가 0이 된 뒤에만 `CLOSED` 처리한다.
- `EXITING` 상태에서는 반복 시장가 매도를 제출하지 않는다.
- 미체결 주문이 조회에서 반복적으로 사라지고 잔고도 없으면 취소/거부 상태로 간주한다.

## P2: 전략 오탐 축소

- Daily context가 bearish이면 신규 롱 setup을 차단한다.
- 4H bullish, 1H POI, 30M liquidity trap, 5M sweep/CHoCH 순서로 필터링한다.
- liquidity score가 기준 미만이면 setup은 기록하되 주문 후보를 만들지 않는다.
- 상한가/하한가/거래정지로 판단되는 상태에서는 신규 진입하지 않는다.

## P3: 검증

- 백테스트는 라이브 엔진과 같은 `ICTSetupBuilder`를 사용한다.
- 20거래일 ready warmup 이전에는 백테스트도 주문을 만들지 않는다.
- `poll` source로 생성된 현재가 스냅샷은 ready warmup에 포함하지 않는다.
- 장기 백테스트는 로컬 1분봉 캐시가 충분히 쌓인 뒤 `scripts/run_ict_backtest.py`로 실행한다.

## P4: 운영 전 체크리스트

- `python -m unittest discover -s tests`
- `python -m compileall -q ict_core strategy_builder tests`
- `npm run lint`
- `npm run build`
- `/ict` 대시보드에서 `vps`, `READY`, `Guard ACTIVE`, `Realtime connected` 확인
- 최초 자동주문은 1종목, 최대 1포지션, 일 1회 신규 진입으로 제한한다.

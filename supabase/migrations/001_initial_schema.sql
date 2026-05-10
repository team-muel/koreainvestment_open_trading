-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. orders: 주문 내역
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_no TEXT UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    status TEXT NOT NULL DEFAULT 'LIMIT_SUBMITTED',
    quantity INTEGER NOT NULL DEFAULT 0,
    filled_qty INTEGER NOT NULL DEFAULT 0,
    price NUMERIC(12,2),
    entry NUMERIC(12,2),
    stop NUMERIC(12,2),
    take_profit NUMERIC(12,2),
    tp_order_no TEXT,
    tp2_order_no TEXT,
    exit_order_no TEXT,
    submitted_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_submitted_at ON orders(submitted_at DESC);

-- 2. fills: 실체결 내역
CREATE TABLE IF NOT EXISTS fills (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_no TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    filled_qty INTEGER NOT NULL DEFAULT 0,
    avg_price NUMERIC(12,2),
    fees NUMERIC(12,4) DEFAULT 0,
    realized_pnl NUMERIC(14,2),
    risk_amount NUMERIC(14,2),
    is_complete BOOLEAN DEFAULT FALSE,
    fill_time TIMESTAMPTZ DEFAULT NOW(),
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(order_no, side)
);
CREATE INDEX IF NOT EXISTS idx_fills_order_no ON fills(order_no);
CREATE INDEX IF NOT EXISTS idx_fills_symbol ON fills(symbol);
CREATE INDEX IF NOT EXISTS idx_fills_fill_time ON fills(fill_time DESC);

-- 3. positions: 현재 보유 포지션
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    order_no TEXT,
    quantity INTEGER NOT NULL DEFAULT 0,
    entry NUMERIC(12,2),
    stop NUMERIC(12,2),
    take_profit NUMERIC(12,2),
    opened_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    payload JSONB
);

-- 4. signals: 시그널 로그 (ICT setup 탐지 이력)
CREATE TABLE IF NOT EXISTS signals (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    signal_id TEXT UNIQUE,
    ticker TEXT NOT NULL,
    signal_type TEXT NOT NULL DEFAULT 'LONG_RECLAIM',
    liquidity_level NUMERIC(12,2),
    sweep_confirmed BOOLEAN DEFAULT FALSE,
    vwap_reclaim BOOLEAN DEFAULT FALSE,
    mss_confirmed BOOLEAN DEFAULT FALSE,
    volume_confirmed BOOLEAN DEFAULT FALSE,
    entry_candidate NUMERIC(12,2),
    stop_candidate NUMERIC(12,2),
    target_candidate NUMERIC(12,2),
    signal_quality_score NUMERIC(5,2),
    action_taken BOOLEAN DEFAULT FALSE,
    reason_not_taken TEXT,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_action_taken ON signals(action_taken);

-- 5. bars_1m: 1분봉 캐시 (최근 N일치)
CREATE TABLE IF NOT EXISTS bars_1m (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    open NUMERIC(12,2),
    high NUMERIC(12,2),
    low NUMERIC(12,2),
    close NUMERIC(12,2),
    volume BIGINT,
    source TEXT DEFAULT 'api',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_1m_symbol_ts ON bars_1m(symbol, ts DESC);

-- 6. portfolio_snapshots: 포트폴리오 스냅샷 (장중 주기적 저장)
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    snapshot_at TIMESTAMPTZ DEFAULT NOW(),
    total_eval INTEGER,
    deposit INTEGER,
    daily_realized NUMERIC(14,2),
    daily_loss NUMERIC(14,2),
    daily_entries INTEGER,
    open_positions_count INTEGER,
    payload JSONB
);
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_at ON portfolio_snapshots(snapshot_at DESC);

-- 7. daily_reports: 일별 리포트
CREATE TABLE IF NOT EXISTS daily_reports (
    report_date DATE PRIMARY KEY,
    start_equity NUMERIC(14,2),
    end_equity NUMERIC(14,2),
    daily_pnl NUMERIC(14,2),
    daily_return_pct NUMERIC(8,4),
    max_intraday_drawdown_pct NUMERIC(8,4),
    trades_count INTEGER DEFAULT 0,
    win_rate NUMERIC(5,2),
    average_r NUMERIC(8,4),
    profit_factor NUMERIC(8,4),
    rule_violation TEXT,
    goal_hit TEXT,
    stopped_reason TEXT,
    market_condition TEXT,
    what_worked TEXT,
    what_failed TEXT,
    next_rule_change TEXT,
    notion_page_id TEXT,
    notion_synced_at TIMESTAMPTZ,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 8. runtime_config: 런타임 설정 (동적 변경 가능)
CREATE TABLE IF NOT EXISTS runtime_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
-- 기본값 삽입
INSERT INTO runtime_config (key, value, description) VALUES
    ('live_trading_enabled', 'false', '실전 모드 여부 - true로 바꾸기 전에 반드시 확인'),
    ('max_daily_entries', '2', '일일 최대 진입 횟수'),
    ('daily_loss_limit_pct', '0.005', '일일 손실 한도 (0.5%)'),
    ('force_exit_time', '14:50', '강제 청산 시각 KST'),
    ('engine_paused', 'false', '엔진 일시 정지 플래그')
ON CONFLICT (key) DO NOTHING;

-- 9. engine_heartbeats: 엔진 생존 신호
CREATE TABLE IF NOT EXISTS engine_heartbeats (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    worker TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    loop_count BIGINT DEFAULT 0,
    last_symbol TEXT,
    last_error TEXT,
    degraded BOOLEAN DEFAULT FALSE,
    metadata JSONB,
    beat_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_heartbeats_worker ON engine_heartbeats(worker, beat_at DESC);

-- 10. premarket_scan: 장전 스캔 결과 (Notion 동기화 대상)
CREATE TABLE IF NOT EXISTS premarket_scan (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scan_date DATE NOT NULL,
    ticker TEXT NOT NULL,
    name TEXT,
    market TEXT,
    prev_change_pct NUMERIC(6,4),
    relative_volume NUMERIC(8,2),
    volume_rank INTEGER,
    liquidity_level TEXT,
    ict_setup TEXT,
    scan_reason TEXT,
    priority INTEGER,
    invalidation_level NUMERIC(12,2),
    plan TEXT,
    status TEXT DEFAULT 'WATCHLIST',
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(scan_date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_premarket_scan_date ON premarket_scan(scan_date DESC);

-- 11. trade_journal: 매매 저널 (Notion 동기화 대상)
CREATE TABLE IF NOT EXISTS trade_journal (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    trade_id TEXT UNIQUE,
    event TEXT NOT NULL,
    ticker TEXT,
    side TEXT,
    entry_time TIMESTAMPTZ,
    entry_price NUMERIC(12,2),
    stop_price NUMERIC(12,2),
    target_price NUMERIC(12,2),
    size INTEGER,
    risk_amount NUMERIC(14,2),
    risk_per_share NUMERIC(10,2),
    r_multiple NUMERIC(8,4),
    setup TEXT,
    entry_reason TEXT,
    exit_reason TEXT,
    notion_synced BOOLEAN DEFAULT FALSE,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trade_journal_ticker ON trade_journal(ticker);
CREATE INDEX IF NOT EXISTS idx_trade_journal_notion_synced ON trade_journal(notion_synced);

-- 12. strategy_change_log: rule-change audit trail
CREATE TABLE IF NOT EXISTS strategy_change_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    changed_rule TEXT NOT NULL,
    before_value TEXT,
    after_value TEXT,
    reason TEXT,
    expected_effect TEXT,
    review_date TEXT,
    result TEXT,
    keep_or_revert TEXT,
    payload JSONB
);
CREATE INDEX IF NOT EXISTS idx_strategy_change_created_at ON strategy_change_log(created_at DESC);

-- 13. job_runs: idempotency and status for scheduled workers
CREATE TABLE IF NOT EXISTS job_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_name TEXT NOT NULL,
    trade_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error_message TEXT,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(job_name, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_job_runs_name_date ON job_runs(job_name, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_job_runs_status ON job_runs(status);

-- 14. watchlists: selected symbols from the pre-market scan
CREATE TABLE IF NOT EXISTS watchlists (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    trade_date DATE NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    priority INTEGER,
    scan_reason TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(trade_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_watchlists_trade_date ON watchlists(trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_watchlists_status ON watchlists(status);

-- 15. agent_runs: AI/automation agent audit trail
CREATE TABLE IF NOT EXISTS agent_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_name TEXT NOT NULL,
    trade_date DATE NOT NULL,
    input_payload JSONB,
    output_payload JSONB,
    status TEXT NOT NULL DEFAULT 'SUCCESS',
    llm_model TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(agent_name, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_name_date ON agent_runs(agent_name, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status);

-- 16. agent_tool_calls: MCP-like internal tool call audit trail
CREATE TABLE IF NOT EXISTS agent_tool_calls (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    input_payload JSONB,
    output_payload JSONB,
    status TEXT NOT NULL DEFAULT 'SUCCESS',
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_agent_created ON agent_tool_calls(agent_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_tool_created ON agent_tool_calls(tool_name, created_at DESC);

-- 17. audit_events: normalized audit observations
CREATE TABLE IF NOT EXISTS audit_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    trade_date DATE NOT NULL,
    severity TEXT NOT NULL DEFAULT 'INFO',
    event_type TEXT NOT NULL,
    ticker TEXT,
    description TEXT,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_events_trade_date ON audit_events(trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_severity ON audit_events(severity);

-- Row Level Security (선택적) - 필요시 활성화
-- ALTER TABLE orders ENABLE ROW LEVEL SECURITY;

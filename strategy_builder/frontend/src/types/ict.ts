export interface ICTTradePlan {
  symbol: string;
  side: "buy";
  entry: number;
  stop: number;
  take_profit: number;
  risk_reward: number;
  quantity: number;
  reason: string;
}

export interface ICTEntryOB {
  low: number;
  high: number;
  midpoint: number;
  timestamp: string;
  timeframe: string;
}

export interface ICTSetup {
  symbol: string;
  trend: "bullish" | "bearish" | "neutral";
  state: string;
  last_price: number | null;
  poi_type: "order_block" | "fvg" | "none";
  poi_low: number | null;
  poi_high: number | null;
  sweep_index: number | null;
  choch_index: number | null;
  entry_ob: ICTEntryOB | null;
  trade_plan: ICTTradePlan | null;
  daily_context: "bullish" | "bearish" | "neutral";
  trap_30m: boolean;
  liquidity_score: number;
  notes: string[];
}

export interface ICTManagedOrder {
  symbol: string;
  side: string;
  order_no: string;
  org_no: string;
  quantity: number;
  filled_quantity: number;
  price: number;
  status: string;
  entry: number;
  stop: number;
  take_profit: number;
  tp_order_no: string;
  tp_org_no: string;
  exit_order_no: string;
  exit_org_no: string;
  submitted_at: string;
}

export interface ICTRealtimeStatus {
  running: boolean;
  connected: boolean;
  symbols: string[];
  last_error: string | null;
}

export interface ICTStatus {
  running: boolean;
  degraded: boolean;
  authenticated?: boolean;
  mode?: string;
  symbols: string[];
  cache: Record<string, { coverage_days: number; state: "READY" | "WARMING_UP" }>;
  active_setups: ICTSetup[];
  pending_orders: ICTManagedOrder[];
  positions: ICTManagedOrder[];
  realtime: ICTRealtimeStatus;
  daily_risk: {
    entries: number;
    max_entries: number;
    loss: number;
    loss_limit_pct: number;
    loss_limit_reached: boolean;
  };
  last_error: string | null;
}

export interface ICTSetupsResponse {
  setups: ICTSetup[];
}

export type ICTChartTimeframe = "1m" | "5m" | "1h" | "4h";

export interface ICTChartCandle {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface ICTChartData {
  symbol: string;
  timeframe: ICTChartTimeframe;
  visible_start_index: number;
  candles: ICTChartCandle[];
  setup: ICTSetup | null;
  orders: {
    pending: ICTManagedOrder | null;
    position: ICTManagedOrder | null;
  };
}

export interface ICTBacktestRequest {
  symbols: string[];
  start: string;
  end: string;
}

export interface ICTBacktestResponse {
  status: "success" | "error";
  results: Array<{
    symbol: string;
    bars?: number;
    bars_1m?: number;
    bars_5m?: number;
    coverage_days: number;
    setups_seen?: number;
    orders_submitted?: number;
    orders_filled?: number;
    orders_expired?: number;
    cache_state?: "READY" | "WARMING_UP";
    summary?: {
      trade_count: number;
      wins: number;
      losses: number;
      win_rate: number;
      total_r: number;
      total_pnl_per_share: number;
    };
    trades?: Array<{
      symbol: string;
      entry_time: string;
      exit_time: string;
      entry: number;
      stop: number;
      take_profit: number;
      exit_price: number;
      exit_reason: "take_profit" | "stop_loss" | "end_of_data";
      pnl_per_share: number;
      r_multiple: number;
      risk_reward: number;
      setup_reason: string;
    }>;
    last_setup?: ICTSetup | null;
    setup?: ICTSetup;
    trade_plan_created?: boolean;
  }>;
}

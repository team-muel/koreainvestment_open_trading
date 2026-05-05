import { apiGet, apiPost } from "./client";
import type {
  ICTBacktestRequest,
  ICTBacktestResponse,
  ICTChartData,
  ICTChartTimeframe,
  ICTSetupsResponse,
  ICTStatus,
} from "@/types/ict";

export function getICTStatus(): Promise<ICTStatus> {
  return apiGet<ICTStatus>("/api/ict/status");
}

export function startICTEngine(symbols: string[], configId = "default_ict_v1"): Promise<ICTStatus> {
  return apiPost<ICTStatus>("/api/ict/start", {
    symbols,
    config_id: configId,
  });
}

export function stopICTEngine(cancelPending = true): Promise<ICTStatus> {
  return apiPost<ICTStatus>("/api/ict/stop", {
    cancel_pending: cancelPending,
  });
}

export function getICTSetups(): Promise<ICTSetupsResponse> {
  return apiGet<ICTSetupsResponse>("/api/ict/setups");
}

export function getICTChart(
  symbol: string,
  timeframe: ICTChartTimeframe = "5m",
  limit = 160
): Promise<ICTChartData> {
  return apiGet<ICTChartData>(`/api/ict/chart/${symbol}?timeframe=${timeframe}&limit=${limit}`);
}

export function runICTBacktest(request: ICTBacktestRequest): Promise<ICTBacktestResponse> {
  return apiPost<ICTBacktestResponse>("/api/ict/backtest", request);
}

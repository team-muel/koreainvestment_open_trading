"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { BarChart3, RefreshCw } from "lucide-react";
import { getICTChart } from "@/lib/api";
import type { ICTChartData, ICTChartTimeframe } from "@/types/ict";

const TIMEFRAMES: ICTChartTimeframe[] = ["5m", "1h", "4h"];
const CHART_WIDTH = 960;
const CHART_HEIGHT = 420;
const PAD = { top: 24, right: 76, bottom: 36, left: 44 };

function formatPrice(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return Math.round(value).toLocaleString("ko-KR");
}

function formatTime(value: string, timeframe: ICTChartTimeframe) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  if (timeframe === "4h") {
    return `${date.getMonth() + 1}/${date.getDate()} ${String(date.getHours()).padStart(2, "0")}h`;
  }
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

interface IctChartProps {
  symbol: string;
  refreshKey?: number;
}

export function IctChart({ symbol, refreshKey = 0 }: IctChartProps) {
  const [timeframe, setTimeframe] = useState<ICTChartTimeframe>("5m");
  const [data, setData] = useState<ICTChartData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const loadChart = useCallback(async () => {
    if (!symbol) return;
    setIsLoading(true);
    try {
      const next = await getICTChart(symbol, timeframe, timeframe === "4h" ? 90 : 160);
      setData(next);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "chart fetch failed");
    } finally {
      setIsLoading(false);
    }
  }, [symbol, timeframe]);

  useEffect(() => {
    loadChart();
    const timer = setInterval(loadChart, 5000);
    return () => clearInterval(timer);
  }, [loadChart, refreshKey]);

  const chart = useMemo(() => {
    const candles = data?.candles ?? [];
    if (!data || candles.length === 0) return null;

    const setup = data.setup;
    const lineValues = [
      setup?.poi_low,
      setup?.poi_high,
      setup?.entry_ob?.low,
      setup?.entry_ob?.high,
      setup?.trade_plan?.entry,
      setup?.trade_plan?.stop,
      setup?.trade_plan?.take_profit,
      data.orders.pending?.entry,
      data.orders.pending?.stop,
      data.orders.pending?.take_profit,
      data.orders.position?.entry,
      data.orders.position?.stop,
      data.orders.position?.take_profit,
    ].filter((value): value is number => typeof value === "number" && Number.isFinite(value));

    const minPrice = Math.min(...candles.map((candle) => candle.low), ...lineValues);
    const maxPrice = Math.max(...candles.map((candle) => candle.high), ...lineValues);
    const pricePad = Math.max((maxPrice - minPrice) * 0.08, 1);
    const low = minPrice - pricePad;
    const high = maxPrice + pricePad;
    const plotW = CHART_WIDTH - PAD.left - PAD.right;
    const plotH = CHART_HEIGHT - PAD.top - PAD.bottom;
    const step = plotW / Math.max(candles.length - 1, 1);
    const bodyW = Math.max(2, Math.min(10, step * 0.62));
    const x = (index: number) => PAD.left + index * step;
    const y = (price: number) => PAD.top + ((high - price) / (high - low)) * plotH;
    const visibleIndex = (sourceIndex: number | null | undefined) => {
      if (typeof sourceIndex !== "number") return null;
      const local = sourceIndex - data.visible_start_index;
      return local >= 0 && local < candles.length ? local : null;
    };

    return { candles, setup, low, high, x, y, bodyW, visibleIndex };
  }, [data]);

  const renderPriceLine = (
    value: number | null | undefined,
    label: string,
    className: string,
    dash = "6 4"
  ) => {
    if (!chart || typeof value !== "number") return null;
    const y = chart.y(value);
    return (
      <g key={label}>
        <line x1={PAD.left} x2={CHART_WIDTH - PAD.right} y1={y} y2={y} className={className} strokeDasharray={dash} />
        <text x={CHART_WIDTH - PAD.right + 8} y={y + 4} className="fill-slate-500 text-[11px] font-semibold">
          {label} {formatPrice(value)}
        </text>
      </g>
    );
  };

  return (
    <section className="card">
      <div className="mb-3 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div className="flex items-center gap-2">
          <BarChart3 className="h-5 w-5 text-primary" />
          <div>
            <h2 className="text-base font-semibold">ICT Chart</h2>
            <p className="text-xs text-slate-500">{symbol} POI, sweep, CHoCH, entry, SL, TP overlay</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex h-9 overflow-hidden rounded-md border border-slate-300 dark:border-slate-700">
            {TIMEFRAMES.map((item) => (
              <button
                key={item}
                onClick={() => setTimeframe(item)}
                className={`px-3 text-sm font-medium ${timeframe === item ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950" : "bg-white text-slate-700 hover:bg-slate-100 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800"}`}
              >
                {item.toUpperCase()}
              </button>
            ))}
          </div>
          <button
            onClick={loadChart}
            className="inline-flex h-9 items-center gap-2 rounded-md border border-slate-300 px-3 text-sm font-medium hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
          >
            <RefreshCw className={`h-4 w-4 ${isLoading ? "animate-spin" : ""}`} />
            Reload
          </button>
        </div>
      </div>

      {error && <p className="mb-3 text-sm text-red-600">{error}</p>}

      {!chart ? (
        <div className="flex h-[420px] items-center justify-center rounded-md border border-dashed border-slate-300 text-sm text-slate-500 dark:border-slate-700">
          로컬 1분봉 캐시가 쌓이면 차트가 표시됩니다.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <svg viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`} className="h-[420px] min-w-[860px] w-full rounded-md bg-white dark:bg-slate-950">
            <rect x={PAD.left} y={PAD.top} width={CHART_WIDTH - PAD.left - PAD.right} height={CHART_HEIGHT - PAD.top - PAD.bottom} className="fill-slate-50 dark:fill-slate-900" />
            {[0.25, 0.5, 0.75].map((ratio) => {
              const y = PAD.top + (CHART_HEIGHT - PAD.top - PAD.bottom) * ratio;
              const price = chart.high - (chart.high - chart.low) * ratio;
              return (
                <g key={ratio}>
                  <line x1={PAD.left} x2={CHART_WIDTH - PAD.right} y1={y} y2={y} className="stroke-slate-200 dark:stroke-slate-800" />
                  <text x={CHART_WIDTH - PAD.right + 8} y={y + 4} className="fill-slate-400 text-[11px]">
                    {formatPrice(price)}
                  </text>
                </g>
              );
            })}

            {chart.setup?.poi_low && chart.setup?.poi_high && (
              <rect
                x={PAD.left}
                y={chart.y(chart.setup.poi_high)}
                width={CHART_WIDTH - PAD.left - PAD.right}
                height={Math.max(2, chart.y(chart.setup.poi_low) - chart.y(chart.setup.poi_high))}
                className="fill-amber-300/20 stroke-amber-500/50"
              />
            )}
            {chart.setup?.entry_ob && (
              <rect
                x={PAD.left}
                y={chart.y(chart.setup.entry_ob.high)}
                width={CHART_WIDTH - PAD.left - PAD.right}
                height={Math.max(2, chart.y(chart.setup.entry_ob.low) - chart.y(chart.setup.entry_ob.high))}
                className="fill-sky-300/20 stroke-sky-500/60"
              />
            )}

            {chart.candles.map((candle, index) => {
              const cx = chart.x(index);
              const up = candle.close >= candle.open;
              const bodyTop = chart.y(Math.max(candle.open, candle.close));
              const bodyBottom = chart.y(Math.min(candle.open, candle.close));
              return (
                <g key={`${candle.time}-${index}`}>
                  <line x1={cx} x2={cx} y1={chart.y(candle.high)} y2={chart.y(candle.low)} className={up ? "stroke-red-500" : "stroke-blue-500"} />
                  <rect
                    x={cx - chart.bodyW / 2}
                    y={bodyTop}
                    width={chart.bodyW}
                    height={Math.max(1, bodyBottom - bodyTop)}
                    className={up ? "fill-red-500" : "fill-blue-500"}
                  />
                </g>
              );
            })}

            {renderPriceLine(chart.setup?.trade_plan?.entry ?? data?.orders.pending?.entry ?? data?.orders.position?.entry, "ENTRY", "stroke-emerald-500", "4 3")}
            {renderPriceLine(chart.setup?.trade_plan?.stop ?? data?.orders.pending?.stop ?? data?.orders.position?.stop, "SL", "stroke-blue-600", "4 3")}
            {renderPriceLine(chart.setup?.trade_plan?.take_profit ?? data?.orders.pending?.take_profit ?? data?.orders.position?.take_profit, "TP", "stroke-red-600", "4 3")}

            {timeframe === "5m" && chart.visibleIndex(chart.setup?.sweep_index) !== null && (
              <g>
                <line
                  x1={chart.x(chart.visibleIndex(chart.setup?.sweep_index)!)}
                  x2={chart.x(chart.visibleIndex(chart.setup?.sweep_index)!)}
                  y1={PAD.top}
                  y2={CHART_HEIGHT - PAD.bottom}
                  className="stroke-blue-500"
                  strokeDasharray="3 4"
                />
                <text x={chart.x(chart.visibleIndex(chart.setup?.sweep_index)!) + 5} y={PAD.top + 16} className="fill-blue-600 text-[11px] font-semibold">SWEEP</text>
              </g>
            )}
            {timeframe === "5m" && chart.visibleIndex(chart.setup?.choch_index) !== null && (
              <g>
                <line
                  x1={chart.x(chart.visibleIndex(chart.setup?.choch_index)!)}
                  x2={chart.x(chart.visibleIndex(chart.setup?.choch_index)!)}
                  y1={PAD.top}
                  y2={CHART_HEIGHT - PAD.bottom}
                  className="stroke-emerald-500"
                  strokeDasharray="3 4"
                />
                <text x={chart.x(chart.visibleIndex(chart.setup?.choch_index)!) + 5} y={PAD.top + 32} className="fill-emerald-600 text-[11px] font-semibold">CHoCH</text>
              </g>
            )}

            {[0, Math.floor(chart.candles.length / 2), chart.candles.length - 1].map((index) => (
              <text key={index} x={chart.x(index)} y={CHART_HEIGHT - 12} textAnchor="middle" className="fill-slate-400 text-[11px]">
                {formatTime(chart.candles[index].time, timeframe)}
              </text>
            ))}
          </svg>
        </div>
      )}

      <div className="mt-3 flex flex-wrap gap-3 text-xs text-slate-500">
        <span><span className="inline-block h-2 w-4 bg-amber-300/60" /> 1H POI</span>
        <span><span className="inline-block h-2 w-4 bg-sky-300/60" /> 5M Entry OB</span>
        <span className="text-emerald-600">ENTRY</span>
        <span className="text-blue-600">SL</span>
        <span className="text-red-600">TP</span>
      </div>
    </section>
  );
}

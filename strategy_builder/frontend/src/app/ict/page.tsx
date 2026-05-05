"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Pause, Play, RefreshCw, ShieldCheck } from "lucide-react";
import { getICTStatus, startICTEngine, stopICTEngine } from "@/lib/api";
import { IctChart } from "@/components/ict/IctChart";
import type { ICTSetup, ICTStatus } from "@/types/ict";

const DEFAULT_SYMBOLS = "005930";

function formatPrice(value: number | null | undefined) {
  if (value === null || value === undefined) return "-";
  return Math.round(value).toLocaleString("ko-KR");
}

function stateClass(state: string) {
  if (state === "READY" || state === "ENTERED") return "text-green-600";
  if (state === "WARMING_UP" || state === "WAITING_TRIGGER") return "text-amber-600";
  if (state === "DEGRADED") return "text-red-600";
  return "text-slate-600 dark:text-slate-300";
}

export default function ICTPage() {
  const [symbolsText, setSymbolsText] = useState(DEFAULT_SYMBOLS);
  const [status, setStatus] = useState<ICTStatus | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const symbols = useMemo(
    () => symbolsText.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean),
    [symbolsText]
  );

  const refresh = useCallback(async () => {
    try {
      const next = await getICTStatus();
      setStatus(next);
      setRefreshTick((value) => value + 1);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "ICT status fetch failed");
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  const onStart = async () => {
    setIsLoading(true);
    try {
      const next = await startICTEngine(symbols);
      setStatus(next);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "ICT start failed");
    } finally {
      setIsLoading(false);
    }
  };

  const onStop = async () => {
    setIsLoading(true);
    try {
      const next = await stopICTEngine(true);
      setStatus(next);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "ICT stop failed");
    } finally {
      setIsLoading(false);
    }
  };

  const setups = status?.active_setups ?? [];

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-950">
      <div className="mx-auto max-w-7xl px-4 py-6 space-y-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <h1 className="text-2xl font-bold text-slate-950 dark:text-slate-50">ICT Auto Trading</h1>
            <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
              4H trend, 1H POI, 5M sweep/CHoCH based domestic stock paper trading.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={symbolsText}
              onChange={(event) => setSymbolsText(event.target.value)}
              className="h-10 w-56 rounded-md border border-slate-300 bg-white px-3 text-sm dark:border-slate-700 dark:bg-slate-900"
              placeholder="005930, 000660"
              aria-label="ICT symbols"
            />
            <button
              onClick={refresh}
              className="inline-flex h-10 items-center gap-2 rounded-md border border-slate-300 px-3 text-sm font-medium hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
            >
              <RefreshCw className="h-4 w-4" />
              Refresh
            </button>
            {status?.running ? (
              <button
                onClick={onStop}
                disabled={isLoading}
                className="inline-flex h-10 items-center gap-2 rounded-md bg-slate-900 px-4 text-sm font-semibold text-white disabled:opacity-50 dark:bg-slate-100 dark:text-slate-950"
              >
                <Pause className="h-4 w-4" />
                Stop
              </button>
            ) : (
              <button
                onClick={onStart}
                disabled={isLoading || symbols.length === 0}
                className="inline-flex h-10 items-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-white disabled:opacity-50"
              >
                <Play className="h-4 w-4" />
                Start VPS
              </button>
            )}
          </div>
        </div>

        {error && (
          <div className="flex items-center gap-2 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/30 dark:text-red-300">
            <AlertTriangle className="h-4 w-4" />
            <span>{error}</span>
          </div>
        )}

        <section className="grid gap-4 md:grid-cols-4">
          <div className="card">
            <p className="text-xs uppercase text-slate-500">Engine</p>
            <p className={`mt-2 text-xl font-bold ${status?.running ? "text-green-600" : "text-slate-700 dark:text-slate-200"}`}>
              {status?.running ? "RUNNING" : "STOPPED"}
            </p>
          </div>
          <div className="card">
            <p className="text-xs uppercase text-slate-500">Mode</p>
            <p className="mt-2 text-xl font-bold text-slate-900 dark:text-slate-100">
              {status?.mode ?? "-"}
            </p>
          </div>
          <div className="card">
            <p className="text-xs uppercase text-slate-500">Daily Entries</p>
            <p className="mt-2 text-xl font-bold text-slate-900 dark:text-slate-100">
              {status?.daily_risk.entries ?? 0}/{status?.daily_risk.max_entries ?? 5}
            </p>
          </div>
          <div className="card">
            <p className="text-xs uppercase text-slate-500">Guard</p>
            <p className={`mt-2 flex items-center gap-2 text-xl font-bold ${status?.degraded ? "text-red-600" : "text-green-600"}`}>
              <ShieldCheck className="h-5 w-5" />
              {status?.degraded ? "DEGRADED" : "ACTIVE"}
            </p>
            <p className="mt-1 text-xs text-slate-500">
              Realtime {status?.realtime?.connected ? "connected" : "idle"}
            </p>
          </div>
        </section>

        <IctChart symbol={symbols[0] ?? DEFAULT_SYMBOLS} refreshKey={refreshTick} />

        <section className="grid gap-4 lg:grid-cols-2">
          <div className="card overflow-x-auto">
            <h2 className="mb-3 text-base font-semibold">Cache Readiness</h2>
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase text-slate-500">
                <tr>
                  <th className="py-2">Symbol</th>
                  <th className="py-2">Coverage</th>
                  <th className="py-2">State</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(status?.cache ?? {}).map(([symbol, item]) => (
                  <tr key={symbol} className="border-t border-slate-200 dark:border-slate-800">
                    <td className="py-2 font-medium">{symbol}</td>
                    <td className="py-2">{item.coverage_days} days</td>
                    <td className={`py-2 font-semibold ${stateClass(item.state)}`}>{item.state}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="card overflow-x-auto">
            <h2 className="mb-3 text-base font-semibold">Orders & Positions</h2>
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase text-slate-500">
                <tr>
                  <th className="py-2">Type</th>
                  <th className="py-2">Symbol</th>
                  <th className="py-2">Qty</th>
                  <th className="py-2">Entry</th>
                  <th className="py-2">TP</th>
                </tr>
              </thead>
              <tbody>
                {[...(status?.pending_orders ?? []), ...(status?.positions ?? [])].map((order) => (
                  <tr key={`${order.symbol}-${order.order_no}-${order.status}`} className="border-t border-slate-200 dark:border-slate-800">
                    <td className={`py-2 font-semibold ${stateClass(order.status)}`}>{order.status}</td>
                    <td className="py-2">{order.symbol}</td>
                    <td className="py-2">{order.quantity}</td>
                    <td className="py-2">{formatPrice(order.entry)}</td>
                    <td className="py-2">{formatPrice(order.take_profit)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="card overflow-x-auto">
          <h2 className="mb-3 text-base font-semibold">Active ICT Setups</h2>
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase text-slate-500">
              <tr>
                <th className="py-2">Symbol</th>
                <th className="py-2">Trend</th>
                <th className="py-2">State</th>
                <th className="py-2">POI</th>
                <th className="py-2">Entry</th>
                <th className="py-2">SL</th>
                <th className="py-2">TP</th>
                <th className="py-2">Notes</th>
              </tr>
            </thead>
            <tbody>
              {setups.map((setup: ICTSetup) => (
                <tr key={setup.symbol} className="border-t border-slate-200 align-top dark:border-slate-800">
                  <td className="py-2 font-medium">{setup.symbol}</td>
                  <td className="py-2">{setup.trend}</td>
                  <td className={`py-2 font-semibold ${stateClass(setup.state)}`}>{setup.state}</td>
                  <td className="py-2">
                    {setup.poi_type === "none" ? "-" : `${setup.poi_type} ${formatPrice(setup.poi_low)}-${formatPrice(setup.poi_high)}`}
                  </td>
                  <td className="py-2">{formatPrice(setup.trade_plan?.entry)}</td>
                  <td className="py-2">{formatPrice(setup.trade_plan?.stop)}</td>
                  <td className="py-2">{formatPrice(setup.trade_plan?.take_profit)}</td>
                  <td className="max-w-sm py-2 text-xs text-slate-500">{setup.notes.join(", ") || setup.trade_plan?.reason || "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  );
}

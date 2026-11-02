"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";

import type { OhlcvBar } from "./CandleChart";

const CandleChart = dynamic(() => import("./CandleChart"), {
  ssr: false,
  loading: () => (
    <div className="flex h-[480px] items-center justify-center text-sm text-slate-500">
      Loading chart…
    </div>
  ),
});

type BarsResponse = {
  symbol: string;
  interval_min: number;
  feed: string;
  as_of: string;
  market_session: string;
  market_is_open: boolean;
  last_data_ts: number | null;
  vwap: { time: number; value: number }[];
  bars: OhlcvBar[];
};

const INTERVALS = [1, 5, 15] as const;
const QUICK_SYMBOLS = ["NVDA", "TSLA", "AAPL", "MSFT", "AMC"];
const SESSION_LABELS: Record<string, string> = {
  pre_market: "Premarket",
  regular: "Regular",
  after_hours: "After-hours",
  closed: "Closed",
};

const ET_FULL = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZoneName: "short",
});

export default function TerminalPage() {
  const [symbol, setSymbol] = useState("NVDA");
  const [input, setInput] = useState("NVDA");
  const [interval, setInterval] = useState<number>(1);
  const [data, setData] = useState<BarsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [showVwap, setShowVwap] = useState(false);
  const [refreshSec, setRefreshSec] = useState(20);
  const [tabVisible, setTabVisible] = useState(true);
  const abortRef = useRef<AbortController | null>(null);
  const inFlightRef = useRef(false);

  const load = useCallback(async (sym: string, iv: number) => {
    if (!sym || inFlightRef.current) return;
    inFlightRef.current = true;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    try {
      const res = await fetch(
        `/api/charts/bars?symbol=${encodeURIComponent(sym)}&interval=${iv}&days=5`,
        { signal: controller.signal },
      );
      const json = await res.json();
      if (!res.ok) {
        throw new Error(
          typeof json.detail === "string"
            ? json.detail
            : typeof json.error === "string"
              ? json.error
              : `Chart data failed (HTTP ${res.status})`,
        );
      }
      setData(json);
      setError("");
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") return;
      // Keep the last valid chart — flag it stale instead of clearing.
      setError(
        err instanceof Error
          ? `${err.message} — showing last known data`
          : "Unexpected error",
      );
    } finally {
      inFlightRef.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(symbol, interval);
    return () => abortRef.current?.abort();
  }, [symbol, interval, load]);

  // Controlled polling: only while auto-refresh is on and the tab is
  // visible; much slower cadence when the market is closed.
  useEffect(() => {
    const onVis = () => setTabVisible(!document.hidden);
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

  useEffect(() => {
    if (!autoRefresh || !tabVisible) return;
    const delay = data?.market_is_open === false ? 120_000 : refreshSec * 1000;
    const id = window.setInterval(() => void load(symbol, interval), delay);
    return () => window.clearInterval(id);
  }, [autoRefresh, tabVisible, refreshSec, data?.market_is_open, symbol, interval, load]);

  return (
    <main className="min-h-screen bg-[#080d17] px-4 py-8 text-slate-200 sm:px-8">
      <div className="mx-auto max-w-6xl space-y-6">
        <header className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <p className="text-xs font-medium uppercase tracking-[0.3em] text-blue-400">
              VERAR Trading Terminal
            </p>
            <h1 className="mt-1 text-2xl font-bold">Market Chart</h1>
            <p className="text-sm text-slate-400">
              Historical 1/5/15-minute bars — read-only market data.
            </p>
          </div>
          <Link
            href="/"
            className="text-xs text-slate-500 underline hover:text-slate-300"
          >
            ← Back to dashboard
          </Link>
        </header>

        <section className="rounded-2xl border border-white/10 bg-[#111b2d] p-4 sm:p-6">
          <div className="mb-4 flex flex-wrap items-center gap-3">
            <form
              onSubmit={(e) => {
                e.preventDefault();
                setSymbol(input.trim().toUpperCase());
              }}
              className="flex items-center gap-2"
            >
              {QUICK_SYMBOLS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => {
                    setInput(s);
                    setSymbol(s);
                  }}
                  className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition ${
                    symbol === s
                      ? "border-blue-500 bg-blue-500/15 text-blue-300"
                      : "border-white/10 bg-[#0d1422] text-slate-400 hover:border-white/25"
                  }`}
                >
                  {s}
                </button>
              ))}
              <input
                value={input}
                onChange={(e) => setInput(e.target.value.toUpperCase())}
                placeholder="SYMBOL"
                className="w-24 rounded-lg border border-white/10 bg-[#0d1422] px-3 py-1.5 text-xs uppercase outline-none focus:border-blue-500"
              />
            </form>

            <div className="ml-auto flex flex-wrap items-center gap-3">
              <div className="flex items-center gap-1 rounded-lg border border-white/10 bg-[#0d1422] p-1">
                {INTERVALS.map((iv) => (
                  <button
                    key={iv}
                    onClick={() => setInterval(iv)}
                    className={`rounded-md px-3 py-1 text-xs font-medium transition ${
                      interval === iv
                        ? "bg-blue-600 text-white"
                        : "text-slate-400 hover:text-slate-200"
                    }`}
                  >
                    {iv}m
                  </button>
                ))}
              </div>

              <label className="flex cursor-pointer items-center gap-1.5 text-xs text-slate-400">
                <input
                  type="checkbox"
                  checked={autoRefresh}
                  onChange={(e) => setAutoRefresh(e.target.checked)}
                  className="accent-blue-500"
                />
                Auto
              </label>
              <input
                type="number"
                min={5}
                max={300}
                value={refreshSec}
                onChange={(e) => setRefreshSec(Math.max(5, Number(e.target.value) || 20))}
                className="w-14 rounded-lg border border-white/10 bg-[#0d1422] px-2 py-1.5 text-xs outline-none focus:border-blue-500"
                title="Refresh interval (seconds)"
              />
              <label className="flex cursor-pointer items-center gap-1.5 text-xs text-amber-400">
                <input
                  type="checkbox"
                  checked={showVwap}
                  onChange={(e) => setShowVwap(e.target.checked)}
                  className="accent-amber-500"
                />
                VWAP
              </label>
            </div>
          </div>

          {error && (
            <p role="alert" className="mb-4 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-400">
              {error}
            </p>
          )}

          <CandleChart
            bars={data?.bars ?? []}
            vwap={data?.vwap ?? []}
            showVwap={showVwap}
          />

          <div className="mt-4 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
            <p className="flex items-center gap-2">
              {data?.market_session && (
                <span
                  className={`rounded-full border px-2 py-0.5 font-medium ${
                    data.market_is_open
                      ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-400"
                      : data.market_session === "pre_market" || data.market_session === "after_hours"
                        ? "border-amber-500/30 bg-amber-500/10 text-amber-400"
                        : "border-slate-500/30 bg-slate-500/10 text-slate-300"
                  }`}
                >
                  {SESSION_LABELS[data.market_session] ?? data.market_session}
                </span>
              )}
              {data
                ? `${data.symbol} · ${data.bars.length} completed ${data.interval_min}-min bars`
                : loading
                  ? "Loading…"
                  : "No data"}
              {data?.last_data_ts && (
                <>
                  {" · last bar "}
                  <span className="text-slate-300">
                    {ET_FULL.format(data.last_data_ts * 1000)}
                  </span>
                  <span className="text-slate-400">
                    {" "}({Math.max(0, Math.round((Date.now() / 1000 - data.last_data_ts) / 60))}m ago)
                  </span>
                </>
              )}
              {data && (
                <>
                  {" · fetched "}
                  <span className="text-slate-300">
                    {new Date(data.as_of).toLocaleTimeString()}
                  </span>
                </>
              )}
            </p>
            <p>
              Source: Alpaca{" "}
              <span className="font-medium text-amber-400">
                {(data?.feed ?? "iex").toUpperCase()} feed
              </span>{" "}
              — IEX is not the consolidated tape; volume understates the full
              market.
            </p>
            <p className="w-full">
              Times in ET ·{" "}
              <span className="text-blue-400">RTH</span> = regular-session
              open · <span className="text-amber-400">AH</span> = after-hours ·
              extended-hours volume shown muted.
              {showVwap && (
                <>
                  {" "}
                  · VWAP resets each ET session over the bars available —
                  on IEX it may differ from consolidated-tape VWAP.
                </>
              )}
            </p>
          </div>
        </section>
      </div>
    </main>
  );
}

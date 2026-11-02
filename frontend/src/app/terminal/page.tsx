"use client";

import { useCallback, useEffect, useState } from "react";
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
  bars: OhlcvBar[];
};

const INTERVALS = [1, 5, 15] as const;
const QUICK_SYMBOLS = ["NVDA", "TSLA", "AAPL", "MSFT", "AMC"];

export default function TerminalPage() {
  const [symbol, setSymbol] = useState("NVDA");
  const [input, setInput] = useState("NVDA");
  const [interval, setInterval] = useState<number>(1);
  const [data, setData] = useState<BarsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async (sym: string, iv: number) => {
    if (!sym) return;
    setLoading(true);
    setError("");
    try {
      const res = await fetch(
        `/api/charts/bars?symbol=${encodeURIComponent(sym)}&interval=${iv}&days=5`,
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
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unexpected error");
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(symbol, interval);
  }, [symbol, interval, load]);

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

            <div className="ml-auto flex items-center gap-1 rounded-lg border border-white/10 bg-[#0d1422] p-1">
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
          </div>

          {error && (
            <p role="alert" className="mb-4 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-400">
              {error}
            </p>
          )}

          <CandleChart bars={data?.bars ?? []} />

          <div className="mt-4 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
            <p>
              {data
                ? `${data.symbol} · ${data.bars.length} completed ${data.interval_min}-min bars`
                : loading
                  ? "Loading…"
                  : "No data"}
              {data && (
                <>
                  {" · updated "}
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
            </p>
          </div>
        </section>
      </div>
    </main>
  );
}

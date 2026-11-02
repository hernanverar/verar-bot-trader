"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import type { OhlcvBar, PriceLine, TradeMark } from "./CandleChart";

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
  vwap_verified: boolean;
  rth_vwap: { time: number; value: number }[];
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

type SimTrade = {
  symbol: string;
  recovery_ts: string;
  entry_ts: string;
  entry_price: number;
  stop_price: number;
  target_price: number;
  shares: number;
  risk_budget: number;
  exit_ts: string;
  exit_price: number;
  exit_reason: string;
  gross_pnl: number;
  net_pnl: number;
  equity_after: number;
};

type SimResult = {
  patterns_confirmed: number;
  trades_entered: number;
  skipped: [string, string][];
  win_rate: number | null;
  net_pnl: number;
  ending_equity: number;
  max_drawdown: number;
  exit_reasons: Record<string, number>;
  trades: SimTrade[];
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

const SYMBOL_RE = /^[A-Z][A-Z0-9.]{0,9}$/;

export default function TerminalPage() {
  return (
    <Suspense>
      <TerminalInner />
    </Suspense>
  );
}

function TerminalInner() {
  const params = useSearchParams();
  const router = useRouter();
  const initial = (params.get("symbol") ?? "").toUpperCase();
  const [symbol, setSymbol] = useState(SYMBOL_RE.test(initial) ? initial : "NVDA");
  const [input, setInput] = useState(SYMBOL_RE.test(initial) ? initial : "NVDA");

  // URL is the single source of truth: every symbol change (scanner link,
  // quick button, manual input, browser back/forward) flows through
  // ?symbol= and this sync. Invalid/missing params fall back to NVDA.
  useEffect(() => {
    const p = (params.get("symbol") ?? "").toUpperCase();
    const next = SYMBOL_RE.test(p) ? p : "NVDA";
    if (next !== symbol) {
      setSymbol(next);
      setInput(next);
    }
  }, [params, symbol]);

  function chooseSymbol(s: string) {
    const clean = s.trim().toUpperCase();
    setInput(clean);
    if (SYMBOL_RE.test(clean) && clean !== symbol) {
      router.push(`/terminal?symbol=${encodeURIComponent(clean)}`);
    }
  }
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
  const [simEquity, setSimEquity] = useState("2000");
  const [simStop, setSimStop] = useState("5");
  const [simTarget, setSimTarget] = useState("15");
  const [simRisk, setSimRisk] = useState("1");
  const [simResult, setSimResult] = useState<SimResult | null>(null);
  const [simLoading, setSimLoading] = useState(false);
  const [simError, setSimError] = useState("");
  const [selectedTrade, setSelectedTrade] = useState<number | null>(null);
  const simInFlight = useRef(false);
  const [showRthVwap, setShowRthVwap] = useState(false);

  const reqSeq = useRef(0);

  const load = useCallback(async (sym: string, iv: number) => {
    if (!sym) return;
    const seq = ++reqSeq.current;           // latest request wins
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    inFlightRef.current = true;
    setLoading(true);
    try {
      const res = await fetch(
        `/api/charts/bars?symbol=${encodeURIComponent(sym)}&interval=${iv}&days=5`,
        { signal: controller.signal },
      );
      const json = await res.json();
      if (seq !== reqSeq.current) return;   // stale response — discard
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
      if (seq !== reqSeq.current) return;   // stale error — ignore
      // Keep the last valid chart — flag it stale instead of clearing.
      setError(
        err instanceof Error
          ? `${err.message} — showing last known data`
          : "Unexpected error",
      );
    } finally {
      inFlightRef.current = false;
      if (seq === reqSeq.current) setLoading(false);
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

  async function runSimulation() {
    if (!symbol || simInFlight.current) return;
    simInFlight.current = true;
    setSimLoading(true);
    setSimError("");
    setSelectedTrade(null);
    try {
      const q = new URLSearchParams({
        symbol,
        initial_equity: simEquity,
        stop_loss_pct: simStop,
        profit_target_pct: simTarget,
        risk_pct: String(Number(simRisk) / 100),
      });
      const res = await fetch(`/api/strategies/pullback-simulation?${q}`);
      const json = await res.json();
      if (!res.ok) {
        throw new Error(typeof json.detail === "string" ? json.detail : json.error || "Simulation failed");
      }
      setSimResult(json);
    } catch (err) {
      setSimError(err instanceof Error ? err.message : "Unexpected error");
    } finally {
      simInFlight.current = false;
      setSimLoading(false);
    }
  }

  // Map simulation trades onto the loaded bars. Timestamps must match an
  // actual bar; trades outside the loaded range are reported, not plotted.
  const epoch = (iso: string) => Math.floor(new Date(iso).getTime() / 1000);
  const barTimes = new Set((data?.bars ?? []).map((b) => b.time));
  const trades = simResult?.trades ?? [];
  const inRange = trades.filter(
    (t) => barTimes.has(epoch(t.entry_ts)) && barTimes.has(epoch(t.exit_ts)),
  );
  const outOfRange = trades.length - inRange.length;

  const tradeMarks: TradeMark[] = inRange.flatMap((t) => {
    const kind =
      t.exit_reason === "target" ? "exit_target"
      : t.exit_reason === "stop" ? "exit_stop"
      : t.exit_reason === "session_end" ? "exit_session"
      : "exit_other";
    return [
      { time: epoch(t.entry_ts), kind: "entry" as const, label: "ENT" },
      { time: epoch(t.exit_ts), kind, label: t.exit_reason === "target" ? "TGT" : t.exit_reason === "stop" ? "STP" : "END" },
    ];
  });

  const sel = selectedTrade !== null ? inRange[selectedTrade] ?? null : null;
  const priceLines: PriceLine[] = sel
    ? [
        { price: sel.stop_price, color: "#ef4444", title: "SL" },
        { price: sel.target_price, color: "#22c55e", title: "TP" },
      ]
    : [];
  const barIndex = (ts: number) =>
    (data?.bars ?? []).findIndex((b) => b.time === ts);
  const focusRange =
    sel && barIndex(epoch(sel.entry_ts)) >= 0 && barIndex(epoch(sel.exit_ts)) >= 0
      ? { from: barIndex(epoch(sel.entry_ts)), to: barIndex(epoch(sel.exit_ts)) }
      : null;

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
                chooseSymbol(input);
              }}
              className="flex items-center gap-2"
            >
              {QUICK_SYMBOLS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => {
                    setInput(s);
                    chooseSymbol(s);
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
              <label className="flex cursor-pointer items-center gap-1.5 text-xs text-cyan-400">
                <input
                  type="checkbox"
                  checked={showRthVwap}
                  onChange={(e) => setShowRthVwap(e.target.checked)}
                  className="accent-cyan-500"
                />
                RTH VWAP
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
            rthVwap={data?.rth_vwap ?? []}
            showVwap={showVwap}
            showRthVwap={showRthVwap}
            tradeMarks={tradeMarks}
            priceLines={priceLines}
            focusRange={focusRange}
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

        <section className="rounded-2xl border border-white/10 bg-[#111b2d] p-4 sm:p-6">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h3 className="text-sm font-semibold">Pullback Simulation</h3>
              <p className="text-xs text-slate-500">
                Historical simulation on this chart — not trading performance.
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
              {(
                [
                  ["Equity $", simEquity, setSimEquity],
                  ["Stop %", simStop, setSimStop],
                  ["Risk %", simRisk, setSimRisk],
                ] as const
              ).map(([l, v, set]) => (
                <label key={l} className="flex items-center gap-1">
                  {l}
                  <input
                    type="number"
                    value={v}
                    onChange={(e) => set(e.target.value)}
                    className="w-16 rounded-lg border border-white/10 bg-[#0d1422] px-2 py-1.5 outline-none focus:border-blue-500"
                  />
                </label>
              ))}
              <label className="flex items-center gap-1">
                Target
                <select
                  value={simTarget}
                  onChange={(e) => setSimTarget(e.target.value)}
                  className="rounded-lg border border-white/10 bg-[#0d1422] px-2 py-1.5 outline-none focus:border-blue-500"
                >
                  <option value="15">15%</option>
                  <option value="20">20%</option>
                </select>
              </label>
              <button
                onClick={runSimulation}
                disabled={simLoading}
                className="rounded-lg bg-blue-600 px-4 py-1.5 font-semibold text-white transition hover:bg-blue-500 disabled:opacity-50"
              >
                {simLoading ? "Simulating…" : "Simulate"}
              </button>
            </div>
          </div>

          {simError && (
            <p role="alert" className="mb-3 text-xs text-red-400">{simError}</p>
          )}

          {simResult && (
            <p className="mb-3 text-xs text-slate-400">
              {simResult.trades_entered} trades · win rate{" "}
              {simResult.win_rate !== null ? `${simResult.win_rate}%` : "n/a"} ·
              net {simResult.net_pnl >= 0 ? "+" : ""}${simResult.net_pnl.toFixed(2)}
              {outOfRange > 0 &&
                ` · ${outOfRange} trade(s) outside the loaded chart range (not plotted)`}
            </p>
          )}

          {simResult && inRange.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-white/10 text-slate-400">
                    {["#", "Entry", "In", "Exit", "Out", "Qty", "Reason", "Net"].map((h) => (
                      <th key={h} className="px-2 py-1.5 font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {inRange.map((t, i) => (
                    <tr
                      key={`${t.entry_ts}-${i}`}
                      onClick={() => setSelectedTrade(selectedTrade === i ? null : i)}
                      className={`cursor-pointer border-b border-white/5 transition ${
                        selectedTrade === i ? "bg-blue-500/10" : "hover:bg-white/5"
                      }`}
                    >
                      <td className="px-2 py-1.5 text-slate-500">{i + 1}</td>
                      <td className="px-2 py-1.5">{ET_FULL.format(new Date(t.entry_ts).getTime())}</td>
                      <td className="px-2 py-1.5">${t.entry_price.toFixed(2)}</td>
                      <td className="px-2 py-1.5">{ET_FULL.format(new Date(t.exit_ts).getTime())}</td>
                      <td className="px-2 py-1.5">${t.exit_price.toFixed(2)}</td>
                      <td className="px-2 py-1.5">{t.shares}</td>
                      <td className="px-2 py-1.5">{t.exit_reason.replace(/_/g, " ")}</td>
                      <td className={`px-2 py-1.5 font-semibold ${t.net_pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                        {t.net_pnl >= 0 ? "+" : ""}${t.net_pnl.toFixed(2)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}

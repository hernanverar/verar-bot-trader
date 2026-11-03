
"use client";

import { useRef, useState } from "react";
import Link from "next/link";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  BarChart3,
  Flame,
  LoaderCircle,
  Play,
  ShieldCheck,
} from "lucide-react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

type ClosedTrade = {
  ticker: string;
  entry_date: string;
  exit_date: string;
  gross_pnl: number;
  costs: number;
  net_pnl: number;
};

type BacktestResult = {
  initial_cash: number;
  final_equity: number;
  return_pct: number;
  benchmark_return_pct: number | null;
  max_drawdown_pct: number;
  trades: number;
  selected_symbols: string[];
  closed_trades: ClosedTrade[];
  equity_curve: [string, number][];
  benchmark_equity_curve: [string, number][];
};

type HotStock = {
  symbol: string;
  last_price: number | null;
  change_pct: number | null;
  relative_volume: number | null;
  cum_volume: number;
  dollar_volume: number;
  momentum_pct: number | null;
  spread_bps: number | null;
  score: number | null;
  classification: "eligible" | "research" | "filtered" | "unavailable";
  ross_eligible: boolean;
  ross_fails: string[];
  ross_unverified: string[];
  reasons: string[];
  research_eligible: boolean;
  bar_status: "live" | "session_ended" | "stale" | "none";
  bar_freshness: "fresh" | "stale" | "none";
  quote_available: boolean;
  as_of: string | null;
};

type SimTrade = {
  symbol: string;
  entry_ts: string;
  entry_price: number;
  shares: number;
  exit_ts: string;
  exit_price: number;
  exit_reason: string;
  net_pnl: number;
};

type SimResult = {
  patterns_confirmed: number;
  trades_entered: number;
  skipped: [string, string][];
  win_rate: number | null;
  gross_pnl: number;
  net_pnl: number;
  ending_equity: number;
  max_drawdown: number;
  exit_reasons: Record<string, number>;
  trades: SimTrade[];
};

type PullbackPattern = {
  symbol: string;
  state: "impulse_detected" | "pullback_in_progress" | "recovery_confirmed" | "invalidated";
  impulse_start_ts: string;
  impulse_high_ts: string;
  impulse_low: number;
  impulse_high: number;
  impulse_gain_pct: number;
  pullback_low: number | null;
  pullback_low_ts: string | null;
  retracement_pct: number | null;
  pullback_bars: number | null;
  recovery_ts: string | null;
  invalidation_reason: string | null;
};

type ScanMeta = {
  universe: string[];
  total_scanned: number;
  eligible_count: number;
  research_candidates_count: number;
  excluded_instruments_count: number;
  discovery_mode: string;
  discovery_source: string;
  discovery_as_of: string | null;
  discovery_warning: string | null;
  is_fallback: boolean;
  market_session: string;
  market_is_open: boolean;
  session_warning: string | null;
};

const classificationStyles: Record<string, string> = {
  eligible:
    "border-emerald-500/30 bg-emerald-500/10 text-emerald-400",
  research: "border-blue-500/30 bg-blue-500/10 text-blue-400",
  filtered: "border-amber-500/30 bg-amber-500/10 text-amber-400",
  unavailable: "border-slate-500/30 bg-slate-500/10 text-slate-400",
};

const sessionLabels: Record<string, string> = {
  pre_market: "Pre-Market",
  regular: "Regular Hours",
  after_hours: "After Hours",
  closed: "Closed",
};

const money = (value: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
  }).format(value);

const percent = (value: number) =>
  `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;

const compactMoney = (value: number) =>
  value >= 1e9
    ? `$${(value / 1e9).toFixed(1)}B`
    : value >= 1e6
      ? `$${(value / 1e6).toFixed(1)}M`
      : money(value);

export default function Home() {
  const [initialCash, setInitialCash] = useState(1000);
  const [holdSessions, setHoldSessions] = useState(5);
  const [topN, setTopN] = useState(3);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [hotStocks, setHotStocks] = useState<HotStock[] | null>(null);
  const [scanMeta, setScanMeta] = useState<ScanMeta | null>(null);
  const [scanLoading, setScanLoading] = useState(false);
  const [scanError, setScanError] = useState("");
  const [scanAsOf, setScanAsOf] = useState("");
  const [eligibleOnly, setEligibleOnly] = useState(false);
  const [scanMode, setScanMode] = useState<"auto" | "manual">("auto");
  const [marketMode, setMarketMode] = useState<"regular" | "premarket">("regular");
  const [pmRows, setPmRows] = useState<Record<string, any>[] | null>(null);
  const [pmMeta, setPmMeta] = useState<any>(null);
  const [pmLoading, setPmLoading] = useState(false);
  const [pmError, setPmError] = useState("");
  const [pmSort, setPmSort] = useState<"gap_pct" | "last" | "pm_volume" | "age_s">("gap_pct");

  const loadPremarket = async (scan = false) => {
    setPmLoading(true); setPmError("");
    try {
      const r = await fetch(`/api/scanner/premarket${scan ? "?scan=true" : ""}`);
      const j = await r.json();
      if (!r.ok) { setPmError(j.detail || j.error || "Scan failed"); setPmRows(null); return; }
      setPmRows(j.rows || []); setPmMeta(j);
    } catch { setPmError("Premarket API unavailable"); }
    finally { setPmLoading(false); }
  };
  const [manualSymbols, setManualSymbols] = useState("");
  const [pbSymbol, setPbSymbol] = useState("");
  const [pbPatterns, setPbPatterns] = useState<PullbackPattern[] | null>(null);
  const [pbLoading, setPbLoading] = useState(false);
  const [pbError, setPbError] = useState("");
  const [pbMeta, setPbMeta] = useState<{ bars: number; count: number } | null>(null);
  const [simEquity, setSimEquity] = useState("2000");
  const [simStop, setSimStop] = useState("5");
  const [simTarget, setSimTarget] = useState("15");
  const [simRisk, setSimRisk] = useState("1");
  const [simResult, setSimResult] = useState<SimResult | null>(null);
  const [simLoading, setSimLoading] = useState(false);
  const [simError, setSimError] = useState("");

  async function runBacktest() {
    setLoading(true);
    setError("");

    try {
      const response = await fetch("/api/backtest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          initial_cash: initialCash,
          hold_sessions: holdSessions,
          top_n: topN,
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : data.error || "Backtest failed",
        );
      }

      setResult(data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Unexpected error",
      );
    } finally {
      setLoading(false);
    }
  }

  async function runScan() {
    setScanLoading(true);
    setScanError("");

    const params = new URLSearchParams({ mode: scanMode });
    if (scanMode === "manual" && manualSymbols.trim()) {
      params.set("symbols", manualSymbols.trim());
    }

    try {
      const response = await fetch(
        `/api/scanner/hot-stocks?${params.toString()}`,
      );
      const data = await response.json();

      if (!response.ok) {
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : data.error || "Scan failed",
        );
      }

      setHotStocks(data.results ?? []);
      setScanMeta({
        universe: data.universe ?? [],
        total_scanned: data.total_scanned ?? 0,
        eligible_count: data.eligible_count ?? 0,
        research_candidates_count: data.research_candidates_count ?? 0,
        excluded_instruments_count: data.excluded_instruments_count ?? 0,
        discovery_mode: data.discovery_mode ?? "manual",
        discovery_source: data.discovery_source ?? "manual",
        discovery_as_of: data.discovery_as_of ?? null,
        discovery_warning: data.discovery_warning ?? null,
        is_fallback: data.is_fallback ?? false,
        market_session: data.market_session ?? "closed",
        market_is_open: data.market_is_open ?? false,
        session_warning: data.session_warning ?? null,
      });
      setScanAsOf(data.as_of ?? "");
    } catch (err) {
      setScanError(
        err instanceof Error ? err.message : "Unexpected error",
      );
    } finally {
      setScanLoading(false);
    }
  }

  const scanInFlight = useRef(false);
  const simInFlight = useRef(false);

  async function runPullbackScan() {
    const symbol = pbSymbol.trim().toUpperCase();
    if (!symbol) {
      setPbError("Select or enter a symbol.");
      return;
    }
    if (scanInFlight.current) return;  // dedupe duplicate triggers
    scanInFlight.current = true;
    setPbLoading(true);
    setPbError("");

    try {
      const response = await fetch(
        `/api/strategies/pullbacks?symbol=${encodeURIComponent(symbol)}`,
      );
      const data = await response.json();

      if (!response.ok) {
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : data.error || "Pullback analysis failed",
        );
      }

      setPbPatterns(data.patterns ?? []);
      setPbMeta({
        bars: data.bars_analyzed ?? 0,
        count: data.patterns_found ?? 0,
      });
    } catch (err) {
      setPbError(err instanceof Error ? err.message : "Unexpected error");
    } finally {
      scanInFlight.current = false;
      setPbLoading(false);
    }
  }

  async function runSimulation() {
    const symbol = pbSymbol.trim().toUpperCase();
    if (!symbol) {
      setSimError("Select a symbol first.");
      return;
    }
    if (simInFlight.current) return;  // dedupe duplicate triggers
    simInFlight.current = true;
    setSimLoading(true);
    setSimError("");
    try {
      const params = new URLSearchParams({
        symbol,
        initial_equity: simEquity,
        stop_loss_pct: simStop,
        profit_target_pct: simTarget,
        risk_pct: String(Number(simRisk) / 100),
      });
      const response = await fetch(`/api/strategies/pullback-simulation?${params}`);
      const data = await response.json();
      if (!response.ok) {
        throw new Error(
          typeof data.detail === "string" ? data.detail : "Simulation failed",
        );
      }
      setSimResult(data);
    } catch (err) {
      setSimError(err instanceof Error ? err.message : "Unexpected error");
    } finally {
      simInFlight.current = false;
      setSimLoading(false);
    }
  }

  const benchmarkByDate = new Map<string, number>(
    result?.benchmark_equity_curve ?? [],
  );
  const chartData =
    result?.equity_curve.map(([date, equity]) => {
      const benchmark = benchmarkByDate.get(date);
      return {
        date,
        equity: Number(equity.toFixed(2)),
        benchmark:
          benchmark !== undefined
            ? Number(benchmark.toFixed(2))
            : null,
      };
    }) ?? [];

  const displayedStocks =
    hotStocks?.filter(
      (s) => !eligibleOnly || s.classification === "eligible",
    ) ?? [];

  const cards = result
    ? [
        {
          label: "Final Equity",
          value: money(result.final_equity),
          detail: `Started with ${money(result.initial_cash)}`,
        },
        {
          label: "Strategy Return",
          value: percent(result.return_pct),
          detail: "Historical net performance",
        },
        {
          label: "SPY Benchmark",
          value:
            result.benchmark_return_pct === null
              ? "N/A"
              : percent(result.benchmark_return_pct),
          detail: "Buy and hold, before costs",
        },
        {
          label: "Max Drawdown",
          value: `${result.max_drawdown_pct.toFixed(2)}%`,
          detail: "Largest historical decline",
        },
      ]
    : [];

  return (
    <div className="min-h-screen bg-[#080d17] text-slate-100">
      <header className="border-b border-white/10 bg-[#0d1422]">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-5">
          <div className="flex items-center gap-3">
            <div className="rounded-xl bg-blue-500/15 p-2.5 text-blue-400">
              <Activity size={24} />
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-wide">
                VERAR TRADER
              </h1>
              <p className="text-xs text-slate-400">
                Quantitative Trading Research
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-3 py-1.5 text-xs text-emerald-400">
            <ShieldCheck size={14} />
            Historical Simulation
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-8 px-6 py-10">
        <section>
          <p className="mb-2 text-sm font-medium text-blue-400">
            STRATEGY LAB / BACKTEST V2
          </p>
          <h2 className="text-3xl font-bold tracking-tight">
            Trading Dashboard
          </h2>
          <p className="mt-2 text-slate-400">
            Analyze historical performance, portfolio risk, and
            simulated trades. No live orders are placed.
          </p>
        </section>

        <section className="rounded-2xl border border-white/10 bg-[#111b2d] p-6">
          <div className="mb-6 flex items-center gap-2">
            <BarChart3 className="text-blue-400" size={20} />
            <h3 className="text-lg font-semibold">
              Backtest Configuration
            </h3>
          </div>

          <div className="grid gap-5 md:grid-cols-4">
            <label className="space-y-2">
              <span className="text-sm text-slate-400">
                Initial Capital ($)
              </span>
              <input
                type="number"
                min={1}
                max={1000000}
                value={initialCash}
                onChange={(e) => setInitialCash(Number(e.target.value))}
                className="w-full rounded-lg border border-white/10 bg-[#080d17] px-4 py-3 outline-none focus:border-blue-500"
              />
            </label>

            <label className="space-y-2">
              <span className="text-sm text-slate-400">
                Holding Sessions
              </span>
              <input
                type="number"
                min={1}
                max={100}
                value={holdSessions}
                onChange={(e) => setHoldSessions(Number(e.target.value))}
                className="w-full rounded-lg border border-white/10 bg-[#080d17] px-4 py-3 outline-none focus:border-blue-500"
              />
            </label>

            <label className="space-y-2">
              <span className="text-sm text-slate-400">
                Max Positions
              </span>
              <input
                type="number"
                min={1}
                max={20}
                value={topN}
                onChange={(e) => setTopN(Number(e.target.value))}
                className="w-full rounded-lg border border-white/10 bg-[#080d17] px-4 py-3 outline-none focus:border-blue-500"
              />
            </label>

            <div className="flex items-end">
              <button
                onClick={runBacktest}
                disabled={loading}
                className="flex w-full items-center justify-center gap-2 rounded-lg bg-blue-600 px-5 py-3 font-semibold transition hover:bg-blue-500 disabled:opacity-50"
              >
                {loading ? (
                  <LoaderCircle className="animate-spin" size={18} />
                ) : (
                  <Play size={18} />
                )}
                {loading ? "Running..." : "Run Backtest"}
              </button>
            </div>
          </div>

          {error && (
            <p role="alert" className="mt-4 text-sm text-red-400">
              {error}
            </p>
          )}
        </section>

        <div className="flex items-center gap-2">
          {(["regular", "premarket"] as const).map((m) => (
            <button key={m} onClick={() => {
                setMarketMode(m);
                if (m === "premarket" && pmRows === null) loadPremarket(); }}
              className={`rounded px-3 py-1 text-xs font-medium ${
                marketMode === m ? "bg-cyan-600" : "bg-[#111b2d] text-slate-400"}`}>
              {m === "regular" ? "Regular" : "Premarket"}</button>
          ))}
          {marketMode === "premarket" && (
            <>
              <button onClick={() => loadPremarket()} className="rounded bg-[#111b2d] px-3 py-1 text-xs">Refresh</button>
              <button onClick={() => loadPremarket(true)} className="rounded bg-emerald-700 px-3 py-1 text-xs">
                Scan broad universe</button>
              <span className="rounded bg-amber-900/40 px-2 py-1 text-[10px] text-amber-300">
                IEX PARTIAL — not consolidated</span>
            </>)}
        </div>
        {marketMode === "premarket" && (
        <section className="rounded-2xl border border-white/10 bg-[#111b2d] p-6">
          <div className="mb-4 flex items-center justify-between">
            <h3 className="text-lg font-semibold">Premarket Momentum — IEX Partial</h3>
          </div>
          {pmMeta?.market === "premarket_pending" && (
            <p className="text-sm text-slate-400">Premarket has not started.</p>)}
          {pmMeta?.market === "premarket" && (
            <p className="text-xs text-amber-300">Premarket monitoring — IEX partial data.</p>)}
          {pmMeta?.market === "premarket_ended" && (
            <p className="text-sm text-slate-400">Premarket session ended — observations below are historical, not live.</p>)}
          {pmMeta?.market === "premarket_not_active" && (
            <p className="text-sm text-slate-400">Outside the 04:00–09:29 ET premarket window or non-trading day.</p>)}
          {pmMeta?.scan && pmMeta.scan.requests > 0 && (
            <p className="mb-3 text-xs text-slate-400">
              Broad scan: {pmMeta.scan.scanned} scanned · {pmMeta.scan.candidates} candidates
              {pmMeta.scan.cached ? ` · cached ${pmMeta.scan.cache_age_s}s ago` : " · fresh"}</p>)}
          {pmError && <p className="text-sm text-red-400">{pmError}</p>}
          {pmLoading && <p className="text-sm text-slate-400">Loading…</p>}
          {pmRows && pmRows.length === 0 && !pmLoading && !pmError && (
            <p className="text-sm text-slate-400">No symbols monitored — try “Scan broad universe”.</p>)}
          {pmRows && pmRows.length > 0 && (
            <table className="w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="cursor-pointer pb-2" onClick={() => setPmSort("gap_pct")}>gap%</th>
                  <th>symbol</th><th>src</th>
                  <th className="cursor-pointer" onClick={() => setPmSort("last")}>last</th>
                  <th className="cursor-pointer" onClick={() => setPmSort("pm_volume")}>pm vol</th>
                  <th>hi/lo</th><th>rvol</th><th className="cursor-pointer" onClick={() => setPmSort("age_s")}>age</th>
                  <th>status</th>
                </tr>
              </thead>
              <tbody>
                {[...pmRows].sort((a, b) => (Number(b[pmSort]) || -1) - (Number(a[pmSort]) || -1)).map(r => (
                  <tr key={`${r.symbol}|${r.source}`}
                    className={r.qualified ? "text-emerald-400" : Number(r.gap_pct) >= 5 ? "text-amber-300" : "text-slate-400"}>
                    <td className="py-0.5">{r.gap_pct != null ? `${r.gap_pct}%` : "—"}</td>
                    <td className="font-mono">{String(r.symbol)}</td>
                    <td className="text-xs">{String(r.source)}</td>
                    <td>{r.last ?? "—"}</td>
                    <td>{r.pm_volume != null ? Number(r.pm_volume).toLocaleString() : "—"}</td>
                    <td className="text-xs">{r.pm_high ?? "—"}/{r.pm_low ?? "—"}</td>
                    <td>{r.rvol != null ? `${r.rvol}×` : "—"}</td>
                    <td className="text-xs">{r.age_s != null ? `${r.age_s}s` : "—"}</td>
                    <td className="text-xs">{String(r.status)}</td>
                  </tr>))}
              </tbody>
            </table>)}
        </section>)}
        {marketMode === "regular" && (
        <section className="rounded-2xl border border-white/10 bg-[#111b2d] p-6">
          <div className="mb-6 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Flame className="text-amber-400" size={20} />
              <div>
                <h3 className="text-lg font-semibold">
                  Hot Stocks — Intraday Scanner
                </h3>
                <p className="text-sm text-slate-400">
                  Read-only momentum and volume scan. Ranking heuristic,
                  not a recommendation.
                </p>
              </div>
            </div>
            <div className="flex items-center gap-4">
              <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-300">
                <input
                  type="checkbox"
                  checked={eligibleOnly}
                  onChange={(e) => setEligibleOnly(e.target.checked)}
                  className="h-4 w-4 accent-emerald-500"
                />
                Eligible only
              </label>
              <button
                onClick={runScan}
                disabled={scanLoading}
                className="flex items-center gap-2 rounded-lg bg-amber-600 px-4 py-2 text-sm font-semibold transition hover:bg-amber-500 disabled:opacity-50"
              >
                {scanLoading ? (
                  <LoaderCircle className="animate-spin" size={16} />
                ) : (
                  <Flame size={16} />
                )}
                {scanLoading ? "Scanning..." : "Run Scan"}
              </button>
            </div>
          </div>

          <div className="mb-6 flex flex-wrap items-center gap-3">
            <div className="flex rounded-lg border border-white/10 p-1 text-xs font-medium">
              {marketMode === "regular" && (["auto", "manual"] as const).map((mode) => (
                <button
                  key={mode}
                  onClick={() => setScanMode(mode)}
                  className={`rounded-md px-3 py-1.5 transition ${
                    scanMode === mode
                      ? "bg-amber-600 text-white"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {mode === "auto" ? "Auto Discover" : "Manual Symbols"}
                </button>
              ))}
            </div>
            {scanMode === "manual" && (
              <input
                type="text"
                value={manualSymbols}
                onChange={(e) => setManualSymbols(e.target.value)}
                placeholder="NVDA, TSLA, AMD (blank = default universe)"
                className="w-72 rounded-lg border border-white/10 bg-[#080d17] px-3 py-1.5 text-xs outline-none focus:border-amber-500"
              />
            )}
          </div>

          {scanMeta && (
            <div className="mb-4 space-y-1.5 text-xs text-slate-400">
              {!scanMeta.market_is_open && (
                <p className="rounded-lg border border-slate-500/30 bg-slate-500/10 px-3 py-2 text-slate-300">
                  Market closed — results are for research only.
                </p>
              )}
              {scanMeta.session_warning && (
                <p className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-amber-300">
                  {scanMeta.session_warning}
                </p>
              )}
              {scanMeta.discovery_warning && (
                <p className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-amber-300">
                  {scanMeta.discovery_warning}
                </p>
              )}
              <p>
                <span
                  className={`mr-2 inline-block rounded-full border px-2 py-0.5 font-medium ${
                    scanMeta.market_is_open
                      ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-400"
                      : "border-slate-500/30 bg-slate-500/10 text-slate-300"
                  }`}
                >
                  {sessionLabels[scanMeta.market_session] ??
                    scanMeta.market_session}
                </span>
                {scanMeta.discovery_mode === "auto"
                  ? "Auto discovery"
                  : "Manual universe"}
                {" · Source: "}
                <span className="font-medium text-slate-300">
                  {scanMeta.discovery_source}
                </span>
                {" · "}
                {scanMeta.universe.length} discovered
                {scanMeta.excluded_instruments_count > 0 &&
                  ` · ${scanMeta.excluded_instruments_count} unsupported excluded`}
                {" · "}
                {scanMeta.total_scanned} evaluated
                {" · "}
                <span className="font-semibold text-emerald-400">
                  {scanMeta.eligible_count} eligible
                </span>
                {" · "}
                <span className="font-semibold text-blue-400">
                  {scanMeta.research_candidates_count} research
                </span>
                {scanMeta.discovery_as_of &&
                  ` · Discovered ${new Date(
                    scanMeta.discovery_as_of,
                  ).toLocaleString(undefined, {
                    dateStyle: "medium",
                    timeStyle: "medium",
                  })}`}
                {scanAsOf &&
                  ` · Scanned ${new Date(scanAsOf).toLocaleString(
                    undefined,
                    { dateStyle: "medium", timeStyle: "medium" },
                  )}`}
              </p>
            </div>
          )}

          {scanError && (
            <p role="alert" className="mb-4 text-sm text-red-400">
              {scanError}
            </p>
          )}

          {hotStocks === null ? (
            <p className="py-8 text-center text-sm text-slate-500">
              Run a scan to rank the universe by intraday momentum,
              relative volume, and liquidity.
            </p>
          ) : hotStocks.length === 0 ? (
            <p className="py-8 text-center text-sm text-slate-500">
              No candidates returned. The market may be closed or data
              may be unavailable.
            </p>
          ) : displayedStocks.length === 0 ? (
            <p className="py-8 text-center text-sm text-slate-500">
              No eligible candidates — {scanMeta?.total_scanned ?? 0}{" "}
              symbols scanned, all filtered or unavailable.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-white/10 text-slate-400">
                  <tr>
                    <th className="pb-3">Symbol</th>
                    <th className="pb-3">Status</th>
                    <th className="pb-3 text-right">Price</th>
                    <th className="pb-3 text-right">Change %</th>
                    <th className="pb-3 text-right">Rel Vol</th>
                    <th className="pb-3 text-right">$ Volume</th>
                    <th className="pb-3 text-right">Score</th>
                  </tr>
                </thead>
                <tbody>
                  {displayedStocks.map((stock) => (
                    <tr
                      key={stock.symbol}
                      className="border-b border-white/5"
                    >
                      <td className="py-3 font-semibold">
                        <Link
                          href={`/terminal?symbol=${stock.symbol}`}
                          className="text-blue-400 transition hover:text-blue-300 hover:underline"
                          title={`Open ${stock.symbol} in Trading Terminal`}
                        >
                          {stock.symbol}
                        </Link>
                        {stock.reasons.length > 0 && (
                          <p className="mt-0.5 max-w-64 text-xs font-normal text-slate-500">
                            {stock.reasons.join("; ")}
                          </p>
                        )}
                      </td>
                      <td className="py-3">
                        <span
                          className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${classificationStyles[stock.classification] ?? classificationStyles.unavailable}`}
                        >
                          {stock.classification}
                        </span>
                        {stock.ross_eligible && (
                          <span
                            className="ml-1.5 inline-block rounded-full border border-cyan-500/40 bg-cyan-500/10 px-2 py-0.5 text-xs font-medium text-cyan-300"
                            title={`VERAR momentum candidate ($1–$10, +10%, 5x RVOL). Unverified: ${stock.ross_unverified.join(", ")}`}
                          >
                            VERAR
                          </span>
                        )}
                      </td>
                      <td className="py-3 text-right tabular-nums">
                        {stock.last_price !== null
                          ? money(stock.last_price)
                          : "—"}
                      </td>
                      <td
                        className={`py-3 text-right tabular-nums ${
                          (stock.change_pct ?? 0) >= 0
                            ? "text-emerald-400"
                            : "text-red-400"
                        }`}
                      >
                        {stock.change_pct !== null
                          ? percent(stock.change_pct * 100)
                          : "—"}
                      </td>
                      <td className="py-3 text-right tabular-nums">
                        {stock.relative_volume !== null
                          ? `${stock.relative_volume.toFixed(1)}x`
                          : "—"}
                      </td>
                      <td className="py-3 text-right tabular-nums">
                        {compactMoney(stock.dollar_volume)}
                      </td>
                      <td className="py-3 text-right font-semibold tabular-nums">
                        {stock.score !== null
                          ? stock.score.toFixed(1)
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>)}

        {!result ? (
          <section className="rounded-2xl border border-dashed border-white/15 bg-[#111b2d]/50 px-6 py-20 text-center">
            <BarChart3 className="mx-auto mb-4 text-slate-500" size={40} />
            <h3 className="text-xl font-semibold">
              Ready to analyze your strategy
            </h3>
            <p className="mt-2 text-slate-400">
              Configure your parameters and run a historical backtest
              to view real simulation results.
            </p>
          </section>
        ) : (
          <>
            <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {cards.map((card) => (
                <div
                  key={card.label}
                  className="rounded-2xl border border-white/10 bg-[#111b2d] p-6"
                >
                  <p className="text-sm text-slate-400">{card.label}</p>
                  <p className="mt-3 text-2xl font-bold tabular-nums">
                    {card.value}
                  </p>
                  <p className="mt-2 text-xs text-slate-500">
                    {card.detail}
                  </p>
                </div>
              ))}
            </section>

            <section className="rounded-2xl border border-white/10 bg-[#111b2d] p-6">
              <div className="mb-6">
                <h3 className="text-lg font-semibold">
                  Portfolio Equity Curve
                </h3>
                <p className="text-sm text-slate-400">
                  Historical marked-to-market portfolio value vs
                  SPY buy &amp; hold
                </p>
              </div>

              <div className="h-80 w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={chartData}>
                    <CartesianGrid
                      stroke="#253248"
                      strokeDasharray="3 3"
                    />
                    <XAxis
                      dataKey="date"
                      stroke="#94a3b8"
                      tick={{ fontSize: 11 }}
                      minTickGap={40}
                    />
                    <YAxis
                      stroke="#94a3b8"
                      tick={{ fontSize: 11 }}
                      domain={["auto", "auto"]}
                      tickFormatter={(value: number) =>
                        `$${Math.round(value)}`
                      }
                    />
                    <Tooltip
                      contentStyle={{
                        backgroundColor: "#111b2d",
                        border: "1px solid #334155",
                        borderRadius: 10,
                      }}
                      formatter={(value) =>
                        money(Number(value ?? 0))
                      }
                    />
                    <Legend />
                    <Line
                      type="monotone"
                      dataKey="equity"
                      stroke="#60a5fa"
                      strokeWidth={2}
                      dot={false}
                      name="VERAR Strategy"
                    />
                    <Line
                      type="monotone"
                      dataKey="benchmark"
                      stroke="#f59e0b"
                      strokeWidth={2}
                      strokeDasharray="6 3"
                      dot={false}
                      name="SPY Buy & Hold"
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </section>

            <section className="grid gap-4 md:grid-cols-3">
              <div className="rounded-2xl border border-white/10 bg-[#111b2d] p-6">
                <p className="text-sm text-slate-400">
                  Simulated Entries
                </p>
                <div className="mt-3 flex items-center gap-2">
                  <ArrowUpRight className="text-blue-400" />
                  <span className="text-2xl font-bold">
                    {result.trades}
                  </span>
                </div>
              </div>

              <div className="rounded-2xl border border-white/10 bg-[#111b2d] p-6">
                <p className="text-sm text-slate-400">
                  Closed Trades
                </p>
                <div className="mt-3 flex items-center gap-2">
                  <ArrowDownRight className="text-blue-400" />
                  <span className="text-2xl font-bold">
                    {result.closed_trades.length}
                  </span>
                </div>
              </div>

              <div className="rounded-2xl border border-white/10 bg-[#111b2d] p-6">
                <p className="text-sm text-slate-400">
                  Selected Symbols
                </p>
                <p className="mt-3 text-lg font-semibold">
                  {result.selected_symbols.join(", ")}
                </p>
              </div>
            </section>

            <section className="rounded-2xl border border-white/10 bg-[#111b2d] p-6">
              <h3 className="mb-5 text-lg font-semibold">
                Recent Closed Trades
              </h3>

              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="border-b border-white/10 text-slate-400">
                    <tr>
                      <th className="pb-3">Ticker</th>
                      <th className="pb-3">Entry</th>
                      <th className="pb-3">Exit</th>
                      <th className="pb-3 text-right">Net P/L</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.closed_trades
                      .slice(-10)
                      .reverse()
                      .map((trade, index) => (
                        <tr
                          key={`${trade.ticker}-${trade.entry_date}-${index}`}
                          className="border-b border-white/5"
                        >
                          <td className="py-3 font-semibold">
                            {trade.ticker}
                          </td>
                          <td className="py-3 text-slate-400">
                            {trade.entry_date}
                          </td>
                          <td className="py-3 text-slate-400">
                            {trade.exit_date}
                          </td>
                          <td
                            className={`py-3 text-right font-medium ${
                              trade.net_pnl >= 0
                                ? "text-emerald-400"
                                : "text-red-400"
                            }`}
                          >
                            {money(trade.net_pnl)}
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            </section>
          </>
        )}

        <section className="rounded-2xl border border-white/10 bg-[#111b2d] p-6">
          <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
            <div>
              <h3 className="text-lg font-semibold">Pullback Research</h3>
              <p className="text-sm text-slate-400">
                Deterministic impulse → pullback → recovery detection on
                1-minute bars. Research only — no trade signals.
              </p>
            </div>
            <div className="flex items-center gap-3">
              <select
                value={pbSymbol}
                onChange={(e) => setPbSymbol(e.target.value)}
                className="rounded-lg border border-white/10 bg-[#080d17] px-3 py-2 text-xs outline-none focus:border-blue-500"
              >
                <option value="">Select symbol…</option>
                {(hotStocks ?? []).map((s) => (
                  <option key={s.symbol} value={s.symbol}>
                    {s.symbol}
                  </option>
                ))}
              </select>
              <input
                type="text"
                value={pbSymbol}
                onChange={(e) => setPbSymbol(e.target.value.toUpperCase())}
                placeholder="or type symbol"
                className="w-32 rounded-lg border border-white/10 bg-[#080d17] px-3 py-2 text-xs outline-none focus:border-blue-500"
              />
              <button
                onClick={runPullbackScan}
                disabled={pbLoading}
                className="flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold transition hover:bg-blue-500 disabled:opacity-50"
              >
                {pbLoading ? (
                  <LoaderCircle className="animate-spin" size={16} />
                ) : (
                  <BarChart3 size={16} />
                )}
                {pbLoading ? "Analyzing..." : "Analyze"}
              </button>
            </div>
          </div>

          {pbError && (
            <p role="alert" className="mb-4 text-sm text-red-400">
              {pbError}
            </p>
          )}

          {pbPatterns === null ? (
            <p className="py-6 text-center text-sm text-slate-500">
              Choose a symbol to analyze recent 1-minute bars for pullback
              patterns.
            </p>
          ) : pbPatterns.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">
              No pullback patterns detected in {pbMeta?.bars ?? 0} bars
              analyzed.
            </p>
          ) : (
            <>
              <p className="mb-4 text-xs text-slate-400">
                {pbMeta?.count} pattern
                {pbMeta?.count === 1 ? "" : "s"} in {pbMeta?.bars} bars
                analyzed
              </p>
              <div className="space-y-3">
                {pbPatterns.map((p, i) => (
                  <div
                    key={`${p.impulse_start_ts}-${i}`}
                    className="rounded-xl border border-white/10 bg-[#0d1422] p-4 text-xs"
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-semibold">{p.symbol}</span>
                      <span
                        className={`rounded-full border px-2 py-0.5 font-medium ${
                          p.state === "recovery_confirmed"
                            ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-400"
                            : p.state === "invalidated"
                              ? "border-red-500/30 bg-red-500/10 text-red-400"
                              : "border-blue-500/30 bg-blue-500/10 text-blue-400"
                        }`}
                      >
                        {p.state.replace(/_/g, " ")}
                      </span>
                    </div>
                    <div className="mt-2 grid gap-1 text-slate-400 sm:grid-cols-2">
                      <span>
                        Impulse: {p.impulse_low.toFixed(2)} →{" "}
                        {p.impulse_high.toFixed(2)} (
                        {p.impulse_gain_pct.toFixed(1)}%)
                      </span>
                      <span>
                        Retracement:{" "}
                        {p.retracement_pct !== null
                          ? `${p.retracement_pct.toFixed(1)}%`
                          : "n/a"}
                        {p.pullback_low !== null &&
                          ` (low ${p.pullback_low.toFixed(2)})`}
                      </span>
                      <span>
                        Impulse: {new Date(p.impulse_start_ts).toLocaleTimeString()}{" "}
                        → {new Date(p.impulse_high_ts).toLocaleTimeString()}
                      </span>
                      <span>
                        {p.recovery_ts
                          ? `Confirmed ${new Date(p.recovery_ts).toLocaleTimeString()}`
                          : p.invalidation_reason ?? "awaiting recovery"}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </section>

        <section className="rounded-2xl border border-white/10 bg-[#111b2d] p-6">
          <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
            <div>
              <h3 className="text-lg font-semibold">Pullback Simulation</h3>
              <p className="text-sm text-slate-400">
                Historical simulation of confirmed patterns — not actual
                trading performance.
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs">
              {(
                [
                  ["Equity $", simEquity, setSimEquity],
                  ["Stop %", simStop, setSimStop],
                  ["Risk %", simRisk, setSimRisk],
                ] as const
              ).map(([label, val, set]) => (
                <label key={label} className="flex items-center gap-1 text-slate-400">
                  {label}
                  <input
                    type="number"
                    value={val}
                    onChange={(e) => set(e.target.value)}
                    className="w-20 rounded-lg border border-white/10 bg-[#080d17] px-2 py-2 outline-none focus:border-blue-500"
                  />
                </label>
              ))}
              <label className="flex items-center gap-1 text-slate-400">
                Target
                <select
                  value={simTarget}
                  onChange={(e) => setSimTarget(e.target.value)}
                  className="rounded-lg border border-white/10 bg-[#080d17] px-2 py-2 outline-none focus:border-blue-500"
                >
                  <option value="15">15%</option>
                  <option value="20">20%</option>
                </select>
              </label>
              <button
                onClick={runSimulation}
                disabled={simLoading}
                className="flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold transition hover:bg-blue-500 disabled:opacity-50"
              >
                {simLoading ? (
                  <LoaderCircle className="animate-spin" size={16} />
                ) : (
                  <BarChart3 size={16} />
                )}
                {simLoading ? "Simulating..." : "Run Simulation"}
              </button>
            </div>
          </div>

          {simError && (
            <p role="alert" className="mb-4 text-sm text-red-400">
              {simError}
            </p>
          )}

          {simResult === null ? (
            <p className="py-6 text-center text-sm text-slate-500">
              Run a simulation on the selected symbol. Uses the same symbol as
              Pullback Research.
            </p>
          ) : (
            <>
              <div className="mb-4 grid grid-cols-2 gap-3 text-xs sm:grid-cols-4 lg:grid-cols-7">
                {(
                  [
                    ["Patterns", simResult.patterns_confirmed],
                    ["Trades", simResult.trades_entered],
                    ["Win rate", simResult.win_rate !== null ? `${simResult.win_rate}%` : "n/a"],
                    ["Gross P/L", money(simResult.gross_pnl)],
                    ["Net P/L", money(simResult.net_pnl)],
                    ["End equity", money(simResult.ending_equity)],
                    ["Max DD", `${simResult.max_drawdown}%`],
                  ] as const
                ).map(([label, val]) => (
                  <div key={label} className="rounded-lg border border-white/10 bg-[#0d1422] p-3">
                    <p className="text-slate-500">{label}</p>
                    <p className="mt-1 font-semibold text-slate-200">{val}</p>
                  </div>
                ))}
              </div>
              {Object.keys(simResult.exit_reasons).length > 0 && (
                <p className="mb-4 text-xs text-slate-400">
                  Exits:{" "}
                  {Object.entries(simResult.exit_reasons)
                    .map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`)
                    .join(" · ")}
                  {simResult.skipped.length > 0 &&
                    ` · skipped: ${simResult.skipped.map((s) => s[1]).join(", ")}`}
                </p>
              )}
              {simResult.trades.length === 0 ? (
                <p className="py-4 text-center text-sm text-slate-500">
                  No trades simulated.
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead>
                      <tr className="border-b border-white/10 text-slate-400">
                        {["Entry", "Entry Price", "Shares", "Exit", "Exit Price", "Reason", "Net P/L"].map((h) => (
                          <th key={h} className="px-2 py-2 font-medium">{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {simResult.trades.map((t) => (
                        <tr key={t.entry_ts} className="border-b border-white/5">
                          <td className="px-2 py-2">{new Date(t.entry_ts).toLocaleString()}</td>
                          <td className="px-2 py-2">${t.entry_price.toFixed(2)}</td>
                          <td className="px-2 py-2">{t.shares}</td>
                          <td className="px-2 py-2">{new Date(t.exit_ts).toLocaleTimeString()}</td>
                          <td className="px-2 py-2">${t.exit_price.toFixed(2)}</td>
                          <td className="px-2 py-2">{t.exit_reason.replace(/_/g, " ")}</td>
                          <td className={`px-2 py-2 font-semibold ${t.net_pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                            {money(t.net_pnl)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </section>

        <footer className="border-t border-white/10 pt-6 text-xs text-slate-500">
          VERAR BOT TRADER · Historical research only · No live
          trading orders
        </footer>
      </main>
    </div>
  );
}


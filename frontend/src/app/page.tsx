
"use client";

import { useState } from "react";
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
  classification: "eligible" | "filtered" | "unavailable";
  reasons: string[];
  bar_freshness: "fresh" | "stale" | "none";
  quote_available: boolean;
  as_of: string | null;
};

type ScanMeta = {
  universe: string[];
  total_scanned: number;
  eligible_count: number;
};

const classificationStyles: Record<string, string> = {
  eligible:
    "border-emerald-500/30 bg-emerald-500/10 text-emerald-400",
  filtered: "border-amber-500/30 bg-amber-500/10 text-amber-400",
  unavailable: "border-slate-500/30 bg-slate-500/10 text-slate-400",
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

    try {
      const response = await fetch("/api/scanner/hot-stocks");
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

          {scanMeta && (
            <p className="mb-4 text-xs text-slate-400">
              <span className="font-semibold text-emerald-400">
                {scanMeta.eligible_count} eligible
              </span>
              {" · "}
              {scanMeta.total_scanned} scanned
              {" · Universe: "}
              {scanMeta.universe.join(", ")}
              {scanAsOf &&
                ` · Scanned at ${new Date(scanAsOf).toLocaleString(
                  undefined,
                  { dateStyle: "medium", timeStyle: "medium" },
                )}`}
            </p>
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
                        {stock.symbol}
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
        </section>

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

        <footer className="border-t border-white/10 pt-6 text-xs text-slate-500">
          VERAR BOT TRADER · Historical research only · No live
          trading orders
        </footer>
      </main>
    </div>
  );
}


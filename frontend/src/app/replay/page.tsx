"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import CandleChart, { type OhlcvBar, type VwapPoint, type PriceLine } from "../terminal/CandleChart";
import { newAccount, placeOrder, stepBar, finalize, journal, equity, unrealized,
         type Account, type PendingOrder } from "./replayEngine";

type Session = { symbol: string; session: string; coverage_pct: number };
type Bar = { t: string; o: number; h: number; l: number; c: number; v: number };
type ReplayData = { bars: Bar[]; feed: string; coverage_pct: number; note: string };

const ET = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", hour: "2-digit", minute: "2-digit",
  month: "short", day: "2-digit", hour12: false });

export default function Replay() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sel, setSel] = useState("");
  const [data, setData] = useState<ReplayData | null>(null);
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [error, setError] = useState("");
  const [acc, setAcc] = useState<Account>(newAccount());
  const [qty, setQty] = useState(10);
  const [stop, setStop] = useState("");
  const [tgt, setTgt] = useState("");
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    fetch("/api/research/sessions").then(r => r.json())
      .then(d => setSessions(d.sessions ?? [])).catch(() => setSessions([]));
  }, []);

  const load = async (s: string) => {
    setSel(s); setError(""); setPlaying(false); setCursor(0); setData(null);
    const [symbol, date] = s.split("|");
    const r = await fetch(`/api/research/replay?symbol=${symbol}&date=${date}`);
    const j = await r.json();
    if (!r.ok) { setError(j.detail || "Snapshot unavailable"); return; }
    setAcc(newAccount());
    setData(j);
  };

  const tick = useCallback(() => {
    setCursor(c => {
      if (!data || c >= data.bars.length - 1) {
        if (data && c >= data.bars.length - 1) setPlaying(false);
        return c;
      }
      const nxt = c + 1;
      setAcc(a => {
        a = stepBar(a, data.bars[nxt]);          // exec on newly revealed bar
        if (nxt === data.bars.length - 1)        // end of session: finalize
          a = finalize(a, data.bars[nxt], sessionEnd(data.bars[nxt].t));
        return a;
      });
      return nxt;
    });
  }, [data]);

  // true only when the last bar is a genuine close bar (15:59 or 12:59 ET)
  const sessionEnd = (t: string) => {
    const et = ET.format(new Date(t));
    return /15:59|12:59/.test(et);
  };

  const order = (side: PendingOrder["side"]) => {
    const so = { side, qty,
      stop: stop ? +stop : undefined, target: tgt ? +tgt : undefined };
    setAcc(a => placeOrder(a, so));
  };

  useEffect(() => {
    if (timer.current) clearInterval(timer.current);
    timer.current = playing ? setInterval(tick, 60000 / speed / 60) : null;
    return () => { if (timer.current) clearInterval(timer.current); };
  }, [playing, speed, tick]);

  const visible: OhlcvBar[] = useMemo(() =>
    (data?.bars ?? []).slice(0, cursor + 1).map(b => ({
      time: Math.floor(new Date(b.t).getTime() / 1000),
      open: b.o, high: b.h, low: b.l, close: b.c, volume: b.v,
    })), [data, cursor]);

  const vwap: VwapPoint[] = useMemo(() => {
    let pv = 0, vv = 0;
    return visible.map(b => {
      const tp = (b.high + b.low + b.close) / 3;
      pv += tp * b.volume; vv += b.volume;
      return { time: b.time, value: vv ? pv / vv : tp };
    });
  }, [visible]);

  const cur = data?.bars[cursor];
  const lines: PriceLine[] = [];
  if (acc.position?.stop) lines.push({ price: acc.position.stop, color: "#f87171", title: "stop" });
  if (acc.position?.target) lines.push({ price: acc.position.target, color: "#34d399", title: "target" });
  if (acc.position) lines.push({ price: acc.position.entry, color: "#22d3ee", title: "entry" });
  const pct = data ? Math.round(100 * cursor / (data.bars.length - 1)) : 0;

  return (
    <main className="min-h-screen bg-[#0a0f1a] p-4 text-slate-100">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-bold">Market Replay</h1>
        <span className="rounded bg-amber-900/50 px-3 py-1 text-xs text-amber-300">
          {data?.note ?? "historical replay — not live trading"}
        </span>
      </div>
      <div className="mb-4 flex flex-wrap items-center gap-3 text-sm">
        <select value={sel} onChange={e => load(e.target.value)}
          className="rounded bg-[#111b2d] px-3 py-1.5">
          <option value="">Select session…</option>
          {sessions.map(s => (
            <option key={`${s.symbol}|${s.session}`} value={`${s.symbol}|${s.session}`}>
              {s.symbol} — {s.session} ({s.coverage_pct}%)
            </option>
          ))}
        </select>
        {data && <>
          <button onClick={() => setPlaying(p => !p)} disabled={cursor >= data.bars.length - 1}
            className="rounded bg-cyan-600 px-4 py-1.5 disabled:opacity-40">
            {playing ? "Pause" : "Play"}</button>
          <button onClick={() => setCursor(c => Math.max(0, c - 1))}
            disabled={acc.traded}
            className="rounded bg-[#111b2d] px-3 py-1.5 disabled:opacity-30">◀</button>
          <button onClick={tick} disabled={cursor >= data.bars.length - 1}
            className="rounded bg-[#111b2d] px-3 py-1.5 disabled:opacity-40">▶</button>
          <button onClick={() => { setPlaying(false); setCursor(0); setAcc(newAccount()); }}
            className="rounded bg-[#111b2d] px-3 py-1.5">Reset</button>
          {[1, 5, 20].map(x => (
            <button key={x} onClick={() => setSpeed(x)}
              className={`rounded px-2 py-1.5 text-xs ${speed === x ? "bg-cyan-700" : "bg-[#111b2d]"}`}>
              {x}×</button>))}
          <span className="text-slate-400">
            {cur ? ET.format(new Date(cur.t)) : "—"} · {pct}% · {data.feed}
            {data.coverage_pct != null ? ` · ${data.coverage_pct}% cov` : ""}
          </span>
        </>}
      </div>
      {error && <p className="mb-3 text-sm text-red-400">{error}</p>}
      {data && <CandleChart bars={visible} vwap={vwap} showVwap priceLines={lines} />}
      {data && (
        <div className="mt-3 space-y-3 rounded bg-[#111b2d] p-3 text-sm">
          <div className="flex flex-wrap items-center gap-3">
            <label className="text-slate-400">qty
              <input type="number" value={qty} min={1}
                onChange={e => setQty(Math.max(1, +e.target.value))}
                className="ml-1 w-16 rounded bg-[#0a0f1a] px-2 py-1" /></label>
            <button onClick={() => order("buy")} disabled={!!acc.position || !!acc.pending}
              className="rounded bg-emerald-600 px-3 py-1 disabled:opacity-40">Buy</button>
            <button onClick={() => order("sell")} disabled={!acc.position || !!acc.pending}
              className="rounded bg-red-600 px-3 py-1 disabled:opacity-40">Sell</button>
            <label className="text-slate-400">stop
              <input value={stop} onChange={e => setStop(e.target.value)}
                className="ml-1 w-20 rounded bg-[#0a0f1a] px-2 py-1" /></label>
            <label className="text-slate-400">target
              <input value={tgt} onChange={e => setTgt(e.target.value)}
                className="ml-1 w-20 rounded bg-[#0a0f1a] px-2 py-1" /></label>
          </div>
          <div className="flex flex-wrap gap-x-5 gap-y-1 text-slate-300">
            <span>Start $1,000.00</span>
            <span>Cash ${acc.cash.toFixed(2)}</span>
            <span>Equity ${equity(acc, cur ? cur.c : 0).toFixed(2)}</span>
            {/* convention: when flat, realized displays as cash-start so
                cash and realized reconcile to the cent (exact internally) */}
            {(() => { const rd = acc.position ? acc.realized : acc.cash - 1000;
              return <span className={rd >= 0 ? "text-emerald-400" : "text-red-400"}>
                Realized {rd >= 0 ? "+" : ""}${rd.toFixed(2)}</span>; })()}
            <span>U/R ${unrealized(acc, cur ? cur.c : 0).toFixed(2)}</span>
            {acc.position && <span className="text-cyan-300">open {acc.position.qty} @ {acc.position.entry}</span>}
            {acc.pending && <span className="text-amber-300">pending {acc.pending.side} {acc.pending.qty}</span>}
            {acc.rejected && <span className="text-red-400">{acc.rejected}</span>}
            {acc.fills.some(f => f.reason === "canceled") &&
              <span className="text-slate-500">order canceled (no next bar)</span>}
          </div>
          {(() => {
            const tr = journal(acc);
            if (!tr.length) return null;
            const w = tr.filter(t => t.pnl > 0).length;
            const net = tr.reduce((s, t) => s + t.pnl, 0);
            return (
              <div>
                <div className="mb-1 flex gap-4 text-xs text-slate-400">
                  <span>trades {tr.length}</span><span>W {w} / L {tr.length - w}</span>
                  <span>wr {(100 * w / tr.length).toFixed(0)}%</span>
                  <span>net {net >= 0 ? "+" : ""}${net.toFixed(2)}</span>
                  <span>avg {(net / tr.length >= 0 ? "+" : "")}${(net / tr.length).toFixed(2)}</span>
                </div>
                <table className="w-full text-left text-xs">
                  <thead className="text-slate-500">
                    <tr><th>entry</th><th>exit</th><th>qty</th><th>px in</th>
                        <th>px out</th><th>pnl</th><th>reason</th></tr>
                  </thead>
                  <tbody>
                    {tr.map(t => (
                      <tr key={`${t.entryTs}|${t.exitTs}|${t.qty}|${t.entry}|${t.exit}`} className={t.pnl >= 0 ? "text-emerald-400" : "text-red-400"}>
                        <td>{ET.format(new Date(t.entryTs))}</td>
                        <td>{ET.format(new Date(t.exitTs))}</td>
                        <td>{t.qty}</td><td>{t.entry.toFixed(4)}</td>
                        <td>{t.exit.toFixed(4)}</td>
                        <td>{t.pnl >= 0 ? "+" : ""}{t.pnl.toFixed(2)}</td>
                        <td>{t.reason}</td>
                      </tr>))}
                  </tbody>
                </table>
              </div>);
          })()}
        </div>)}
    </main>
  );
}

"use client";

import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  LineSeries,
  TickMarkType,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type UTCTimestamp,
} from "lightweight-charts";

export type OhlcvBar = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type VwapPoint = { time: number; value: number };

export type TradeMark = {
  time: number;
  kind: "entry" | "exit_target" | "exit_stop" | "exit_session" | "exit_other" | "setup" | "setup_pending";
  label: string;
};

export type PriceLine = { price: number; color: string; title: string };

const ET_TIME = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const ET_DATE = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  month: "short",
  day: "numeric",
});
const ET_FULL = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZoneName: "short",
});

function etMinutes(ts: number): number {
  const [h, m] = ET_TIME.format(ts * 1000).split(":").map(Number);
  return (h === 24 ? 0 : h) * 60 + m;
}

type Session = "pre" | "regular" | "post";

function sessionOf(ts: number): Session {
  const mins = etMinutes(ts);
  if (mins < 9 * 60 + 30) return "pre";
  if (mins < 16 * 60) return "regular";
  return "post";
}

const CHART_OPTIONS = {
  layout: {
    background: { type: ColorType.Solid, color: "#0d1422" },
    textColor: "#94a3b8",
    fontSize: 11,
  },
  grid: {
    vertLines: { color: "rgba(148,163,184,0.08)" },
    horzLines: { color: "rgba(148,163,184,0.08)" },
  },
  timeScale: {
    borderColor: "rgba(148,163,184,0.2)",
    timeVisible: true,
    secondsVisible: false,
    tickMarkFormatter: (time: number, tickType: number) =>
      tickType === TickMarkType.DayOfMonth ||
      tickType === TickMarkType.Month ||
      tickType === TickMarkType.Year
        ? ET_DATE.format(time * 1000)
        : ET_TIME.format(time * 1000),
  },
  localization: {
    timeFormatter: (time: number) => `${ET_FULL.format(time * 1000)}`,
  },
  rightPriceScale: { borderColor: "rgba(148,163,184,0.2)" },
  crosshair: { mode: 0 },
  autoSize: true,
  panes: {
    separatorColor: "rgba(148,163,184,0.25)",
    separatorHoverColor: "rgba(148,163,184,0.45)",
    enableResize: true,
  },
} as const;

const toUtc = (t: number) => t as UTCTimestamp;

function sessionMarkers(bars: OhlcvBar[]): SeriesMarker<UTCTimestamp>[] {
  const markers: SeriesMarker<UTCTimestamp>[] = [];
  let prev: Session | null = null;
  for (const b of bars) {
    const s = sessionOf(b.time);
    if (s !== prev) {
      if (s === "regular") {
        markers.push({ time: toUtc(b.time), position: "belowBar", color: "#3b82f6", shape: "circle", text: "RTH", size: 0.5 });
      } else if (s === "post" && prev === "regular") {
        markers.push({ time: toUtc(b.time), position: "belowBar", color: "#f59e0b", shape: "circle", text: "AH", size: 0.5 });
      }
      prev = s;
    }
  }
  return markers;
}

/** Candles + VWAP on pane 0, volume on a dedicated lower pane (~25%),
 * sharing the chart's single time axis. Incremental refreshes append
 * via series.update() so zoom/scroll are preserved; full reloads happen
 * only on symbol/interval changes. */
const MARK_STYLE: Record<TradeMark["kind"], { color: string; position: "aboveBar" | "belowBar"; shape: "arrowUp" | "arrowDown" }> = {
  entry: { color: "#22c55e", position: "belowBar", shape: "arrowUp" },
  exit_target: { color: "#22c55e", position: "aboveBar", shape: "arrowDown" },
  exit_stop: { color: "#ef4444", position: "aboveBar", shape: "arrowDown" },
  exit_session: { color: "#94a3b8", position: "aboveBar", shape: "arrowDown" },
  exit_other: { color: "#f59e0b", position: "aboveBar", shape: "arrowDown" },
  setup: { color: "#06b6d4", position: "belowBar", shape: "arrowUp" },
  setup_pending: { color: "#a78bfa", position: "belowBar", shape: "arrowUp" },
};

/** Bars per ET session for initial viewport sizing. */
function lastSessionBars(bars: OhlcvBar[]): number {
  if (!bars.length) return 0;
  const lastDay = etDay(bars[bars.length - 1].time);
  let n = 0;
  for (let i = bars.length - 1; i >= 0 && etDay(bars[i].time) === lastDay; i--) n++;
  return n;
}

const ET_DAY = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});
const etDay = (ts: number) => ET_DAY.format(ts * 1000);

export default function CandleChart({
  bars,
  vwap = [],
  rthVwap = [],
  showVwap = false,
  showRthVwap = false,
  tradeMarks = [],
  priceLines = [],
  focusRange = null,
}: {
  bars: OhlcvBar[];
  vwap?: VwapPoint[];
  rthVwap?: VwapPoint[];
  showVwap?: boolean;
  showRthVwap?: boolean;
  tradeMarks?: TradeMark[];
  priceLines?: PriceLine[];
  focusRange?: { from: number; to: number } | null;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const seriesRef = useRef<{
    candles: ISeriesApi<"Candlestick"> | null;
    volume: ISeriesApi<"Histogram"> | null;
    vwap: ISeriesApi<"Line"> | null;
    rth: ISeriesApi<"Line"> | null;
    lastBar: OhlcvBar | null;
    lastVwap: VwapPoint | null;
  }>({ candles: null, volume: null, vwap: null, rth: null, lastBar: null, lastVwap: null });
  const priceLinesRef = useRef<{ remove(): void }[]>([]);
  const firstBarTimeRef = useRef<number | null>(null);
  const chartRef = useRef<IChartApi | null>(null);

  // Build chart + series once.
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, CHART_OPTIONS);
    chartRef.current = chart;

    const candles = chart.addSeries(
      CandlestickSeries,
      { upColor: "#22c55e", downColor: "#ef4444", wickUpColor: "#22c55e", wickDownColor: "#ef4444", borderVisible: false },
      0,
    );
    const vwapLine = chart.addSeries(
      LineSeries,
      { color: "#f59e0b", lineWidth: 1, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: true, title: "VWAP" },
      0,
    );
    const rthLine = chart.addSeries(
      LineSeries,
      { color: "#06b6d4", lineWidth: 1, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: true, title: "RTH VWAP", lineStyle: 2 },
      0,
    );
    const volumePane = chart.addPane();
    volumePane.setStretchFactor(0.25);
    chart.panes()[0].setStretchFactor(0.75);
    const volume = chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" } }, 1);

    seriesRef.current = { candles, volume, vwap: vwapLine, rth: rthLine, lastBar: null, lastVwap: null };
    priceLinesRef.current = [];
    createSeriesMarkers(candles, []);

    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = { candles: null, volume: null, vwap: null, rth: null, lastBar: null, lastVwap: null };
    };
  }, []);

  const volumePoint = (b: OhlcvBar) => ({
    time: toUtc(b.time),
    value: b.volume,
    color:
      b.close >= b.open
        ? sessionOf(b.time) === "regular"
          ? "rgba(34,197,94,0.5)"
          : "rgba(34,197,94,0.22)"
        : sessionOf(b.time) === "regular"
          ? "rgba(239,68,68,0.5)"
          : "rgba(239,68,68,0.22)",
  });

  // Data updates — incremental append when possible, full reset on
  // symbol/interval change so stale candles never linger.
  useEffect(() => {
    const { candles, volume, vwap: vwapSeries, rth: rthSeries, lastBar, lastVwap } = seriesRef.current;
    if (!candles || !volume || !vwapSeries || !rthSeries) return;

    const isAppend =
      lastBar !== null &&
      firstBarTimeRef.current !== null &&
      bars.length > 0 &&
      bars[0].time === firstBarTimeRef.current &&
      bars[bars.length - 1].time >= lastBar.time;

    if (isAppend) {
      for (const b of bars) {
        if (b.time >= lastBar!.time) {
          candles.update({ time: toUtc(b.time), open: b.open, high: b.high, low: b.low, close: b.close });
          volume.update(volumePoint(b));
        }
      }
      const nv = vwap[vwap.length - 1];
      if (nv && (!lastVwap || nv.time >= lastVwap.time)) {
        vwapSeries.update({ time: toUtc(nv.time), value: nv.value });
        seriesRef.current.lastVwap = nv;
      }
      const nr = rthVwap[rthVwap.length - 1];
      if (nr) rthSeries.update({ time: toUtc(nr.time), value: nr.value });
      seriesRef.current.lastBar = bars[bars.length - 1];
      return;
    }

    // Full reset path.
    candles.setData(
      bars.map((b) => ({ time: toUtc(b.time), open: b.open, high: b.high, low: b.low, close: b.close })),
    );
    volume.setData(bars.map(volumePoint));
    vwapSeries.setData(vwap.map((p) => ({ time: toUtc(p.time), value: p.value })));
    rthSeries.setData(rthVwap.map((p) => ({ time: toUtc(p.time), value: p.value })));
    seriesRef.current.lastBar = bars[bars.length - 1] ?? null;
    seriesRef.current.lastVwap = vwap[vwap.length - 1] ?? null;
    firstBarTimeRef.current = bars[0]?.time ?? null;
    if (bars.length > 0) {
      // Default viewport: the most recent trading session; earlier
      // history stays reachable by scrolling left.
      const lastCount = Math.max(1, lastSessionBars(bars));
      chartRef.current?.timeScale().setVisibleLogicalRange({
        from: bars.length - lastCount,
        to: bars.length + 2,
      });
    }
  }, [bars, vwap, rthVwap]);

  // VWAP visibility toggles.
  useEffect(() => {
    seriesRef.current.vwap?.applyOptions({ visible: showVwap });
  }, [showVwap]);
  useEffect(() => {
    seriesRef.current.rth?.applyOptions({ visible: showRthVwap });
  }, [showRthVwap]);

  // Session + trade markers.
  useEffect(() => {
    const { candles } = seriesRef.current;
    if (!candles) return;
    const barTimes = new Set(bars.map((b) => b.time));
    const trade = tradeMarks
      .filter((m) => barTimes.has(m.time))
      .map((m) => ({
        time: toUtc(m.time),
        position: MARK_STYLE[m.kind].position,
        color: MARK_STYLE[m.kind].color,
        shape: MARK_STYLE[m.kind].shape,
        text: m.label,
        size: 1,
      }));
    createSeriesMarkers(candles, [...sessionMarkers(bars), ...trade]);
  }, [bars, tradeMarks]);

  // Horizontal price lines for the selected trade.
  useEffect(() => {
    const { candles } = seriesRef.current;
    if (!candles) return;
    for (const l of priceLinesRef.current) candles.removePriceLine(l as never);
    priceLinesRef.current = priceLines.map((l) =>
      candles.createPriceLine({
        price: l.price,
        color: l.color,
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: l.title,
      }) as unknown as { remove(): void },
    );
  }, [priceLines]);

  // Focus a trade's entry→exit span only when the user selects it —
  // never on periodic refresh, and manual zoom/scroll remains free after.
  useEffect(() => {
    if (!focusRange || !chartRef.current) return;
    const pad = 6;
    chartRef.current.timeScale().setVisibleLogicalRange({
      from: focusRange.from - pad,
      to: focusRange.to + pad,
    });
  }, [focusRange]);

  return <div ref={containerRef} className="h-[480px] w-full" />;
}

"use client";

import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  TickMarkType,
  createChart,
  createSeriesMarkers,
  type IChartApi,
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

// ---- America/New_York handling -------------------------------------------
// All tick labels and tooltip times are formatted through Intl with an
// explicit ET timezone, so EST<->EDT transitions are handled automatically
// and labels are correct regardless of the browser's timezone.

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

/** Candles on pane 0 (~75%) + volume on a dedicated lower pane (~25%),
 * sharing the chart's single time axis — zoom/scroll stay synchronized.
 * Future layers (VWAP, MAs, entry/stop lines, alert markers) attach as
 * additional series on pane 0. */
export default function CandleChart({ bars }: { bars: OhlcvBar[] }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, CHART_OPTIONS);
    chartRef.current = chart;

    const candles = chart.addSeries(
      CandlestickSeries,
      {
        upColor: "#22c55e",
        downColor: "#ef4444",
        wickUpColor: "#22c55e",
        wickDownColor: "#ef4444",
        borderVisible: false,
      },
      0,
    );

    const volumePane = chart.addPane();
    volumePane.setStretchFactor(0.25);
    chart.panes()[0].setStretchFactor(0.75);
    const volume = chart.addSeries(
      HistogramSeries,
      { priceFormat: { type: "volume" } },
      1,
    );

    const toUtc = (t: number) => t as UTCTimestamp;
    candles.setData(
      bars.map((b) => ({
        time: toUtc(b.time),
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      })),
    );

    // Session-aware volume: green/red inside regular hours, muted in
    // extended hours — direction is preserved, session is visible.
    volume.setData(
      bars.map((b) => {
        const regular = sessionOf(b.time) === "regular";
        const up = b.close >= b.open;
        return {
          time: toUtc(b.time),
          value: b.volume,
          color: up
            ? regular
              ? "rgba(34,197,94,0.5)"
              : "rgba(34,197,94,0.22)"
            : regular
              ? "rgba(239,68,68,0.5)"
              : "rgba(239,68,68,0.22)",
        };
      }),
    );

    // Session boundary markers — placed where the data itself transitions,
    // so nothing is marked for sessions the feed did not report.
    const markers: SeriesMarker<UTCTimestamp>[] = [];
    let prev: Session | null = null;
    for (const b of bars) {
      const s = sessionOf(b.time);
      if (s !== prev) {
        if (s === "regular") {
          markers.push({
            time: toUtc(b.time),
            position: "belowBar",
            color: "#3b82f6",
            shape: "circle",
            text: "RTH",
            size: 0.5,
          });
        } else if (s === "post" && prev === "regular") {
          markers.push({
            time: toUtc(b.time),
            position: "belowBar",
            color: "#f59e0b",
            shape: "circle",
            text: "AH",
            size: 0.5,
          });
        }
        prev = s;
      }
    }
    createSeriesMarkers(candles, markers);

    chart.timeScale().fitContent();

    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, [bars]);

  return <div ref={containerRef} className="h-[480px] w-full" />;
}

// Pure replay trading engine — local simulation only, no broker calls.
// Same fill/slippage conventions as the backend simulator:
//   entry at next-bar open +5bps, exit fill -5bps (stop/target/manual),
//   same-bar stop+target ambiguity resolves STOP-FIRST (conservative),
//   gap-through fills at bar open (worse than the level).

export type Bar = { t: string; o: number; h: number; l: number; c: number; v: number };

export type PendingOrder = {
  side: "buy" | "sell";
  qty: number;
  stop?: number;
  target?: number;
};

export type Position = {
  qty: number;
  entry: number;
  entryTs: string;
  stop?: number;
  target?: number;
};

export type Fill = {
  ts: string;
  side: "buy" | "sell";
  qty: number;
  price: number;
  reason: "manual" | "stop" | "target" | "session_end" | "data_end" | "canceled";
  pnl?: number;
};

export type Account = {
  cash: number;
  position: Position | null;
  pending: PendingOrder | null;
  fills: Fill[];
  realized: number;
  rejected: string | null;
  traded: boolean;   // any order submitted — locks rewind permanently
};

export const INITIAL_CASH = 1000;
export const SLIPPAGE = 0.0005;

export function newAccount(): Account {
  return { cash: INITIAL_CASH, position: null, pending: null,
           fills: [], realized: 0, rejected: null, traded: false };
}

export function placeOrder(acc: Account, order: PendingOrder): Account {
  if (acc.pending) return { ...acc, rejected: "order already pending" };
  return { ...acc, pending: order, rejected: null, traded: true };
}

export function equity(acc: Account, last: number): number {
  return acc.cash + (acc.position ? acc.position.qty * last : 0);
}

export function unrealized(acc: Account, last: number): number {
  return acc.position ? (last - acc.position.entry) * acc.position.qty : 0;
}

// Advance one bar: fill pending orders at this bar's open, then check
// stop/target against this bar's range. Only called with revealed bars.
export function stepBar(acc: Account, bar: Bar): Account {
  let { cash, position, pending, fills, realized } = acc;
  let rejected = acc.rejected;

  if (pending) {
    if (pending.side === "buy" && !position) {
      const px = round4(bar.o * (1 + SLIPPAGE));
      const cost = px * pending.qty;
      if (cost <= cash) {
        cash -= cost;
        position = { qty: pending.qty, entry: px, entryTs: bar.t, stop: pending.stop, target: pending.target };
        fills = [...fills, { ts: bar.t, side: "buy", qty: pending.qty, price: px, reason: "manual" }];
      } else {
        rejected = "insufficient cash";
      }
    } else if (pending.side === "sell" && position) {
      const px = round4(bar.o * (1 - SLIPPAGE));
      const pnl = (px - position.entry) * position.qty;
      cash += px * position.qty;
      realized += pnl;   // exact — display layer rounds
      fills = [...fills, { ts: bar.t, side: "sell", qty: position.qty, price: px, reason: "manual", pnl }];
      position = null;
    }
    pending = null;
  }

  // Stop/target on the newly revealed bar — stop first if both touched.
  if (position) {
    const stopHit = position.stop != null && bar.l <= position.stop;
    const tgtHit = position.target != null && bar.h >= position.target;
    if (stopHit || tgtHit) {
      let px: number, reason: "stop" | "target";
      if (stopHit) {
        reason = "stop";
        // gap-through: fill at open if it gaps below the stop
        px = bar.o < position.stop! ? bar.o : position.stop!;
      } else {
        reason = "target";
        px = bar.o > position.target! ? bar.o : position.target!;
      }
      px = round4(px * (1 - SLIPPAGE));
      const pnl = (px - position.entry) * position.qty;
      cash += px * position.qty;
      realized += pnl;
      fills = [...fills, { ts: bar.t, side: "sell", qty: position.qty, price: px, reason, pnl }];
      position = null;
    }
  }
  return { cash, position, pending, fills, realized, rejected, traded: acc.traded };
}

// End of revealed data: cancel pending (no next bar exists) and liquidate
// any open position at the last bar's CLOSE with exit slippage.
// sessionEnd=true only when the last bar is a real session close bar —
// truncated snapshots report data_end instead.
export function finalize(acc: Account, bar: Bar, sessionEnd: boolean): Account {
  let { cash, position, pending, fills, realized } = acc;
  if (pending) {
    fills = [...fills, { ts: bar.t, side: pending.side, qty: pending.qty,
                         price: 0, reason: "canceled" }];
    pending = null;
  }
  if (position) {
    const px = round4(bar.c * (1 - SLIPPAGE));
    const pnl = (px - position.entry) * position.qty;
    cash += px * position.qty;
    realized += pnl;
    fills = [...fills, { ts: bar.t, side: "sell", qty: position.qty, price: px,
                         reason: sessionEnd ? "session_end" : "data_end", pnl }];
    position = null;
  }
  return { cash, position, pending, fills, realized, rejected: acc.rejected,
           traded: acc.traded };
}

function round4(x: number): number { return Math.round(x * 10000) / 10000; }

// Pair fills into complete trades for the journal.
export type Trade = {
  entryTs: string; exitTs: string; qty: number;
  entry: number; exit: number; pnl: number; reason: string;
};
export function journal(acc: Account): Trade[] {
  const out: Trade[] = [];
  let open: Fill | null = null;
  for (const f of acc.fills) {
    if (f.side === "buy") open = f;
    else if (open) {
      out.push({ entryTs: open.ts, exitTs: f.ts, qty: f.qty,
                 entry: open.price, exit: f.price, pnl: f.pnl ?? 0,
                 reason: f.reason });
      open = null;
    }
  }
  return out;
}

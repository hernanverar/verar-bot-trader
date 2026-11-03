import { test } from "node:test";
import assert from "node:assert/strict";
import { newAccount, placeOrder, stepBar, finalize, journal, equity, unrealized } from "./replayEngine.ts";

const b = (t: string, o: number, h: number, l: number, c: number, v = 100) =>
  ({ t, o, h, l, c, v });

test("buy fills at next bar open + slippage", () => {
  let a = newAccount();
  a = placeOrder(a, { side: "buy", qty: 10 });
  a = stepBar(a, b("T2", 2.0, 2.1, 1.9, 2.05));
  assert.equal(a.position!.entry, 2.001);
  assert.equal(a.cash, 1000 - 20.01);
});

test("sell fills at next bar open - slippage", () => {
  let a = newAccount();
  a = stepBar(placeOrder(a, { side: "buy", qty: 10 }), b("T1", 2.0, 2.0, 2.0, 2.0));
  a = placeOrder(a, { side: "sell", qty: 10 });
  a = stepBar(a, b("T2", 2.10, 2.2, 2.0, 2.1));
  assert.equal(a.position, null);
  assert.equal(a.fills[1].price, 2.099);
});

test("insufficient cash rejects at fill", () => {
  let a = placeOrder(newAccount(), { side: "buy", qty: 100000 });
  a = stepBar(a, b("T1", 2.0, 2.0, 2.0, 2.0));
  assert.equal(a.position, null);
  assert.equal(a.rejected, "insufficient cash");
});

test("no duplicate pending orders", () => {
  let a = placeOrder(newAccount(), { side: "buy", qty: 1 });
  a = placeOrder(a, { side: "buy", qty: 5 });
  assert.equal(a.rejected, "order already pending");
  assert.equal(a.pending!.qty, 1);
});

test("stop executes and gap-through fills at open", () => {
  let a = stepBar(placeOrder(newAccount(), { side: "buy", qty: 10, stop: 1.95 }),
                  b("T1", 2.0, 2.0, 2.0, 2.0));
  a = stepBar(a, b("T2", 1.90, 1.92, 1.85, 1.88)); // open 1.90 < stop 1.95
  assert.equal(a.position, null);
  assert.equal(a.fills[1].reason, "stop");
  assert.equal(a.fills[1].price, 1.8991); // gap: open - slippage
});

test("same-bar stop+target resolves stop-first", () => {
  let a = stepBar(placeOrder(newAccount(), { side: "buy", qty: 10, stop: 1.9, target: 2.2 }),
                  b("T1", 2.0, 2.0, 2.0, 2.0));
  a = stepBar(a, b("T2", 2.0, 2.3, 1.85, 2.1)); // touches both
  assert.equal(a.fills[1].reason, "stop");
});

test("target fills at level", () => {
  let a = stepBar(placeOrder(newAccount(), { side: "buy", qty: 10, target: 2.1 }),
                  b("T1", 2.0, 2.0, 2.0, 2.0));
  a = stepBar(a, b("T2", 2.05, 2.15, 2.0, 2.12));
  assert.equal(a.fills[1].reason, "target");
  assert.equal(a.fills[1].price, 2.099); // 2.1 - slippage
  assert.ok(a.fills[1].pnl! > 0);
});

test("no fill without next bar; pending survives", () => {
  let a = placeOrder(newAccount(), { side: "buy", qty: 10 });
  assert.equal(a.position, null);
  assert.ok(a.pending);
});

test("unrealized and equity track last price", () => {
  let a = stepBar(placeOrder(newAccount(), { side: "buy", qty: 10 }), b("T1", 2.0, 2.0, 2.0, 2.0));
  assert.equal(unrealized(a, 2.10), (2.10 - 2.001) * 10);
  assert.equal(equity(a, 2.10), a.cash + 10 * 2.10);
});

test("same-bar entry then stop executes", () => {
  let a = placeOrder(newAccount(), { side: "buy", qty: 10, stop: 1.95 });
  a = stepBar(a, b("T1", 2.0, 2.05, 1.90, 1.98)); // fills at open, low hits stop
  assert.equal(a.position, null);
  assert.equal(a.fills[0].reason, "manual");
  assert.equal(a.fills[1].reason, "stop");
  assert.equal(a.fills[1].price, 1.949); // stop level - slippage
});

test("traded flag persists after position closed (rewind lock)", () => {
  let a = stepBar(placeOrder(newAccount(), { side: "buy", qty: 10 }), b("T1", 2, 2, 2, 2));
  a = stepBar(placeOrder(a, { side: "sell", qty: 10 }), b("T2", 2, 2, 2, 2));
  assert.equal(a.position, null);
  assert.equal(a.traded, true);
});

test("finalize cancels pending and liquidates at close", () => {
  let a = placeOrder(newAccount(), { side: "buy", qty: 5 });
  a = finalize(a, b("T9", 2, 2, 2, 2.5), true);
  assert.equal(a.pending, null);
  assert.equal(a.fills[0].reason, "canceled");
  a = stepBar(placeOrder(newAccount(), { side: "buy", qty: 10 }), b("T1", 2, 2, 2, 2));
  a = finalize(a, b("T9", 2, 2, 2, 2.2), true);
  assert.equal(a.fills[1].reason, "session_end");
  a = stepBar(placeOrder(newAccount(), { side: "buy", qty: 10 }), b("T1", 2, 2, 2, 2));
  a = finalize(a, b("T3", 2, 2, 2, 2.2), false);
  assert.equal(a.fills[1].reason, "data_end");
});

test("flat account reconciles: cash = initial + realized exactly", () => {
  let a = stepBar(placeOrder(newAccount(), { side: "buy", qty: 7 }), b("T1", 1.937, 2, 1.9, 1.95));
  a = stepBar(placeOrder(a, { side: "sell", qty: 7 }), b("T2", 2.113, 2.2, 2.1, 2.15));
  assert.equal(a.position, null);
  assert.ok(Math.abs(a.cash - (1000 + a.realized)) < 1e-9);
});

test("journal pairs buys with exits", () => {
  const t0 = journal;
  let a = stepBar(placeOrder(newAccount(), { side: "buy", qty: 10, target: 2.2 }), b("T1", 2, 2, 2, 2));
  a = stepBar(a, b("T2", 2.1, 2.3, 2.0, 2.25));
  const t = t0(a);
  assert.equal(t.length, 1);
  assert.equal(t[0].reason, "target");
  assert.ok(t[0].pnl > 0);
});

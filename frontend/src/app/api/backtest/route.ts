import { NextResponse } from "next/server";

export async function POST(request: Request) {
  try {
    const body = await request.json();

    const response = await fetch("http://127.0.0.1:8000/api/backtest", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(120_000),
    });

    const data = await response.json();

    return NextResponse.json(data, {
      status: response.status,
    });
  } catch (error) {
    console.error("Backtest API error:", error);

    return NextResponse.json(
      { error: "Unable to connect to the backtesting service" },
      { status: 502 },
    );
  }
}

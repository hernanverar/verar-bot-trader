import { NextResponse } from "next/server";

export async function POST() {
  try {
    const response = await fetch("http://127.0.0.1:8000/api/alerts/evaluate", {
      method: "POST",
      cache: "no-store",
      signal: AbortSignal.timeout(60_000),
    });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error("Alerts evaluate error:", error);
    return NextResponse.json({ error: "Unable to reach the alerts service" }, { status: 502 });
  }
}

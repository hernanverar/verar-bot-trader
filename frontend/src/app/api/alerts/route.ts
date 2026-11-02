import { NextResponse } from "next/server";

async function proxy(method: string, body?: string) {
  try {
    const response = await fetch("http://127.0.0.1:8000/api/alerts", {
      method,
      cache: "no-store",
      signal: AbortSignal.timeout(60_000),
      headers: body ? { "Content-Type": "application/json" } : {},
      body,
    });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error("Alerts API error:", error);
    return NextResponse.json({ error: "Unable to reach the alerts service" }, { status: 502 });
  }
}

export async function GET() {
  return proxy("GET");
}

export async function POST(request: Request) {
  return proxy("POST", await request.text());
}

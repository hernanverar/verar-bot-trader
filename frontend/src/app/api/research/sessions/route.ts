import { NextResponse } from "next/server";

export async function GET() {
  try {
    const r = await fetch("http://127.0.0.1:8000/api/research/sessions",
      { cache: "no-store", signal: AbortSignal.timeout(15_000) });
    return NextResponse.json(await r.json(), { status: r.status });
  } catch {
    return NextResponse.json({ error: "Backend unavailable" }, { status: 502 });
  }
}

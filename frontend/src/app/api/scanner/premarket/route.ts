import { NextResponse } from "next/server";

export async function GET(request: Request) {
  try {
    const { search } = new URL(request.url);
    const r = await fetch(`http://127.0.0.1:8000/api/scanner/premarket${search}`,
      { cache: "no-store", signal: AbortSignal.timeout(45_000) });
    return NextResponse.json(await r.json(), { status: r.status });
  } catch {
    return NextResponse.json({ error: "Backend unavailable" }, { status: 502 });
  }
}

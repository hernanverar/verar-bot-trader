import { NextResponse } from "next/server";

export async function POST(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await params;
    const response = await fetch(`http://127.0.0.1:8000/api/alerts/${id}/rearm`, {
      method: "POST",
      cache: "no-store",
      signal: AbortSignal.timeout(60_000),
    });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error("Alerts API error:", error);
    return NextResponse.json({ error: "Unable to reach the alerts service" }, { status: 502 });
  }
}

import { NextResponse } from "next/server";

export async function GET(request: Request) {
  try {
    const { search } = new URL(request.url);

    const response = await fetch(
      `http://127.0.0.1:8000/api/scanner/hot-stocks${search}`,
      {
        method: "GET",
        cache: "no-store",
        signal: AbortSignal.timeout(120_000),
      },
    );

    const data = await response.json();

    return NextResponse.json(data, {
      status: response.status,
    });
  } catch (error) {
    console.error("Hot stocks scanner API error:", error);

    return NextResponse.json(
      { error: "Unable to connect to the scanner service" },
      { status: 502 },
    );
  }
}

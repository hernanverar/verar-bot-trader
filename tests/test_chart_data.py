from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.chart_data import session_vwap

ET = ZoneInfo("America/New_York")
DAY = datetime(2026, 1, 15, 9, 30, tzinfo=ET)


def bar(i, h, l, c, v, day=DAY):
    return SimpleNamespace(
        timestamp=day + timedelta(minutes=i), open=c, high=h, low=l, close=c, volume=v,
    )


def test_vwap_typical_price_weighted():
    bars = [bar(0, 10.2, 9.9, 10.0, 100), bar(1, 10.4, 10.0, 10.3, 300)]
    out = session_vwap(bars)
    tp1, tp2 = 10.0333333, 10.2333333
    assert out[0][1] == pytest.approx(tp1)
    assert out[1][1] == pytest.approx((tp1 * 100 + tp2 * 300) / 400)


def test_vwap_resets_each_session():
    next_day = DAY + timedelta(days=1)
    bars = [
        bar(0, 10.2, 9.9, 10.0, 100),
        bar(0, 20.4, 19.9, 20.0, 100, day=next_day),
    ]
    out = session_vwap(bars)
    assert out[1][1] == pytest.approx(20.1)  # fresh accumulation, no mixing


def test_vwap_skips_zero_volume_keeps_last():
    bars = [
        bar(0, 10.2, 9.9, 10.0, 100),
        bar(1, 50.0, 49.0, 49.5, 0),  # zero volume — no contribution
    ]
    out = session_vwap(bars)
    assert len(out) == 2
    assert out[1][1] == pytest.approx(10.0333, abs=0.001)  # prior VWAP carried, no fabricated value


def test_vwap_empty_input():
    assert session_vwap([]) == []

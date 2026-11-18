"""default_end_date / _cache_is_fresh / _trim_to_end の仕様テスト。"""
import re
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import gap_up_scanner as scanner


# ---------- default_end_date ----------
def test_default_end_date_unset_is_first_of_month_jst(monkeypatch):
    monkeypatch.delenv("END_DATE", raising=False)
    before = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-01")
    got = scanner.default_end_date()
    after = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-01")
    assert re.fullmatch(r"\d{4}-\d{2}-01", got)
    assert got in (before, after)


def test_default_end_date_blank_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("END_DATE", "   ")
    assert scanner.default_end_date().endswith("-01")


def test_default_end_date_jst_month_boundary(monkeypatch):
    """UTC では前月末でも JST で翌月1日になっていれば JST の月を使う。"""
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            # 2026-05-31 15:30 UTC = 2026-06-01 00:30 JST
            return datetime(2026, 5, 31, 15, 30, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.delenv("END_DATE", raising=False)
    monkeypatch.setattr(scanner, "datetime", FakeDT)
    assert scanner.default_end_date() == "2026-06-01"


def test_default_end_date_override(monkeypatch):
    monkeypatch.setenv("END_DATE", "2026-06-01")
    assert scanner.default_end_date() == "2026-06-01"


@pytest.mark.parametrize("bad", ["2026/06/01", "2026-13-01", "20260601", "abc"])
def test_default_end_date_invalid_format(monkeypatch, bad):
    monkeypatch.setenv("END_DATE", bad)
    with pytest.raises(ValueError) as exc:
        scanner.default_end_date()
    msg = str(exc.value)
    assert "END_DATE" in msg and "YYYY-MM-DD" in msg and bad in msg


# ---------- _cache_is_fresh ----------
def _df(last, tz=None, n=5):
    idx = pd.date_range(end=last, periods=n, freq="D", tz=tz)
    return pd.DataFrame({"Open": 1.0, "Close": 1.0}, index=idx)


@pytest.mark.parametrize("tz", [None, "Asia/Tokyo"])
@pytest.mark.parametrize("last,expected", [
    ("2026-05-25", True),   # ちょうど7日前（境界）
    ("2026-05-29", True),
    ("2026-05-24", False),  # 8日前
    ("2026-06-03", True),   # 終了日より後
])
def test_cache_is_fresh_boundaries(last, expected, tz):
    assert scanner._cache_is_fresh(_df(last, tz), "2026-06-01") is expected


def test_cache_is_fresh_empty_and_none():
    assert scanner._cache_is_fresh(None, "2026-06-01") is False
    empty = pd.DataFrame(columns=["Open", "Close"], index=pd.DatetimeIndex([]))
    assert scanner._cache_is_fresh(empty, "2026-06-01") is False


# ---------- _trim_to_end ----------
@pytest.mark.parametrize("tz", [None, "Asia/Tokyo"])
def test_trim_to_end_excludes_end_date_and_after(tz):
    df = _df("2026-06-03", tz, n=6)  # 05-29 .. 06-03
    out = scanner._trim_to_end(df, "2026-06-01")
    days = [d.strftime("%Y-%m-%d") for d in out.index]
    assert days == ["2026-05-29", "2026-05-30", "2026-05-31"]
    assert getattr(out.index, "tz", None) == getattr(df.index, "tz", None)  # 元のtzは保持


def test_trim_to_end_nothing_to_trim():
    df = _df("2026-05-20", None)
    out = scanner._trim_to_end(df, "2026-06-01")
    assert len(out) == len(df)


# --- 月の途中に保存したキャッシュは月末が欠けるため、終了日以降に保存したものだけを最新とみなす ---
def test_cache_saved_before_end_date_is_stale():
    import datetime as dt
    import pandas as pd
    import gap_up_scanner as g
    df = pd.DataFrame({"Close": [1.0]}, index=pd.to_datetime(["2026-05-26"]))
    jst = dt.timezone(dt.timedelta(hours=9))
    saved_mid_month = dt.datetime(2026, 5, 27, 18, 0, tzinfo=jst).timestamp()
    saved_after_month = dt.datetime(2026, 6, 1, 9, 0, tzinfo=jst).timestamp()
    assert g._cache_is_fresh(df, "2026-06-01", saved_mid_month) is False
    assert g._cache_is_fresh(df, "2026-06-01", saved_after_month) is True
    # 保存時刻を渡さない場合は従来どおり最終足だけで判定
    assert g._cache_is_fresh(df, "2026-06-01") is True

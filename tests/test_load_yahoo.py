"""_load_yahoo_prototype_data：yfinance.download を差し替えて END_DATE 以降の足が混入しないこと。"""
import os
import sys
import types

import numpy as np
import pandas as pd
import pytest

import gap_up_scanner as scanner


def _ohlc(start="2015-01-01", end="2016-02-17", tz=None):
    idx = pd.bdate_range(start, end, tz=tz)
    rng = np.random.default_rng(0)
    close = 1000 + np.cumsum(rng.normal(0, 5, len(idx)))
    opens = close * (1 + rng.normal(0, 0.01, len(idx)))
    return pd.DataFrame({"Open": opens, "High": np.maximum(opens, close) + 1,
                         "Low": np.minimum(opens, close) - 1, "Close": close,
                         "Volume": 1000.0}, index=idx)


@pytest.fixture
def fake_yf(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("END_DATE", "2016-02-01")
    calls = []
    state = {"df": _ohlc()}

    def download(ticker, start=None, end=None, **kw):
        calls.append({"ticker": ticker, "start": start, "end": end})
        return state["df"].copy()  # 意図的に end より後の足も返す（取引途中の足の混入を想定）

    mod = types.ModuleType("yfinance")
    mod.download = download
    monkeypatch.setitem(sys.modules, "yfinance", mod)
    return calls, state


def test_download_trimmed_to_end_date(fake_yf, tmp_path):
    calls, _ = fake_yf
    res = scanner._load_yahoo_prototype_data("9999")
    assert calls and calls[0]["end"] == "2016-02-01" and calls[0]["ticker"] == "9999.T"
    assert res["metrics"]["end"] < "2016-02-01"
    assert res["metrics"]["end"] == "2016-01-29"  # 2016-01-29(金) が最終営業日
    assert res["source"] == "Yahoo Finance download"
    assert all(row["date"] < "2016-02-01" for row in res["daily_pnl"])
    assert os.path.exists(tmp_path / "price_cache" / "9999.T.pkl")


def test_fresh_cache_is_reused_and_trimmed(fake_yf, tmp_path):
    calls, _ = fake_yf
    scanner._load_yahoo_prototype_data("9999")
    res = scanner._load_yahoo_prototype_data("9999")
    assert len(calls) == 1  # 2回目はキャッシュ
    assert res["source"] == "Yahoo Finance cache"
    assert res["metrics"]["end"] < "2016-02-01"


def test_stale_cache_triggers_download(fake_yf, tmp_path):
    calls, _ = fake_yf
    os.makedirs(tmp_path / "price_cache")
    _ohlc(end="2015-12-30").to_pickle(tmp_path / "price_cache" / "9999.T.pkl")
    res = scanner._load_yahoo_prototype_data("9999")
    assert len(calls) == 1
    assert res["source"] == "Yahoo Finance download"


def test_tz_aware_download_trimmed(fake_yf):
    calls, state = fake_yf
    state["df"] = _ohlc(tz="Asia/Tokyo")
    res = scanner._load_yahoo_prototype_data("9999")
    assert res["metrics"]["end"] < "2016-02-01"


def test_too_few_rows_after_trim_raises(fake_yf, monkeypatch):
    """ダウンロード件数は min_data_days 以上でも、トリム後に不足なら ValueError。"""
    calls, state = fake_yf
    monkeypatch.setenv("END_DATE", "2015-03-02")  # 2015-01-01〜02-27 は約42営業日
    with pytest.raises(ValueError):
        scanner._load_yahoo_prototype_data("9999")


def test_short_download_raises(fake_yf):
    _, state = fake_yf
    state["df"] = _ohlc(end="2015-02-27")
    with pytest.raises(ValueError):
        scanner._load_yahoo_prototype_data("9999")

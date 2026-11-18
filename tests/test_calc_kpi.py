"""calc_kpi：年数を取引期間から計算する変更の検証（合成trades）。"""
import pandas as pd
import pytest

import gap_up_scanner as scanner


def _result(trades):
    margin = scanner.CONFIG["margin"]
    cum, curve = 0, []
    for t in trades:
        cum += t["pnl"]
        t["cumulative"] = cum
        curve.append(margin + cum)
    return {"ticker": "TEST.T", "trades": trades, "equity_curve": curve,
            "monthly": {}, "gap_up_count": 1, "raw_net": cum}


def _trades():
    # 2020-01-06 エントリー 〜 2022-01-06 エグジット（731日 = 2.0014年）
    return [
        {"entry_date": "2020-01-06", "exit_date": "2020-01-07", "pnl": 100_000},
        {"entry_date": "2021-01-06", "exit_date": "2021-01-07", "pnl": -50_000},
        {"entry_date": "2022-01-05", "exit_date": "2022-01-06", "pnl": 160_000},
    ]


def test_cagr_uses_trade_period(monkeypatch):
    monkeypatch.setitem(scanner.CONFIG, "margin", 1_000_000)
    kpi = scanner.calc_kpi(_result(_trades()))
    years = 731 / 365.25
    expected = round(((1_210_000 / 1_000_000) ** (1 / years) - 1) * 100, 2)
    assert kpi["cagr"] == pytest.approx(expected)
    assert kpi["net_profit"] == 210_000
    assert kpi["pf"] == round(260_000 / 50_000, 3)
    assert kpi["win_rate"] == 66.7
    assert kpi["total_trades"] == 3


def test_cagr_independent_of_config_end_date(monkeypatch):
    monkeypatch.setitem(scanner.CONFIG, "margin", 1_000_000)
    monkeypatch.setitem(scanner.CONFIG, "end_date", "2022-02-01")
    a = scanner.calc_kpi(_result(_trades()))
    monkeypatch.setitem(scanner.CONFIG, "end_date", "2035-12-01")
    b = scanner.calc_kpi(_result(_trades()))
    monkeypatch.setitem(scanner.CONFIG, "start_date", "2000-01-01")
    c = scanner.calc_kpi(_result(_trades()))
    assert a["cagr"] == b["cagr"] == c["cagr"]


def test_calc_kpi_on_run_backtest_output(monkeypatch):
    """run_backtest の出力形式そのままで calc_kpi が動き、CONFIG end_date に依存しない。"""
    monkeypatch.setitem(scanner.CONFIG, "min_trades", 1)
    monkeypatch.setitem(scanner.CONFIG, "min_net_profit", 0)
    idx = pd.bdate_range("2021-01-04", periods=10)
    close = [100.0] * 10
    opens = [100.0, 103.0, 99.0, 104.0, 100.0, 101.0, 100.0, 102.0, 100.0, 100.0]
    df = pd.DataFrame({"Open": opens, "High": 110.0, "Low": 90.0, "Close": close}, index=idx)
    r = scanner.run_backtest(df, "TEST.T")
    assert r is not None and len(r["trades"]) == 9
    monkeypatch.setitem(scanner.CONFIG, "end_date", "2021-02-01")
    a = scanner.calc_kpi(r)
    monkeypatch.setitem(scanner.CONFIG, "end_date", "2030-01-01")
    b = scanner.calc_kpi(r)
    assert a["cagr"] == b["cagr"]
    # 1日目エントリー 2021-01-04 〜 最終エグジット 2021-01-15 = 11日
    shares = int(scanner.CONFIG["position_size"] / 100.0)
    net = sum((o - 100.0) * shares for o in opens[1:])
    years = 11 / 365.25
    m = scanner.CONFIG["margin"]
    assert a["cagr"] == round((((m + net) / m) ** (1 / years) - 1) * 100, 2)


def test_calc_kpi_fallback_to_config_when_dates_missing(monkeypatch):
    monkeypatch.setitem(scanner.CONFIG, "margin", 1_000_000)
    monkeypatch.setitem(scanner.CONFIG, "start_date", "2020-01-01")
    monkeypatch.setitem(scanner.CONFIG, "end_date", "2022-01-01")
    trades = [{"pnl": 100_000}, {"pnl": -10_000}]
    kpi = scanner.calc_kpi(_result(trades))
    years = 731 / 365.25
    assert kpi["cagr"] == round(((1_090_000 / 1_000_000) ** (1 / years) - 1) * 100, 2)


def test_calc_kpi_single_day_period_does_not_divide_by_zero(monkeypatch):
    monkeypatch.setitem(scanner.CONFIG, "margin", 1_000_000)
    trades = [{"entry_date": "2021-01-04", "exit_date": "2021-01-04", "pnl": 1_000}]
    kpi = scanner.calc_kpi(_result(trades))
    assert kpi["cagr"] > 0  # years = 1/365.25 の下限が効く


def test_calc_kpi_all_losses_large(monkeypatch):
    monkeypatch.setitem(scanner.CONFIG, "margin", 1_000_000)
    trades = [
        {"entry_date": "2021-01-04", "exit_date": "2021-01-05", "pnl": -600_000},
        {"entry_date": "2021-01-05", "exit_date": "2021-01-06", "pnl": -500_000},
    ]
    kpi = scanner.calc_kpi(_result(trades))
    assert kpi["cagr"] == -100.0
    assert kpi["win_rate"] == 0.0
    assert kpi["pf"] == 0.0

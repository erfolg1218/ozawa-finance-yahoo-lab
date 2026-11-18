"""build_static_site.build / prepare_out_dir / fetch_previous のテスト（ネットワーク不要）。"""
import json
import os
import shutil

import pytest

import build_static_site as bss
import gap_up_scanner as scanner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "watchlist.txt").write_text("1111  # OK\n2222\n# comment\n", encoding="utf-8")
    (tmp_path / "stock_list.csv").write_text(
        "ticker,name,version\n1111.T,テスト一,1.10\n2222.T,テスト二,1.10\n3333.T,テスト三,1.10\n",
        encoding="utf-8")
    shutil.copy(os.path.join(ROOT, bss.HTML_SOURCE), tmp_path / bss.HTML_SOURCE)
    monkeypatch.setitem(scanner.CONFIG, "end_date", "2026-06-01")
    monkeypatch.setenv("END_DATE", "2026-06-01")  # build は default_end_date() を基準にする
    monkeypatch.setattr(bss.time, "sleep", lambda s: None)

    def fake_load(code):
        if code == "2222":
            raise ConnectionError("secret-token https://example.invalid/?key=XYZ")
        return {"metrics": {"end": "2026-05-29", "label": code}, "daily_pnl": []}
    monkeypatch.setattr(scanner, "_load_yahoo_prototype_data", fake_load)
    return tmp_path


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_failed_stock_carried_over_when_previous_exists(workdir, monkeypatch):
    prev = {"metrics": {"end": "2026-04-30", "label": "2222"}, "name": "前回"}
    monkeypatch.setattr(bss, "fetch_previous", lambda code: prev if code == "2222" else None)
    rc = bss.build("_site")
    assert rc == 0
    out = workdir / "_site"
    assert _read(out / "data" / "stocks" / "2222.json") == prev
    assert (out / "data" / "stocks" / "1111.json").exists()
    index = _read(out / "data" / "stocks.json")
    assert index["carried_over"] == ["2222"]
    assert index["available"] == ["1111", "2222"]
    assert index["failed"] == [{"code": "2222", "error": "ConnectionError"}]
    assert index["data_through"] == "2026-05"
    status = _read(out / "data_status.json")
    assert status["carried_over"] == ["2222"]
    assert status["failed"] == [{"code": "2222", "error": "ConnectionError"}]
    assert "XYZ" not in json.dumps(status) and "XYZ" not in json.dumps(index)
    avail = {s["code"]: s["available"] for s in index["stocks"]}
    assert avail == {"1111": True, "2222": True, "3333": False}
    assert (out / ".nojekyll").exists()
    assert "window.STATIC_SITE = true" in (out / "index.html").read_text(encoding="utf-8")


def test_failed_stock_not_output_without_previous(workdir, monkeypatch):
    monkeypatch.setattr(bss, "fetch_previous", lambda code: None)
    rc = bss.build("_site")
    assert rc == 0
    out = workdir / "_site"
    assert not (out / "data" / "stocks" / "2222.json").exists()
    index = _read(out / "data" / "stocks.json")
    assert index["carried_over"] == []
    assert index["available"] == ["1111"]
    assert index["failed"] == [{"code": "2222", "error": "ConnectionError"}]


def test_all_failed_returns_1(workdir, monkeypatch):
    def boom(code):
        raise ValueError("x")
    monkeypatch.setattr(scanner, "_load_yahoo_prototype_data", boom)
    monkeypatch.setattr(bss, "fetch_previous", lambda code: None)
    assert bss.build("_site") == 1


def test_stale_detection(workdir, monkeypatch):
    monkeypatch.setattr(scanner, "_load_yahoo_prototype_data",
                        lambda code: {"metrics": {"end": "2026-04-30"}})
    monkeypatch.setattr(bss, "fetch_previous", lambda code: None)
    bss.build("_site")
    index = _read(workdir / "_site" / "data" / "stocks.json")
    assert {"code": "1111", "last_month": "2026-04"} in index["stale"]


def test_fetch_previous_without_env_returns_none(monkeypatch):
    monkeypatch.delenv("PREVIOUS_SITE_URL", raising=False)
    assert bss.fetch_previous("1111") is None


def test_fetch_previous_swallows_errors(monkeypatch):
    monkeypatch.setenv("PREVIOUS_SITE_URL", "https://example.invalid/site/")
    def fail(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(bss.urllib.request, "urlopen", fail)
    assert bss.fetch_previous("1111") is None


# ---------- prepare_out_dir ----------
def test_prepare_out_dir_refuses_non_generated_dir(tmp_path):
    target = tmp_path / "important"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    with pytest.raises(SystemExit):
        bss.prepare_out_dir(str(target))
    assert (target / "keep.txt").exists()


def test_build_refuses_non_generated_dir(workdir, monkeypatch):
    target = workdir / "repo"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    with pytest.raises(SystemExit):
        bss.build(str(target))
    assert (target / "keep.txt").exists()


def test_prepare_out_dir_removes_site_named_dir(tmp_path):
    target = tmp_path / "_site"
    target.mkdir()
    (target / "old.json").write_text("x")
    bss.prepare_out_dir(str(target) + os.sep)  # 末尾スラッシュ付きでも _site と判定
    assert not target.exists()


def test_prepare_out_dir_removes_dir_with_nojekyll(tmp_path):
    target = tmp_path / "public"
    target.mkdir()
    (target / ".nojekyll").write_text("")
    bss.prepare_out_dir(str(target))
    assert not target.exists()


def test_prepare_out_dir_missing_dir_is_noop(tmp_path):
    bss.prepare_out_dir(str(tmp_path / "nothing"))


def test_carried_over_old_data_is_marked_stale(workdir, monkeypatch):
    prev = {"metrics": {"end": "2026-04-30"}, "name": "前回"}
    monkeypatch.setattr(bss, "fetch_previous", lambda code: prev if code == "2222" else None)
    assert bss.build("_site") == 0
    status = _read(workdir / "_site" / "data_status.json")
    assert status["carried_over"] == ["2222"]
    assert {"code": "2222", "last_month": "2026-04"} in status["stale"]


def test_too_old_previous_is_not_carried(workdir, monkeypatch):
    prev = {"metrics": {"end": "2026-01-30"}, "name": "古すぎる"}  # 4か月遅れ > 上限2か月
    monkeypatch.setattr(bss, "fetch_previous", lambda code: prev)
    assert bss.build("_site") == 0
    assert not (workdir / "_site" / "data" / "stocks" / "2222.json").exists()
    assert _read(workdir / "_site" / "data_status.json")["carried_over"] == []


def test_malformed_previous_is_ignored(workdir, monkeypatch):
    monkeypatch.setattr(bss, "fetch_previous", lambda code: ["not", "a", "dict"])
    assert bss.build("_site") == 0
    assert not (workdir / "_site" / "data" / "stocks" / "2222.json").exists()


def test_only_carried_over_counts_as_failure(workdir, monkeypatch):
    """Yahoo全面障害で全銘柄が前回分の引き継ぎだけなら、成功扱いにしない。"""
    def all_fail(code):
        raise ConnectionError("down")
    monkeypatch.setattr(scanner, "_load_yahoo_prototype_data", all_fail)
    monkeypatch.setattr(bss, "fetch_previous", lambda code: {"metrics": {"end": "2026-05-29"}})
    assert bss.build("_site") == 1

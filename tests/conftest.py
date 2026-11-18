import os
import socket
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Yahoo Finance など外部への接続を禁止する（誤接続をテスト失敗として検出）。"""
    def _blocked(*args, **kwargs):
        raise RuntimeError("テスト中のネットワーク接続は禁止されています")
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.delenv("PREVIOUS_SITE_URL", raising=False)

"""apply_pending - DB 스캔 → 미적용 액션 집계/적용 회귀 테스트.

IMAP(_apply_account_actions)·계정 로딩은 monkeypatch. 실행: repo 루트에서 py -m pytest
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_app import apply_pending  # noqa: E402
from mail_app.mail_log_store import connect, upsert_messages  # noqa: E402


def _cat(action, **kw):
    kws = {"senders": [], "domains": [], "title": [], "contents": []}
    kws.update(kw)
    return {"description": "", "action": action, "priority": "NORMAL", "keywords": kws}


CATEGORIES = {
    "광고": _cat("trash", domains=["ads.example"]),
    "영수증": _cat("save", senders=["receipt@shop.example"]),
    "관심": _cat("keep", title=["뉴스레터"]),
}


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "app.db"
    connect(p).close()  # 스키마 생성
    now = datetime.now()
    recent = (now - timedelta(days=3)).isoformat()
    old = (now - timedelta(days=400)).isoformat()
    upsert_messages(p, [
        # 최근 - trash 대상
        dict(account="a@x.com", uid="1", account_type="gmail", sender="promo@ads.example",
             subject="세일", message_date=recent, web_link=None, message_id="<1>"),
        # 최근 - save 대상
        dict(account="a@x.com", uid="2", account_type="gmail", sender="receipt@shop.example",
             subject="주문", message_date=recent, web_link=None, message_id="<2>"),
        # 최근 - keep(대상 아님)
        dict(account="a@x.com", uid="3", account_type="gmail", sender="x@y.com",
             subject="뉴스레터 9월", message_date=recent, web_link=None, message_id="<3>"),
        # 오래됨 - trash 대상이지만 기본 30일 밖
        dict(account="a@x.com", uid="4", account_type="gmail", sender="promo@ads.example",
             subject="옛세일", message_date=old, web_link=None, message_id="<4>"),
    ])
    return p


def test_count_pending_default_window(db):
    c = apply_pending.count_pending(db, CATEGORIES)
    assert c["total"] == 2
    assert c["by_action"] == {"trash": 1, "save": 1}
    assert c["by_account"] == {"a@x.com": 2}


def test_count_pending_wide_window_includes_old(db):
    assert apply_pending.count_pending(db, CATEGORIES, days=730)["total"] == 3


def test_pending_excludes_already_read_for_read_action(db):
    cats = {"알림": _cat("read", senders=["promo@ads.example"])}
    conn = connect(db)
    conn.execute("UPDATE messages SET is_read = 1 WHERE uid = '1'")
    conn.commit()
    conn.close()
    assert apply_pending.count_pending(db, cats)["total"] == 0  # uid1은 이미 읽음


def test_apply_pending_moves_and_syncs_db(db, monkeypatch):
    monkeypatch.setattr(apply_pending, "load_categories", lambda _p: CATEGORIES)
    monkeypatch.setattr(apply_pending, "load_accounts",
                        lambda _p: [{"user": "a@x.com", "type": "gmail", "password": "pw"}])

    seen = {}

    def fake_apply(account, grouped):
        seen["grouped"] = grouped
        return [
            {"action": a, "candidates": len(u), "succeeded": list(u), "failed": 0,
             "folder": a, "note": None}
            for a, u in grouped.items()
        ]

    monkeypatch.setattr(apply_pending, "_apply_account_actions", fake_apply)

    summary = apply_pending.apply_pending(db, Path("unused"))
    assert summary["applied"] == 2 and summary["failed"] == 0
    assert set(seen["grouped"]) == {"trash", "save"}

    conn = connect(db)
    statuses = dict(conn.execute("SELECT uid, status FROM messages").fetchall())
    conn.close()
    assert statuses["1"] == "trashed"
    assert statuses["2"] == "archived"
    assert statuses["3"] == "active"


def test_apply_pending_account_missing_is_reported(db, monkeypatch):
    monkeypatch.setattr(apply_pending, "load_categories", lambda _p: CATEGORIES)
    monkeypatch.setattr(apply_pending, "load_accounts", lambda _p: [])
    summary = apply_pending.apply_pending(db, Path("unused"))
    assert summary["applied"] == 0 and summary["failed"] == 2
    assert summary["accounts"][0]["note"] == "계정 설정 없음"

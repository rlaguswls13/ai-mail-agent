"""classify_eval - 지표 계산 / __none__ 처리 / dedupe / macro-F1 게이트 회귀 테스트.

실행: repo 루트에서  py -m pytest
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_app import classify_eval  # noqa: E402
from mail_app.config_store import add_category, connect  # noqa: E402

NONE = classify_eval.NONE_LABEL


def _cat(action, **kw):
    kws = {"senders": [], "domains": [], "title": [], "contents": []}
    kws.update(kw)
    return {"description": "", "action": action, "priority": "NORMAL", "keywords": kws}


# 손으로 검산한 케이스: 카테고리 2개, 메일 5건.
CATEGORIES = {
    "광고": _cat("trash", title=["세일"]),
    "영수증": _cat("save", senders=["r@shop.com"]),
}

RECORDS = [
    {"key": "k1", "sender": "x@a.com", "subject": "여름 세일", "label": "광고", "source": "manual", "labeled_at": "t"},
    {"key": "k2", "sender": "r@shop.com", "subject": "주문 확인", "label": "영수증", "source": "manual", "labeled_at": "t"},
    {"key": "k3", "sender": "y@b.com", "subject": "안녕하세요", "label": NONE, "source": "manual", "labeled_at": "t"},
    {"key": "k4", "sender": "z@c.com", "subject": "세일 안내", "label": NONE, "source": "manual", "labeled_at": "t"},
    {"key": "k5", "sender": "r@shop.com", "subject": "뉴스레터", "label": "광고", "source": "manual", "labeled_at": "t"},
]


def test_predict_labels_maps_none_for_uncategorized():
    pred = classify_eval.predict_labels(RECORDS, CATEGORIES)
    assert pred == {"k1": "광고", "k2": "영수증", "k3": NONE, "k4": "광고", "k5": "영수증"}


def test_metrics_hand_checked():
    m = classify_eval.evaluate(RECORDS, CATEGORIES)
    assert m["total"] == 5
    assert m["accuracy"] == pytest.approx(0.6)
    assert m["uncategorized_rate"] == pytest.approx(0.2)
    assert m["macro_f1"] == pytest.approx((0.5 + 2 / 3) / 2)

    pc = m["per_category"]
    assert pc["광고"]["precision"] == pytest.approx(0.5)
    assert pc["광고"]["recall"] == pytest.approx(0.5)
    assert pc["광고"]["f1"] == pytest.approx(0.5)
    assert pc["광고"]["support"] == 2
    assert pc["영수증"]["precision"] == pytest.approx(0.5)
    assert pc["영수증"]["recall"] == pytest.approx(1.0)
    assert pc["영수증"]["support"] == 1
    assert pc[NONE]["precision"] == pytest.approx(1.0)
    assert pc[NONE]["recall"] == pytest.approx(0.5)
    assert pc[NONE]["support"] == 2

    assert m["confusion"]["광고"] == {NONE: 0, "광고": 1, "영수증": 1}
    assert m["confusion"][NONE] == {NONE: 1, "광고": 1, "영수증": 0}


def test_worst_lists_only_misclassifications():
    m = classify_eval.evaluate(RECORDS, CATEGORIES)
    assert m["worst_total"] == 2
    keys = {(w["true"], w["pred"]) for w in m["worst"]}
    assert keys == {(NONE, "광고"), ("광고", "영수증")}


def test_macro_f1_excludes_none_label():
    # __none__ 만 있는 데이터셋 -> real label 없음 -> macro_f1 0.0
    recs = [{"key": "a", "sender": "s", "subject": "j", "label": NONE, "source": "manual", "labeled_at": "t"}]
    m = classify_eval.evaluate(recs, CATEGORIES)
    assert m["macro_f1"] == 0.0
    assert NONE in m["per_category"]


def test_dedupe_prefers_manual_over_public():
    recs = [
        {"key": "dup", "sender": "s", "subject": "j", "label": "광고", "source": "public:x", "labeled_at": "t1"},
        {"key": "dup", "sender": "s", "subject": "j", "label": "영수증", "source": "manual", "labeled_at": "t2"},
        {"key": "solo", "sender": "s", "subject": "j", "label": NONE, "source": "public:x", "labeled_at": "t1"},
    ]
    out = {r["key"]: r for r in classify_eval.dedupe_records(recs)}
    assert len(out) == 2
    assert out["dup"]["label"] == "영수증"
    assert out["solo"]["source"] == "public:x"


def test_dedupe_order_independent():
    recs = [
        {"key": "d", "sender": "s", "subject": "j", "label": "영수증", "source": "manual", "labeled_at": "t"},
        {"key": "d", "sender": "s", "subject": "j", "label": "광고", "source": "public:x", "labeled_at": "t"},
    ]
    out = classify_eval.dedupe_records(recs)
    assert len(out) == 1 and out[0]["label"] == "영수증"


@pytest.fixture
def env(tmp_path):
    labels = tmp_path / "labels"
    labels.mkdir()
    with open(labels / "manual.jsonl", "w", encoding="utf-8") as fh:
        for r in RECORDS:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        fh.write("\n")  # 빈 줄 무시 확인

    db = tmp_path / "app.db"
    connect(db).close()
    add_category(db, "광고", "", "trash", "NORMAL", [], ["세일"], [], [])
    add_category(db, "영수증", "", "save", "NORMAL", ["r@shop.com"], [], [], [])
    return labels, db


def test_main_json_output(env, capsys):
    labels, db = env
    rc = classify_eval.main(["--json", "--labels-dir", str(labels), "--db", str(db)])
    assert rc == 0
    m = json.loads(capsys.readouterr().out)
    assert m["total"] == 5
    assert m["macro_f1"] == pytest.approx((0.5 + 2 / 3) / 2)


def test_main_min_macro_f1_gate(env, capsys):
    labels, db = env
    assert classify_eval.main(["--labels-dir", str(labels), "--db", str(db), "--min-macro-f1", "0.5"]) == 0
    assert classify_eval.main(["--labels-dir", str(labels), "--db", str(db), "--min-macro-f1", "0.9"]) == 1


def test_main_no_labels_path(tmp_path, capsys):
    empty = tmp_path / "labels"
    empty.mkdir()
    rc = classify_eval.main(["--labels-dir", str(empty)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "import_labels" in out and "/label" in out

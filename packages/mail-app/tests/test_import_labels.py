"""import_labels 회귀 테스트 - corpus(mbox/Maildir) + csv 어댑터, 출력 계약 검증.

실행: repo 루트 또는 packages/mail-app/ 에서  py -m pytest
"""
import json
import mailbox
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_app import config_store as cfg  # noqa: E402
from mail_app import import_labels  # noqa: E402

CONTRACT_KEYS = {"key", "sender", "subject", "label", "source", "labeled_at"}


def _msg_bytes(sender: str, subject: str) -> bytes:
    m = EmailMessage()
    m["From"] = sender
    m["Subject"] = subject  # 비ASCII 는 as_bytes() 가 RFC2047 로 인코딩
    m.set_content("body")
    return m.as_bytes()


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_corpus_maildir_and_mbox_match_contract(tmp_path):
    # spam: Maildir (RFC2047 제목 포함)
    md_path = tmp_path / "spam_maildir"
    md = mailbox.Maildir(str(md_path))
    md.add(_msg_bytes("promo@ads.example", "무료 경품 당첨"))  # 한글 -> encoded-word
    md.add(_msg_bytes("Deals <sale@ads.example>", "Cheap meds"))

    # ham: mbox 파일
    mbox_path = tmp_path / "ham.mbox"
    mb = mailbox.mbox(str(mbox_path))
    mb.add(_msg_bytes("friend@example.org", "lunch tomorrow?"))
    mb.flush()
    mb.close()

    out = tmp_path / "out.jsonl"
    summary = import_labels.run_corpus(
        name="sa1", spam=str(md_path), ham=str(mbox_path), out=str(out)
    )

    recs = _read_jsonl(out)
    assert len(recs) == 3
    assert summary["by_label"] == {"ad": 2, "__none__": 1}
    for i, r in enumerate(recs):
        assert set(r) == CONTRACT_KEYS
        assert r["key"] == f"pub:sa1:{i}"
        assert r["source"] == "public:sa1"
        datetime.fromisoformat(r["labeled_at"])  # 유효한 ISO8601

    by_subject = {r["subject"]: r for r in recs}
    assert by_subject["무료 경품 당첨"]["label"] == "ad"  # RFC2047 디코딩됨
    assert by_subject["무료 경품 당첨"]["sender"] == "promo@ads.example"
    assert by_subject["Cheap meds"]["sender"] == "sale@ads.example"  # parseaddr
    assert by_subject["lunch tomorrow?"]["label"] == "__none__"


def test_corpus_dedupes_within_file(tmp_path):
    md_path = tmp_path / "spam"
    md = mailbox.Maildir(str(md_path))
    md.add(_msg_bytes("a@ads.example", "same"))
    md.add(_msg_bytes("a@ads.example", "same"))
    md.add(_msg_bytes("a@ads.example", "other"))

    out = tmp_path / "o.jsonl"
    summary = import_labels.run_corpus(name="d", spam=str(md_path), out=str(out))
    assert summary["total"] == 2


def test_csv_valid_rows_imported_unknown_label_skipped(tmp_path, capsys):
    db = tmp_path / "app.db"
    cfg.add_category(db, name="ad", description="", action="trash", priority="NORMAL",
                     senders=[], title=[], contents=[], domains=[])

    csv_path = tmp_path / "labels.csv"
    csv_path.write_text(
        "sender,subject,label\n"
        "spam@x.com,사은품,ad\n"
        "hr@y.com,legit notice,__none__\n"
        "bad@z.com,mystery,not_a_category\n",
        encoding="utf-8",
    )

    out = tmp_path / "out.jsonl"
    summary = import_labels.run_csv(
        name="kr", input_path=str(csv_path), db_path=db, out=str(out)
    )

    recs = _read_jsonl(out)
    assert len(recs) == 2
    assert summary["by_label"] == {"ad": 1, "__none__": 1}
    assert all(set(r) == CONTRACT_KEYS for r in recs)
    assert recs[0]["key"] == "pub:kr:0"
    assert recs[0]["source"] == "public:kr"

    warn = capsys.readouterr().err
    assert "not_a_category" in warn


def test_csv_limit_caps_records(tmp_path):
    db = tmp_path / "app.db"
    cfg.add_category(db, name="ad", description="", action="trash", priority="NORMAL",
                     senders=[], title=[], contents=[], domains=[])
    csv_path = tmp_path / "l.csv"
    csv_path.write_text(
        "sender,subject,label\n"
        "a@x.com,one,ad\nb@x.com,two,ad\nc@x.com,three,ad\n",
        encoding="utf-8",
    )
    out = tmp_path / "o.jsonl"
    summary = import_labels.run_csv(
        name="k", input_path=str(csv_path), db_path=db, limit=2, out=str(out)
    )
    assert summary["total"] == 2

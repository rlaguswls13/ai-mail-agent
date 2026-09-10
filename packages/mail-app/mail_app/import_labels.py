"""공개(public) 라벨링된 메일 데이터셋을 분류 규칙 튜닝용 라벨 데이터로 흡수한다.

이 프로젝트의 분류기(`mail_core.classify`)는 발신인(sender)과 제목(subject)만 본다
(본문 미저장). 규칙 튜닝을 사용자 본인의 ~4000건 헤더로만 평가하면 과적합하므로,
공개 데이터셋 - 특히 스팸/정상(ham) 경계 - 을 같은 라벨 포맷으로 합쳐 넣는다.

카테고리는 개인화돼 있어 공개 데이터로 채울 수 있는 건 사실상 스팸 경계뿐이다:
스팸 -> 카테고리 `ad` (action=trash), 정상(ham) -> `__none__` (액션 대상 아님 / 정당하게 미분류).

공개 코퍼스 구하는 곳 (이 도구는 자동 다운로드하지 않는다 - 사용자가 직접 받아
로컬 경로를 넘긴다):
  - SpamAssassin public corpus: https://spamassassin.apache.org/old/publiccorpus/
    (spam / easy_ham / hard_ham 아카이브. 압축 풀면 각각 raw 메시지 파일 디렉터리)
  - Enron email dataset, TREC 2007 Public Spam Corpus 도 옵션.
강한 공개 한국어 메일 데이터셋은 없다. 손으로 만든/ KISA 파생 한국어 목록은
`csv` 어댑터(sender,subject,label 열)로 넣는다.

출력: JSONL, 한 줄에 JSON 객체 하나, `data/labels/public-<name>.jsonl` 로.
  {"key": "pub:<name>:<idx>", "sender": str, "subject": str, "label": str,
   "source": "public:<name>", "labeled_at": <ISO8601>}
`label` 은 카테고리 이름이거나 리터럴 "__none__".

    python -m mail_app.import_labels corpus --name sa2003 \\
        --spam /path/to/spam --ham /path/to/easy_ham
    python -m mail_app.import_labels csv --name kr_hand --input labels.csv
"""
import argparse
import csv
import email
import json
import mailbox
import sys
from datetime import datetime
from email.header import decode_header
from email.utils import parseaddr
from pathlib import Path

from mail_app import app_paths
from mail_app.config_store import load_categories

NONE_LABEL = "__none__"
SPAM_LABEL = "ad"


def _decode_mime_header(raw: str) -> str:
    """RFC2047 인코딩 워드(=?UTF-8?B?...?=)를 사람이 읽는 문자열로 편다."""
    if not raw:
        return ""
    out = []
    for text, enc in decode_header(raw):
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return " ".join("".join(out).split()).strip()


def _sender_of(raw_from: str) -> str:
    decoded = _decode_mime_header(raw_from)
    _name, addr = parseaddr(decoded)
    return (addr or decoded).strip()


def _iter_corpus(path: str):
    """mbox 파일 / Maildir / raw 메시지 파일 디렉터리를 자동 판별해 Message 를 낸다.

    SpamAssassin public corpus 는 cur/new/tmp 없는 평범한 파일 디렉터리라 Maildir 로
    안 열린다 - 그 경우 파일을 직접 파싱한다.
    """
    p = Path(path)
    if p.is_file():
        box = mailbox.mbox(str(p))
        try:
            for msg in box:
                yield msg
        finally:
            box.close()
        return
    if not p.is_dir():
        raise FileNotFoundError(f"경로를 찾을 수 없습니다: {path}")
    if all((p / d).is_dir() for d in ("cur", "new", "tmp")):
        for msg in mailbox.Maildir(str(p), factory=None):
            yield msg
        return
    for f in sorted(p.rglob("*")):
        if not f.is_file():
            continue
        try:
            with f.open("rb") as fh:
                yield email.message_from_binary_file(fh)
        except Exception:  # noqa: BLE001 - 깨진 파일 하나가 전체를 막지 않게
            continue


def _record(name: str, idx: int, sender: str, subject: str, label: str) -> dict:
    return {
        "key": f"pub:{name}:{idx}",
        "sender": sender,
        "subject": subject,
        "label": label,
        "source": f"public:{name}",
        "labeled_at": datetime.now().isoformat(timespec="seconds"),
    }


def _resolve_out(name: str, out: str | None) -> Path:
    if out:
        return Path(out)
    d = app_paths.data_dir() / "labels"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"public-{name}.jsonl"


def _write(records: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _summary(records: list[dict], out_path: Path) -> dict:
    counts: dict[str, int] = {}
    for r in records:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    return {"total": len(records), "by_label": counts, "out": str(out_path)}


def run_corpus(*, name, spam=None, ham=None, limit=None, out=None) -> dict:
    if not spam and not ham:
        raise SystemExit("--spam 또는 --ham 중 적어도 하나는 필요합니다.")
    seen: set[tuple[str, str]] = set()
    records: list[dict] = []
    for path, label in ((spam, SPAM_LABEL), (ham, NONE_LABEL)):
        if not path:
            continue
        for msg in _iter_corpus(path):
            sender = _sender_of(msg.get("From", ""))
            subject = _decode_mime_header(msg.get("Subject", ""))
            if not sender and not subject:
                continue
            dk = (sender, subject)
            if dk in seen:
                continue
            seen.add(dk)
            records.append(_record(name, len(records), sender, subject, label))
            if limit and len(records) >= limit:
                out_path = _resolve_out(name, out)
                _write(records, out_path)
                return _summary(records, out_path)
    out_path = _resolve_out(name, out)
    _write(records, out_path)
    return _summary(records, out_path)


def run_csv(*, name, input_path, db_path=None, limit=None, out=None) -> dict:
    db_path = db_path or app_paths.db_path()
    valid = set(load_categories(db_path)) | {NONE_LABEL}
    seen: set[tuple[str, str]] = set()
    records: list[dict] = []
    with open(input_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = {"sender", "subject", "label"} - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"CSV에 필요한 열이 없습니다: {', '.join(sorted(missing))}")
        for row in reader:
            sender = (row.get("sender") or "").strip()
            subject = (row.get("subject") or "").strip()
            label = (row.get("label") or "").strip()
            if label not in valid:
                print(
                    f"경고: 알 수 없는 라벨 '{label}' - 건너뜀 ({sender} / {subject})",
                    file=sys.stderr,
                )
                continue
            dk = (sender, subject)
            if dk in seen:
                continue
            seen.add(dk)
            records.append(_record(name, len(records), sender, subject, label))
            if limit and len(records) >= limit:
                break
    out_path = _resolve_out(name, out)
    _write(records, out_path)
    return _summary(records, out_path)


def _print_summary(summary: dict) -> None:
    print(f"가져온 라벨: {summary['total']}건")
    for label, n in sorted(summary["by_label"].items()):
        print(f"  {label}: {n}")
    print(f"출력: {summary['out']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="공개 라벨링된 메일 데이터셋을 라벨 JSONL 로 가져온다",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("corpus", help="로컬 SpamAssassin 형식 코퍼스 (mbox / Maildir / 파일 디렉터리)")
    c.add_argument("--name", required=True, help="데이터셋 슬러그 (key/source 에 들어감)")
    c.add_argument("--spam", help="스팸 코퍼스 경로 -> 라벨 ad")
    c.add_argument("--ham", help="정상(ham) 코퍼스 경로 -> 라벨 __none__")
    c.add_argument("--limit", type=int, help="최대 레코드 수")
    c.add_argument("--out", help="출력 경로 재지정")

    v = sub.add_parser("csv", help="sender,subject,label 열을 가진 일반 CSV")
    v.add_argument("--name", required=True, help="데이터셋 슬러그")
    v.add_argument("--input", required=True, help="입력 CSV 파일")
    v.add_argument("--limit", type=int, help="최대 레코드 수")
    v.add_argument("--out", help="출력 경로 재지정")

    args = ap.parse_args(argv)
    if args.cmd == "corpus":
        summary = run_corpus(name=args.name, spam=args.spam, ham=args.ham,
                             limit=args.limit, out=args.out)
    else:
        summary = run_csv(name=args.name, input_path=args.input,
                          limit=args.limit, out=args.out)
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())

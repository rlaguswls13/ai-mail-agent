"""라벨링된 메일로 현재 분류 규칙의 정밀도·재현율을 측정한다.

대시보드의 "미분류율" 지표만으로는 규칙을 바꿨을 때 **어느 카테고리가** 좋아지고
나빠졌는지 알 수 없다. 이 CLI 는 사람이 정답을 매긴 데이터셋(`data/labels/*.jsonl`)에
현재 카테고리 규칙(`load_categories`)을 그대로 돌려서 카테고리별
precision / recall / F1 / support, macro-F1, 혼동 행렬, 최악의 오분류 목록을 낸다.

    python -m mail_app.classify_eval              # 사람이 읽는 리포트
    python -m mail_app.classify_eval --json       # 기계용 지표 dict
    python -m mail_app.classify_eval --min-macro-f1 0.7   # 미달 시 exit 1 (CI/회귀)

라벨 파일 한 줄의 계약(다른 도구가 생성한다):

    {"key": str, "sender": str, "subject": str, "label": str,
     "source": str, "labeled_at": str}

`label` 은 카테고리 이름이거나 리터럴 ``"__none__"`` (정상적으로 미분류 =
액션 대상이 아님). `source` 는 ``"manual"`` 또는 ``"public:<name>"``.
`key` 로 중복을 제거하며 같은 key 는 manual 을 public 보다 우선한다.

본문 키워드(categories.contents) 규칙은 라벨 데이터에 본문이 없어 여기서 매칭되지
않는다 - 발신인/도메인/제목 규칙 기준이다(대시보드 재분류와 동일한 한계).
"""
import argparse
import glob
import json
import sys
from pathlib import Path

from mail_core.classify import classify

from mail_app import app_paths
from mail_app.config_store import load_categories

NONE_LABEL = "__none__"

DB_PATH = app_paths.db_path()


def labels_dir() -> Path:
    """라벨 jsonl 이 사는 곳 - 메일 데이터 디렉터리 아래 ``labels/``."""
    return app_paths.data_dir() / "labels"


def load_labeled_records(directory: Path) -> list[dict]:
    """``<directory>/*.jsonl`` 을 모두 읽어 레코드 리스트로 돌려준다.

    빈 줄은 건너뛴다. 깨진 JSON 한 줄은 ``ValueError`` 로 즉시 알린다(조용히
    버리면 지표가 소리 없이 틀어진다).
    """
    records: list[dict] = []
    for path in sorted(glob.glob(str(directory / "*.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{lineno} JSON 파싱 실패: {exc}") from exc
    return records


def dedupe_records(records: list[dict]) -> list[dict]:
    """``key`` 로 중복 제거. 같은 key 는 ``source == "manual"`` 을 우선한다.

    manual 이 아직 없으면 먼저 본 레코드를 유지하고, 나중에 manual 이 나오면 교체한다.
    """
    chosen: dict[str, dict] = {}
    for rec in records:
        key = rec["key"]
        current = chosen.get(key)
        if current is None:
            chosen[key] = rec
        elif current.get("source") != "manual" and rec.get("source") == "manual":
            chosen[key] = rec
    return list(chosen.values())


def predict_labels(records: list[dict], categories: dict) -> dict[str, str]:
    """``{key: 예측 라벨}`` - classify() 가 매칭한 카테고리, 없으면 ``__none__``."""
    messages = [
        {"sender": r.get("sender", ""), "subject": r.get("subject", ""), "uid": r["key"]}
        for r in records
    ]
    result = classify(messages, categories, sample_cap=None)

    predicted = {r["key"]: NONE_LABEL for r in records}
    for name, matched in result["category_matches"].items():
        for msg in matched:
            predicted[msg["uid"]] = name
    return predicted


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def compute_metrics(pairs: list[tuple[str, str]]) -> dict:
    """``(정답, 예측)`` 쌍에서 전체 지표를 계산한다.

    반환 dict: ``total``, ``accuracy``, ``uncategorized_rate``, ``macro_f1``,
    ``per_category`` ({label: {precision, recall, f1, support}}),
    ``confusion`` ({true: {pred: count}}), ``labels`` (등장한 라벨 정렬 목록).
    """
    total = len(pairs)
    labels = sorted({lbl for pair in pairs for lbl in pair})

    confusion: dict[str, dict[str, int]] = {t: {p: 0 for p in labels} for t in labels}
    for true, pred in pairs:
        confusion[true][pred] += 1

    per_category: dict[str, dict] = {}
    for lbl in labels:
        tp = confusion[lbl][lbl]
        fp = sum(confusion[t][lbl] for t in labels if t != lbl)
        fn = sum(confusion[lbl][p] for p in labels if p != lbl)
        support = sum(confusion[lbl].values())
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        per_category[lbl] = {
            "precision": precision,
            "recall": recall,
            "f1": _f1(precision, recall),
            "support": support,
        }

    real_labels = [lbl for lbl in labels if lbl != NONE_LABEL]
    macro_f1 = (
        sum(per_category[lbl]["f1"] for lbl in real_labels) / len(real_labels)
        if real_labels
        else 0.0
    )
    correct = sum(1 for true, pred in pairs if true == pred)
    uncategorized = sum(1 for _true, pred in pairs if pred == NONE_LABEL)

    return {
        "total": total,
        "accuracy": correct / total if total else 0.0,
        "uncategorized_rate": uncategorized / total if total else 0.0,
        "macro_f1": macro_f1,
        "per_category": per_category,
        "confusion": confusion,
        "labels": labels,
    }


def evaluate(records: list[dict], categories: dict) -> dict:
    """중복 제거된 레코드 + 카테고리로 지표 dict 를 만든다.

    ``worst`` 에는 오분류(예측 != 정답) 레코드가 최대 20건 담긴다.
    """
    records = dedupe_records(records)
    predicted = predict_labels(records, categories)
    pairs = [(r["label"], predicted[r["key"]]) for r in records]

    metrics = compute_metrics(pairs)
    worst = [
        {
            "sender": r.get("sender", ""),
            "subject": r.get("subject", ""),
            "true": r["label"],
            "pred": predicted[r["key"]],
        }
        for r in records
        if r["label"] != predicted[r["key"]]
    ]
    metrics["worst"] = worst[:20]
    metrics["worst_total"] = len(worst)
    return metrics


def format_report(metrics: dict) -> str:
    """사람이 읽는 리포트 문자열."""
    lines: list[str] = []
    lines.append(f"라벨 {metrics['total']}건 · 정확도 {metrics['accuracy']:.3f} · "
                 f"macro-F1 {metrics['macro_f1']:.3f} · "
                 f"미분류율 {metrics['uncategorized_rate']:.3f}")
    lines.append("")

    lines.append(f"{'category':<24} {'prec':>7} {'recall':>7} {'f1':>7} {'support':>8}")
    for lbl in metrics["labels"]:
        m = metrics["per_category"][lbl]
        lines.append(f"{lbl:<24} {m['precision']:>7.3f} {m['recall']:>7.3f} "
                     f"{m['f1']:>7.3f} {m['support']:>8}")
    lines.append("")

    lines.append("혼동 행렬 (행=정답, 열=예측):")
    labels = metrics["labels"]
    header = " " * 24 + "".join(f"{lbl[:10]:>12}" for lbl in labels)
    lines.append(header)
    for true in labels:
        row = "".join(f"{metrics['confusion'][true][p]:>12}" for p in labels)
        lines.append(f"{true:<24}{row}")
    lines.append("")

    lines.append(f"최악의 오분류 (총 {metrics['worst_total']}건, 최대 20건 표시):")
    for w in metrics["worst"]:
        lines.append(f"  [{w['true']} -> {w['pred']}] {w['sender']} | {w['subject']}")
    return "\n".join(lines)


_NO_LABELS_MSG = """라벨 파일이 없습니다: {directory}/*.jsonl

평가하려면 먼저 정답 데이터를 만드세요:
  - python -m mail_app.import_labels        # 기존 분류 결과/공개 데이터셋 가져오기
  - 또는 admin-ui 의 /label 페이지에서 직접 라벨링

라벨 파일 한 줄 형식:
  {{"key": "...", "sender": "...", "subject": "...", "label": "카테고리명 또는 __none__", "source": "manual", "labeled_at": "..."}}
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="라벨 데이터로 분류 규칙 정밀도·재현율 측정")
    ap.add_argument("--json", action="store_true", help="지표 dict 를 JSON 으로 출력")
    ap.add_argument("--labels-dir", type=Path, default=None, help="라벨 jsonl 디렉터리 (기본: data/labels)")
    ap.add_argument("--db", type=Path, default=None, help="app.db 경로 (기본: data/app.db)")
    ap.add_argument("--min-macro-f1", type=float, default=None,
                    help="macro-F1 이 이 값 미만이면 exit 1 (CI/회귀 감지용)")
    args = ap.parse_args(argv)

    directory = args.labels_dir or labels_dir()
    db_path = args.db or DB_PATH

    records = load_labeled_records(directory)
    if not records:
        print(_NO_LABELS_MSG.format(directory=directory))
        return 0

    categories = load_categories(db_path)
    metrics = evaluate(records, categories)

    if args.json:
        print(json.dumps(metrics, ensure_ascii=False))
    else:
        print(format_report(metrics))

    if args.min_macro_f1 is not None and metrics["macro_f1"] < args.min_macro_f1:
        print(f"macro-F1 {metrics['macro_f1']:.3f} < 기준 {args.min_macro_f1}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

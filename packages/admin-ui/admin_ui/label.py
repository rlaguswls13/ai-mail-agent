"""admin_ui `/label` 블루프린트 - 수동 라벨링(정답 카테고리 지정) 도구.

규칙(classify.py)이 예측한 카테고리와 별개로, 사용자가 "이 메일의 진짜 카테고리"를
지정해 eval 하네스용 ground-truth 데이터를 쌓는다. 저장소는 label_store.py
(data/app.db 의 message_labels 테이블), 내보내기는 data/labels/manual.jsonl.

- GET  /label         : 메일 목록(예측 카테고리 + 라벨 <select>) + 페이지네이션 + 요약
- POST /label         : label[<key>]=<value> 다건 upsert
- POST /label/export  : message_labels -> data/labels/manual.jsonl

보안: 상태 변경 POST 는 admin_app._block_cross_origin_writes(@app.before_request)가
cross-origin/DNS-rebinding 으로부터 한 곳에서 막는다. 여기엔 자체 CSRF 방어가 없다.
"""
import math
from datetime import datetime, timedelta

from flask import Blueprint, redirect, request

from mail_app import label_store

from admin_ui._shared import DATA_DIR, DB_PATH, build_qs, esc, page, run_state
from admin_ui._render import (
    MSG_DEFAULT_SINCE_DAYS,
    MSG_PAGE_SIZE_MAX,
    MSG_PAGE_SIZE_MIN,
    _account_options,
    _parse_page_num,
    _parse_page_size,
    load_all_messages,
    msg_date_cell,
    render_pagination,
)

bp = Blueprint("label", __name__)

LABELS_JSONL_PATH = DATA_DIR / "labels" / "manual.jsonl"


def _label_select(key: str, categories: dict, current: str | None) -> str:
    opts = [f'<option value=""{"" if current else " selected"}></option>']
    opts.append(
        f'<option value="{label_store.NONE_LABEL}"'
        f'{" selected" if current == label_store.NONE_LABEL else ""}>'
        f'{label_store.NONE_LABEL} (해당 없음)</option>'
    )
    for name in categories:
        opts.append(
            f'<option value="{esc(name)}"{" selected" if name == current else ""}>{esc(name)}</option>'
        )
    return f'<select name="label[{esc(key)}]">{"".join(opts)}</select>'


def _label_status_html(run: dict | None) -> str:
    if run is None:
        return ""
    return (
        f'<div class="run-status"><strong>{esc(run["msg"])}</strong> · {run["ran_at"]}</div>'
    )


def _summary_html(categories: dict, predicted_counts: dict, label_counts: dict, total_labeled: int) -> str:
    rows = [f'<div>전체 라벨: <b>{total_labeled}</b>건</div>']
    for name in categories:
        rows.append(
            f'<div>{esc(name)}: <b>{label_counts.get(name, 0)}</b> / {predicted_counts.get(name, 0)}</div>'
        )
    rows.append(
        f'<div>{label_store.NONE_LABEL}: <b>{label_counts.get(label_store.NONE_LABEL, 0)}</b> '
        f'/ {predicted_counts.get(None, 0)}</div>'
    )
    return f'<div class="run-status"><strong>요약 (라벨 / 예측)</strong>{"".join(rows)}</div>'


@bp.route("/label")
def label_page():
    account_filter = request.args.get("account", "").strip() or None
    only = request.args.get("only", "").strip() or None
    page_size = _parse_page_size(request.args.get("page_size"))

    since = datetime.now() - timedelta(days=MSG_DEFAULT_SINCE_DAYS)
    all_messages, all_users, account_type_by_user, categories = load_all_messages(
        since, None, account_filter
    )

    predicted_counts: dict = {}
    for m in all_messages:
        predicted_counts[m["_category"]] = predicted_counts.get(m["_category"], 0) + 1

    label_map = label_store.get_label_map(DB_PATH)
    label_counts: dict = {}
    for lbl in label_map.values():
        label_counts[lbl] = label_counts.get(lbl, 0) + 1

    pool = list(all_messages)
    if only == "uncat":
        pool = [m for m in pool if m["_category"] is None]
    pool.sort(key=lambda m: m.get("message_date") or "", reverse=True)

    total = len(pool)
    total_pages = max(1, math.ceil(total / page_size))
    page_num = min(_parse_page_num(request.args.get("page")), total_pages)
    start = (page_num - 1) * page_size
    page_items = pool[start : start + page_size]

    account_options = _account_options(all_users, account_type_by_user, account_filter)
    filter_state = {"account": account_filter or "", "only": only or "", "page_size": page_size}
    filter_form = f"""
    <form class="filter-form" method="get" action="/label">
      <label>계정<select name="account">{account_options}</select></label>
      <label>미분류만<input type="checkbox" name="only" value="uncat"{" checked" if only == "uncat" else ""}></label>
      <label>페이지당 건수<input type="number" name="page_size" min="{MSG_PAGE_SIZE_MIN}" max="{MSG_PAGE_SIZE_MAX}" value="{page_size}"></label>
      <button class="btn" type="submit">필터 적용</button>
    </form>
    """

    rows = []
    for m in page_items:
        key = label_store.make_key(m["account"], m["uid"])
        rows.append(
            f"<tr>"
            f"{msg_date_cell(m.get('message_date'))}"
            f'<td class="msg-cat"><span class="pill">{esc(m["_category"] or "미분류")}</span></td>'
            f'<td class="msg-subj"><span class="subj">{esc((m["subject"] or "")[:120])}</span></td>'
            f'<td class="msg-sender">{esc(m["sender"])}</td>'
            f"<td>{_label_select(key, categories, label_map.get(key))}"
            f'<input type="hidden" name="sender[{esc(key)}]" value="{esc(m["sender"])}">'
            f'<input type="hidden" name="subject[{esc(key)}]" value="{esc(m["subject"])}"></td>'
            f"</tr>"
        )
    if page_items:
        table = (
            '<div class="table-scroll"><table class="msg-table">'
            "<thead><tr><th>날짜</th><th>예측</th><th>제목</th><th>발신인</th><th>정답 카테고리</th></tr></thead>"
            f'<tbody>{"".join(rows)}</tbody></table></div>'
        )
    else:
        table = '<p class="empty">해당 조건의 메일이 없습니다.</p>'

    prev_qs = build_qs(**filter_state, page=page_num - 1)
    next_qs = build_qs(**filter_state, page=page_num + 1)
    prev_link = f'<a href="/label?{prev_qs}">← 이전</a>' if page_num > 1 else '<span class="disabled">← 이전</span>'
    next_link = f'<a href="/label?{next_qs}">다음 →</a>' if page_num < total_pages else '<span class="disabled">다음 →</span>'
    pagination = render_pagination(prev_link, next_link, page_num, total_pages, total)

    body = f"""
    <h1 class="page-title">🏷️ 수동 라벨링</h1>
    <p class="sub">각 메일의 "정답" 카테고리를 지정해 eval 하네스용 ground-truth 를 만듭니다.
    빈 값은 저장하지 않습니다. <code>__none__</code> 은 "어떤 카테고리에도 해당 없음".</p>
    {_label_status_html(run_state.get("last_label_action"))}
    {_summary_html(categories, predicted_counts, label_counts, label_store.count_labels(DB_PATH))}
    <div class="task-run-bar">
      <form method="post" action="/label/export" style="margin:0">
        <button class="btn secondary" type="submit">manual.jsonl 로 내보내기</button>
      </form>
    </div>
    {filter_form}
    <form method="post" action="/label">
      {table}
      <div class="actions-row"><button class="btn" type="submit">이 페이지 라벨 저장</button></div>
    </form>
    {pagination}
    """
    return page("수동 라벨링", body, "label")


@bp.route("/label", methods=["POST"])
def label_save():
    saved = 0
    for field, value in request.form.items():
        if not (field.startswith("label[") and field.endswith("]")):
            continue
        value = value.strip()
        if not value:
            continue
        key = field[len("label[") : -1]
        if not key.startswith("acct:"):
            continue
        account, _, uid = key[len("acct:") :].rpartition(":")
        if not account or not uid:
            continue
        label_store.set_label(
            DB_PATH,
            account,
            uid,
            request.form.get(f"sender[{key}]", ""),
            request.form.get(f"subject[{key}]", ""),
            value,
        )
        saved += 1
    run_state["last_label_action"] = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "msg": f"{saved}건 라벨 저장",
    }
    return redirect("/label")


@bp.route("/label/export", methods=["POST"])
def label_export():
    n = label_store.export_jsonl(DB_PATH, LABELS_JSONL_PATH)
    run_state["last_label_action"] = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "msg": f"{n}건을 {LABELS_JSONL_PATH} 로 내보냄",
    }
    return redirect("/label")

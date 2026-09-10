"""classify.py 매칭 규칙 회귀 테스트.

실행: packages/mail-core/ 에서  py -m pytest  (또는 repo 루트에서 경로 지정)
pytest 없이도 되도록 아래 main 가드로 assert 러너를 겸한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_core.classify import classify, needs_contents, _clean_domains  # noqa: E402


def _cats(**over):
    base = {
        "sec": {"priority": "HIGH", "action": "save", "keywords": {"senders": ["no-reply@accounts.google.com"]}},
        "ad": {"priority": "HIGH", "action": "trash", "keywords": {"title": ["(광고)"]}},
        "job": {"priority": "NORMAL", "action": "keep", "keywords": {"domains": ["greetinghr.com"]}},
    }
    base.update(over)
    return base


def test_exact_sender_match():
    r = classify([{"sender": "no-reply@accounts.google.com", "subject": "hi"}], _cats())
    assert r["categories"]["sec"]["count"] == 1


def test_domain_and_subdomain_match():
    msgs = [
        {"sender": "recruiting_wb@greetinghr.com", "subject": "지원"},
        {"sender": "x@mail.greetinghr.com", "subject": "지원"},
        {"sender": "x@notgreetinghr.com", "subject": "무관"},
    ]
    r = classify(msgs, _cats())
    assert r["categories"]["job"]["count"] == 2
    assert r["uncategorized_count"] == 1


def test_priority_order_wins():
    # 같은 메일이 sec(HIGH)와 job(NORMAL) 둘 다 걸려도 HIGH가 이긴다
    cats = _cats(job={"priority": "NORMAL", "action": "keep",
                      "keywords": {"domains": ["accounts.google.com"]}})
    r = classify([{"sender": "no-reply@accounts.google.com", "subject": "x"}], cats)
    assert r["categories"]["sec"]["count"] == 1
    assert r["categories"]["job"]["count"] == 0


def test_senders_beats_domains_within_pipeline_but_domains_beats_title():
    cats = {
        "d": {"priority": "NORMAL", "action": "keep", "keywords": {"domains": ["inflearn.com"]}},
        "t": {"priority": "NORMAL", "action": "keep", "keywords": {"title": ["새 강의"]}},
    }
    r = classify([{"sender": "notice@inflearn.com", "subject": "새 강의 오픈"}], cats)
    assert r["categories"]["d"]["count"] == 1
    assert r["categories"]["t"]["count"] == 0


def test_clean_domains_drops_public_suffix_and_bare_labels():
    assert _clean_domains(["co.kr", "noreply", "@saramin.co.kr", "GREETINGHR.COM", "kr"]) == [
        "saramin.co.kr", "greetinghr.com",
    ]


def test_needs_contents_true_only_when_a_category_uses_contents_keywords():
    assert needs_contents(_cats()) is False
    with_contents = _cats(biz={"priority": "LOW", "action": "keep",
                               "keywords": {"contents": ["invoice"]}})
    assert needs_contents(with_contents) is True
    # 빈 리스트는 "안 씀"으로 친다
    assert needs_contents(_cats(biz={"keywords": {"contents": []}})) is False


def test_contents_keyword_matches_only_when_body_present():
    cats = {"biz": {"priority": "NORMAL", "action": "keep", "keywords": {"contents": ["invoice"]}}}
    # 본문 없음 → 통과 안 함
    assert classify([{"sender": "x@y.com", "subject": "hi"}], cats)["uncategorized_count"] == 1
    # 본문 있음 → 매칭
    r = classify([{"sender": "x@y.com", "subject": "hi", "contents": "your INVOICE is ready"}], cats)
    assert r["categories"]["biz"]["count"] == 1


def test_malformed_category_keyword_null_does_not_crash():
    """손으로 편집한 DB 행에서 keywords 값이 null 로 들어와도(메일 0건이어도) 안 터진다."""
    cats = {"bad": {"priority": "NORMAL", "action": "keep",
                    "keywords": {"senders": None, "domains": None, "title": None, "contents": None}}}
    r = classify([], cats)
    assert r["total"] == 0
    r2 = classify([{"sender": "x@y.com", "subject": "hi"}], cats)
    assert r2["uncategorized_count"] == 1


def test_sample_cap_limits_sample_not_matches():
    cats = {"j": {"priority": "NORMAL", "action": "keep", "keywords": {"domains": ["x.com"]}}}
    msgs = [{"sender": f"{i}@x.com", "subject": "m"} for i in range(30)]
    msgs += [{"sender": f"u{i}@none.com", "subject": "m"} for i in range(30)]
    capped = classify(msgs, cats, sample_cap=5)
    assert capped["categories"]["j"]["count"] == 30
    assert len(capped["category_matches"]["j"]) == 30
    assert capped["uncategorized_count"] == 30
    assert len(capped["uncategorized_sample"]) == 5
    assert len(classify(msgs, cats, sample_cap=None)["uncategorized_sample"]) == 30


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"\n{len(fns)} passed")

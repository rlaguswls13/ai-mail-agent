"""classify.py 매칭 규칙 회귀 테스트.

실행: packages/mail-core/ 에서  py -m pytest  (또는 repo 루트에서 경로 지정)
pytest 없이도 되도록 아래 main 가드로 assert 러너를 겸한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_core.classify import classify, _clean_domains  # noqa: E402


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"\n{len(fns)} passed")

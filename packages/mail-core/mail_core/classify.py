"""카테고리 우선순위·키워드 순서에 따라 메일을 분류한다.

매칭 순서:
1. 카테고리를 priority(HIGH > NORMAL > LOW) 순으로 검사한다. 같은 priority끼리는
   categories.json에 적힌 순서를 따른다.
2. 카테고리 하나 안에서는 keywords.senders -> keywords.title -> keywords.contents
   순으로 검사하고, 하나라도 걸리면 그 카테고리로 확정한다.
3. 먼저 매칭되는 카테고리가 그 메일의 최종 분류다 (다른 카테고리도 걸릴 수 있어도 무시).

keywords.senders는 발신인 주소와 **완전히 동일한 문자열일 때만** 매칭된다(부분 문자열 X).
도메인이나 "noreply" 같은 일반 키워드로 senders를 채우면 무관한 발신인까지 광범위하게
잡히는 문제가 실제로 있었다(예: "noreply"가 보안/결제 알림 메일까지 전부 광고로 오분류)
— 그래서 senders는 정확한 전체 주소만 허용한다.

keywords.domains는 발신인 주소의 **도메인 부분**과 매칭된다. 항목이 "saramin.co.kr"이면
발신 도메인이 "saramin.co.kr"이거나 그 서브도메인("mailinfo.saramin.co.kr")일 때 걸린다.
한 발신 도메인 전체를 한 카테고리가 독점할 때(예: lguplus.co.kr, greetinghr.com) 주소를
한 줄씩 나열하는 대신 이걸 쓴다. 여러 카테고리가 나눠 쓰는 공용 도메인(google.com,
navercorp.com 등)에는 쓰면 안 되고 senders(정확한 주소)로 구분해야 한다. "co.kr"처럼
public suffix 단독이거나 점이 없는 단일 라벨 항목은 무시된다(너무 광범위).

넓게 잡고 싶으면 keywords.title(제목 키워드)을 쓴다. title/contents는 부분 문자열 매칭이다.

contents는 메일 본문이 필요해서 헤더만으로는 채워지지 않는다 — 이 함수는 message
dict에 "contents" 키가 없어도(또는 빈 문자열이어도) 그냥 그 조건만 통과시키지 않고
넘어간다. 본문을 가져와서 다시 분류하는 건 호출하는 쪽(fetch_mail.py)의 책임이다.
"""

PRIORITY_ORDER = {"HIGH": 0, "NORMAL": 1, "LOW": 2}

# domains 항목으로 쓰면 무관한 메일까지 통째로 잡히는 public suffix — 무시한다.
PUBLIC_SUFFIXES = {
    "co.kr", "or.kr", "ne.kr", "go.kr", "re.kr", "pe.kr", "ac.kr", "hs.kr",
    "ms.kr", "es.kr", "sc.kr", "kg.kr", "seoul.kr",
    "com", "net", "org", "io", "kr", "jp", "co.jp", "com.tw", "co.uk", "com.au",
}


def _domain_of(addr: str) -> str:
    return addr.rpartition("@")[2].strip().strip(">").lower()


def _clean_domains(raw: list[str]) -> list[str]:
    out = []
    for entry in raw:
        d = entry.strip().lower().lstrip("@").strip(".")
        if "." not in d or d in PUBLIC_SUFFIXES:
            continue
        out.append(d)
    return out


def _priority_rank(cfg: dict) -> int:
    return PRIORITY_ORDER.get(str(cfg.get("priority", "NORMAL")).upper(), 1)


def _matches(message: dict, keywords: dict) -> bool:
    sender_l = (message.get("sender") or "").lower()
    subject_l = (message.get("subject") or "").lower()
    contents_l = (message.get("contents") or "").lower()

    sender_kw = {k.lower() for k in keywords.get("senders", [])}
    if sender_l in sender_kw:
        return True

    domain_kw = _clean_domains(keywords.get("domains", []))
    if domain_kw:
        sender_domain = _domain_of(sender_l)
        if sender_domain and any(
            sender_domain == d or sender_domain.endswith("." + d) for d in domain_kw
        ):
            return True

    title_kw = [k.lower() for k in keywords.get("title", [])]
    if title_kw and any(kw in subject_l for kw in title_kw):
        return True

    contents_kw = [k.lower() for k in keywords.get("contents", [])]
    if contents_kw and contents_l and any(kw in contents_l for kw in contents_kw):
        return True

    return False


def needs_contents(categories: dict) -> bool:
    """어느 카테고리든 contents 키워드가 설정돼 있으면 True.

    이 프로젝트는 헤더만 가져오는 게 기본값이라(성능), contents 키워드를 하나도
    안 쓰면 본문 조회 자체를 아예 건너뛴다.
    """
    return any(cfg.get("keywords", {}).get("contents") for cfg in categories.values())


def classify(messages: list[dict], categories: dict, sample_cap: int | None = 20) -> dict:
    """sample_cap은 uncategorized_sample 리스트만 제한한다(category_matches는 항상 전체
    매치를 담는다) — 대시보드 요약 렌더링은 기본 20건 캡으로 충분하지만, 미분류 메일을
    전부 훑어봐야 하는 화면(관리 화면의 메일 목록 페이지 등)에서는 None을 넘겨 전체를
    받는다."""
    ordered_names = sorted(
        categories.keys(), key=lambda name: _priority_rank(categories[name])
    )

    result = {
        "total": len(messages),
        "categories": {
            name: {
                "count": 0,
                "action": categories[name].get("action", "keep"),
                "priority": str(categories[name].get("priority", "NORMAL")).upper(),
            }
            for name in ordered_names
        },
        "category_matches": {name: [] for name in ordered_names},
        "uncategorized_sample": [],
    }

    uncategorized_count = 0
    for m in messages:
        matched_name = None
        for name in ordered_names:
            if _matches(m, categories[name].get("keywords", {})):
                matched_name = name
                break

        if matched_name:
            result["categories"][matched_name]["count"] += 1
            result["category_matches"][matched_name].append(m)
        else:
            uncategorized_count += 1
            if sample_cap is None or len(result["uncategorized_sample"]) < sample_cap:
                result["uncategorized_sample"].append(m)

    result["uncategorized_count"] = uncategorized_count
    return result

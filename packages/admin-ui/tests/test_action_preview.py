"""`compute_action_preview` / 대시보드 캐러셀·통합 카드의 "자동 처리 예정" 수치가
- (순수 로직) action=keep 제외, read는 안읽음만, 그 외는 active 상태만 센다
- (회귀) 화면에 보이는 기간(daily/weekly/...)에 맞춰 계정별로 달라진다 -
  `mail_log_store.latest_action_summary()`(가장 최근 동기화 스냅샷 고정값)를 그대로
  써서 모든 탭이 같은 숫자를 보여주던 session 7 버그의 회귀 방지 테스트.
- (정합성) 통합 카드와 계정 카드가 같은 계정·같은 기간에 대해 완전히 같은 한 줄을
  보여준다.
를 검증한다. session 7 handoff TODO: "전체" 기간의 큰 절대값은 기간별 통계와의
정합성만 맞추면 된다(사용자 확인 완료, 2026-09-11) - 그 정합성 자체가 이 파일의 대상."""
from admin_ui.dashboard import (
    ACTION_PREVIEW_TERM,
    UNIFIED_ACCT_ID,
    compute_action_preview,
    render_action_summary_line,
)


# --- 순수 함수 단위 테스트 ---------------------------------------------------

def test_keep_action_is_excluded():
    per_account = {
        "a@x.com": {
            "categories": {"채용": {"action": "keep"}},
            "category_matches": {"채용": [{"status": "active"}, {"status": "active"}]},
        }
    }
    assert compute_action_preview(per_account) == {}


def test_read_action_counts_only_unread():
    per_account = {
        "a@x.com": {
            "categories": {"뉴스": {"action": "read"}},
            "category_matches": {
                "뉴스": [{"is_read": False}, {"is_read": True}, {"is_read": False}]
            },
        }
    }
    assert compute_action_preview(per_account) == {"a@x.com": {"read": {"candidates": 2}}}


def test_non_read_action_counts_only_active_status():
    per_account = {
        "a@x.com": {
            "categories": {"광고": {"action": "save"}},
            "category_matches": {
                "광고": [
                    {"status": "active"},
                    {"status": "archived"},
                    {},  # status 없음 -> active로 취급
                    {"status": "trashed"},
                ]
            },
        }
    }
    assert compute_action_preview(per_account) == {"a@x.com": {"save": {"candidates": 2}}}


def test_multiple_categories_same_action_sum_up():
    per_account = {
        "a@x.com": {
            "categories": {"광고": {"action": "save"}, "뉴스레터": {"action": "save"}},
            "category_matches": {
                "광고": [{"status": "active"}],
                "뉴스레터": [{"status": "active"}, {"status": "active"}],
            },
        }
    }
    assert compute_action_preview(per_account) == {"a@x.com": {"save": {"candidates": 3}}}


def test_account_with_no_pending_action_is_omitted():
    per_account = {
        "a@x.com": {
            "categories": {"광고": {"action": "save"}},
            "category_matches": {"광고": [{"status": "archived"}]},  # 전부 처리됨
        },
        "b@x.com": {"categories": {}, "category_matches": {}},
    }
    assert compute_action_preview(per_account) == {}


def test_render_action_summary_line_formats_and_skips_zero():
    line = render_action_summary_line("a@x.com", {"save": {"candidates": 3}, "trash": {"candidates": 0}})
    assert "a@x.com" in line
    assert ACTION_PREVIEW_TERM in line
    assert "보관 3" in line
    assert "휴지통 이동" not in line  # candidates=0 인 액션은 생략


def test_render_action_summary_line_empty_when_no_actions():
    assert render_action_summary_line("a@x.com", {}) == ""


# --- 라우트 레벨 회귀 테스트 --------------------------------------------------
# sample_data: a@gmail.com의 uid1(오늘, 세일 안내, news@shop.com)과 b@naver.com의
# uid10(4일 전, 주간 뉴스레터, news@shop.com) 모두 "광고" 카테고리(action=save,
# domain shop.com)에 걸리고 둘 다 active 상태 - "daily" 탭은 오늘치인 uid1만,
# "all" 탭(EPOCH_START 기준 진짜 전체)은 둘 다 포함해야 한다.

def test_action_preview_is_filtered_by_visible_period_not_global_backlog(client, sample_data):
    daily_html = client.get(f"/?range=daily&acct={UNIFIED_ACCT_ID}").get_data(as_text=True)
    all_html = client.get(f"/?range=all&acct={UNIFIED_ACCT_ID}").get_data(as_text=True)

    assert "a@gmail.com · 자동 처리 예정" in daily_html
    assert "b@naver.com · 자동 처리 예정" not in daily_html  # 4일 전 메일은 daily 탭에 없어야 함

    assert "a@gmail.com · 자동 처리 예정" in all_html
    assert "b@naver.com · 자동 처리 예정" in all_html  # "전체"는 진짜 전체 기간이라 포함


def test_unified_card_and_account_card_agree_on_same_period(client, sample_data):
    """같은 기간(all)에 대해 통합 카드와 계정 카드가 같은 계정에 대해 같은 수치를
    보여줘야 한다(액션 프리뷰 포맷 통일 - render_action_summary_line 공유)."""
    unified_html = client.get(f"/?range=all&acct={UNIFIED_ACCT_ID}").get_data(as_text=True)
    account_html = client.get("/?range=all&acct=a@gmail.com").get_data(as_text=True)

    assert "a@gmail.com · 자동 처리 예정: 보관 1" in unified_html
    # 계정 카드는 자기 계정 몫만 보여주므로 접두사 없이 같은 서식.
    assert "자동 처리 예정: 보관 1" in account_html

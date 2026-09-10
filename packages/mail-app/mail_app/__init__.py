"""mail_app - ai-mail-agent 앱 레이어.

data/app.db(SQLite) 영속(categories / messages / action_runs), 파이프라인 오케스트레이션
(fetch_mail), 대시보드 리포트 생성(generate_html), 공유 CSS(web_style), 데이터/설정
경로 해석(app_paths). mail_core 에 의존한다.
"""
__version__ = "0.2.0"

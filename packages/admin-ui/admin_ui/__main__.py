"""`python -m admin_ui` 진입점."""
from admin_ui._entrypoint import resolve_accounts_from_stdin

resolve_accounts_from_stdin()

from admin_ui.admin_app import main  # noqa: E402

if __name__ == "__main__":
    main()

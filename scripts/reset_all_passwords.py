"""
One-off script: reset the password of EVERY user in the database to a
fixed value.

Uses werkzeug PBKDF2-SHA256 hashing (same format as app/auth.py:hash_password)
so the existing verify_password() path picks the new hash up without needing
the legacy-upgrade branch.

Usage:
  # Local (will hit whatever DATABASE_URL / DB_* config.py resolves to):
  DISABLE_SYNC=true python scripts/reset_all_passwords.py

  # Railway:
  railway run python scripts/reset_all_passwords.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.auth import hash_password
from app.database import get_conn

NEW_PASSWORD = "qwerty@8"


def main() -> int:
    new_hash = hash_password(NEW_PASSWORD)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            before = cur.fetchone()[0]

            cur.execute(
                "UPDATE users SET password_hash = %s, failed_logins = 0, "
                "locked_until = NULL, updated_at = NOW()",
                (new_hash,),
            )
            affected = cur.rowcount
        conn.commit()

    print(f"users in table: {before}")
    print(f"password_hash rows updated: {affected}")
    print(f"new password (plaintext): {NEW_PASSWORD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

"""
Weekly progress email sender.

Run this from cron/Task Scheduler (e.g. hourly). It will send to users who:
- have weekly_email_enabled = 1
- have email_enabled = 1
- have a valid email
- match the configured weekly day/hour (server local time)

Example (PowerShell):
  python scripts/send_weekly_emails.py
"""

from datetime import datetime, timedelta
import sqlite3

from db import init_db, get_connection
from webapp.services.emailer import send_achievement_email


def _weekly_summary(conn: sqlite3.Connection, user_id: int) -> dict:
    cur = conn.cursor()
    since = datetime.now() - timedelta(days=7)
    cur.execute(
        """
        SELECT COUNT(*),
               SUM(is_correct),
               SUM(quality)
        FROM card_attempts
        WHERE user_id = ? AND timestamp >= ?
        """,
        (user_id, since.isoformat(timespec="seconds")),
    )
    attempts, correct, quality_sum = cur.fetchone()
    attempts = int(attempts or 0)
    correct = int(correct or 0)
    quality_sum = int(quality_sum or 0)
    acc = (correct / attempts) if attempts else 0.0
    avg_quality = (quality_sum / attempts) if attempts else 0.0

    # XP gained last 7 days (from daily_progress)
    day_since = (datetime.now() - timedelta(days=7)).date().isoformat()
    cur.execute("SELECT SUM(xp), SUM(attempts) FROM daily_progress WHERE user_id = ? AND day >= ?", (user_id, day_since))
    xp_week, attempts_week = cur.fetchone()

    # Current streak
    cur.execute("SELECT current_streak, best_streak, xp, level FROM user_stats WHERE user_id = ?", (user_id,))
    row = cur.fetchone() or (0, 0, 0, 1)
    current_streak, best_streak, xp_total, level = row

    return {
        "attempts": attempts,
        "accuracy": acc,
        "avg_quality": avg_quality,
        "xp_week": int(xp_week or 0),
        "attempts_week": int(attempts_week or 0),
        "streak": int(current_streak or 0),
        "best_streak": int(best_streak or 0),
        "xp_total": int(xp_total or 0),
        "level": int(level or 1),
    }


def main() -> None:
    init_db()
    now = datetime.now()
    weekday = (now.weekday())  # 0=Mon
    hour = now.hour

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT u.id, u.username, u.email
        FROM users u
        JOIN user_prefs p ON p.user_id = u.id
        WHERE p.email_enabled = 1
          AND p.weekly_email_enabled = 1
          AND p.weekly_email_day = ?
          AND p.weekly_email_hour = ?
          AND u.email IS NOT NULL
          AND TRIM(u.email) <> ''
        """,
        (weekday, hour),
    )

    users = cur.fetchall()
    for user_id, username, email in users:
        summary = _weekly_summary(conn, int(user_id))
        # Reuse the existing emailer with a "fake achievement list" payload for now
        # to avoid adding another email template system.
        achievements = [
            {"code": "weekly", "icon": "📈", "name": "Your weekly progress", "description": f"{summary['attempts_week']} attempts · {summary['xp_week']} XP"},
            {"code": "streak", "icon": "🔥", "name": "Streak", "description": f"{summary['streak']} days (best {summary['best_streak']})"},
            {"code": "level", "icon": "⭐", "name": "Level", "description": f"Level {summary['level']} · {summary['xp_total']} XP total"},
        ]
        send_achievement_email(to_email=str(email), username=str(username), achievements=achievements)

    conn.close()


if __name__ == "__main__":
    main()


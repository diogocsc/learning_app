from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import List, Dict


def _smtp_settings():
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    from_addr = os.getenv("SMTP_FROM", user or "")
    return host, port, user, password, from_addr


def send_achievement_email(*, to_email: str, username: str, achievements: List[Dict[str, str]]) -> None:
    """
    Send an email about newly earned achievements.

    For Gmail you should use an App Password (SMTP_PASSWORD).
    """
    host, port, user, password, from_addr = _smtp_settings()
    if not to_email or not host or not port or not user or not password or not from_addr:
        return

    subject = "New achievements unlocked"
    lines = [f"Hi {username},", "", "You unlocked:"]
    for a in achievements:
        icon = a.get("icon", "")
        name = a.get("name", a.get("code", ""))
        desc = a.get("description", "")
        lines.append(f"- {icon} {name} — {desc}".strip())
    lines += ["", "Keep going!", "AI Learning Assistant"]

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content("\n".join(lines))

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
        smtp.login(user, password)
        smtp.send_message(msg)


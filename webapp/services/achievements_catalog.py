from __future__ import annotations

# Simple catalog used for UI rendering.
# Codes must match those awarded in `db.py`.

ACHIEVEMENTS = [
    {
        "code": "first_10_attempts",
        "scope": "user",
        "name": "Warm start",
        "description": "Complete 10 attempts.",
        "icon": "⭐",
    },
    {
        "code": "streak_3",
        "scope": "user",
        "name": "3‑day streak",
        "description": "Hit your daily goal 3 days in a row.",
        "icon": "🔥",
    },
    {
        "code": "streak_7",
        "scope": "user",
        "name": "7‑day streak",
        "description": "Hit your daily goal 7 days in a row.",
        "icon": "🔥",
    },
    {
        "code": "streak_30",
        "scope": "user",
        "name": "30‑day streak",
        "description": "Hit your daily goal 30 days in a row.",
        "icon": "🏆",
    },
    {
        "code": "day_accuracy_90_20",
        "scope": "user",
        "name": "Precision day",
        "description": "≥90% accuracy over 20 attempts in a day.",
        "icon": "🎯",
    },
    {
        "code": "subject_50_xp",
        "scope": "subject",
        "name": "Getting traction",
        "description": "Earn 50 XP in this subject.",
        "icon": "📚",
    },
    {
        "code": "subject_mastery_60",
        "scope": "subject",
        "name": "Solid understanding",
        "description": "Reach 60% mastery in this subject.",
        "icon": "🧠",
    },
    {
        "code": "subject_mastery_85",
        "scope": "subject",
        "name": "Near‑mastery",
        "description": "Reach 85% mastery in this subject.",
        "icon": "🧠",
    },
]


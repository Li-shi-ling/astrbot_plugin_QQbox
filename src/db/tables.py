CREATE_QQ_PROFILE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS qq_profile (
    qq TEXT PRIMARY KEY,
    nickname TEXT,
    color INTEGER,
    content TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_QQ_PROFILE_UPDATED_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_qq_profile_updated_at
ON qq_profile(updated_at)
"""

SELECT_ALL_QQ_PROFILES_SQL = """
SELECT qq, nickname, color, content, notes
FROM qq_profile
"""

UPSERT_QQ_PROFILE_SQL = """
INSERT INTO qq_profile (qq, nickname, color, content, notes)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(qq) DO UPDATE SET
    nickname = excluded.nickname,
    color = excluded.color,
    content = excluded.content,
    notes = excluded.notes,
    updated_at = CURRENT_TIMESTAMP
"""

INSERT_MISSING_QQ_PROFILE_SQL = """
INSERT OR IGNORE INTO qq_profile (qq, nickname, color, content, notes)
VALUES (?, ?, ?, ?, ?)
"""

PROFILE_FIELDS = ("nickname", "color", "content", "notes")

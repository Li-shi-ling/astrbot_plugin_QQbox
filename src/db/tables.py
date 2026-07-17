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

CREATE_LAYOUT_PRESET_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS layout_preset (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_LAYOUT_PRESET_ACTIVE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_layout_preset_active
ON layout_preset(is_active)
WHERE is_active = 1
"""

SELECT_ALL_QQ_PROFILES_SQL = """
SELECT qq, nickname, color, content, notes
FROM qq_profile
"""

REPLACE_ALL_QQ_PROFILES_SQL = """
DELETE FROM qq_profile
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

SELECT_LAYOUT_PRESETS_SQL = """
SELECT id, name, config_json, is_active, created_at, updated_at
FROM layout_preset
ORDER BY is_active DESC, updated_at DESC, id DESC
"""

SELECT_LAYOUT_PRESET_SQL = """
SELECT id, name, config_json, is_active, created_at, updated_at
FROM layout_preset
WHERE id = ?
"""

SELECT_ACTIVE_LAYOUT_PRESET_SQL = """
SELECT id, name, config_json, is_active, created_at, updated_at
FROM layout_preset
WHERE is_active = 1
LIMIT 1
"""

INSERT_LAYOUT_PRESET_SQL = """
INSERT INTO layout_preset (name, config_json)
VALUES (?, ?)
"""

UPDATE_LAYOUT_PRESET_SQL = """
UPDATE layout_preset
SET name = ?, config_json = ?, updated_at = CURRENT_TIMESTAMP
WHERE id = ?
"""

DELETE_LAYOUT_PRESET_SQL = """
DELETE FROM layout_preset WHERE id = ?
"""

DEACTIVATE_LAYOUT_PRESETS_SQL = """
UPDATE layout_preset SET is_active = 0 WHERE is_active = 1
"""

ACTIVATE_LAYOUT_PRESET_SQL = """
UPDATE layout_preset
SET is_active = 1, updated_at = CURRENT_TIMESTAMP
WHERE id = ?
"""

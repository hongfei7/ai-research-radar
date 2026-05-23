"""SQLite 指纹库 —— URL 去重"""

import sqlite3
import logging
from pathlib import Path
from typing import Optional

from radar.models import utcnow_iso

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "state" / "seen.db"


class DedupStore:
    """管理 seen 指纹库，提供去重判断"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._init_table()
        return self._conn

    def _init_table(self) -> None:
        conn = self._conn
        if conn is None:
            return
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen (
                id TEXT PRIMARY KEY,
                title TEXT,
                first_seen_at TEXT NOT NULL
            )
        """)
        conn.commit()

    def is_seen(self, item_id: str) -> bool:
        """检查 id 是否已存在"""
        conn = self._get_conn()
        row = conn.execute("SELECT 1 FROM seen WHERE id = ?", (item_id,)).fetchone()
        return row is not None

    def filter_new(self, item_ids: list[str]) -> list[str]:
        """从 id 列表中筛选出未见过的新 id"""
        if not item_ids:
            return []
        conn = self._get_conn()
        seen_set: set[str] = set()
        _BATCH = 900  # SQLite 变量数限制默认 999
        for i in range(0, len(item_ids), _BATCH):
            batch = item_ids[i : i + _BATCH]
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                f"SELECT id FROM seen WHERE id IN ({placeholders})", batch
            ).fetchall()
            seen_set.update(r[0] for r in rows)
        return [iid for iid in item_ids if iid not in seen_set]

    def mark_seen(self, item_id: str, title: str = "") -> None:
        """标记条目为已处理"""
        conn = self._get_conn()
        now = utcnow_iso()
        conn.execute(
            "INSERT OR IGNORE INTO seen (id, title, first_seen_at) VALUES (?, ?, ?)",
            (item_id, title, now),
        )
        conn.commit()

    def mark_seen_batch(self, items: list[tuple[str, str]]) -> None:
        """批量标记 (id, title) 列表"""
        if not items:
            return
        conn = self._get_conn()
        now = utcnow_iso()
        conn.executemany(
            "INSERT OR IGNORE INTO seen (id, title, first_seen_at) VALUES (?, ?, ?)",
            [(iid, title, now) for iid, title in items],
        )
        conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

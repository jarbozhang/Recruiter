import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# 合法的状态转换
VALID_STATUS_TRANSITIONS = {
    "pending": ["approved"],
    "approved": ["sending"],
    "sending": ["sent", "failed", "timeout"],
    "failed": ["approved"],  # 可重新进入审核队列重试
    "timeout": [],  # 需要人工确认，不自动转换
    "sent": ["replied"],
    "replied": [],
}


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self):
        schema_sql = SCHEMA_PATH.read_text()
        self.conn.executescript(schema_sql)

    def close(self):
        self.conn.close()

    # -- Jobs --

    def create_job(self, title: str, jd: str, platform: str = "boss",
                   match_threshold: int = 60) -> int:
        cur = self.conn.execute(
            "INSERT INTO jobs (title, jd, platform, match_threshold) VALUES (?, ?, ?, ?)",
            (title, jd, platform, match_threshold),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_job(self, job_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, status: str = "active") -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
        return [dict(r) for r in rows]

    def update_job_status(self, job_id: int, status: str):
        self.conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
            (status, datetime.now().isoformat(), job_id),
        )
        self.conn.commit()

    # -- Candidates --

    def upsert_candidate(self, platform: str, platform_id: str, name: str = None,
                         resume_text: str = None, source: str = "inbound") -> int | None:
        """INSERT OR IGNORE，返回候选人 ID。如果已存在返回已有记录的 ID。"""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO candidates (platform, platform_id, name, resume_text, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (platform, platform_id, name, resume_text, source),
        )
        self.conn.commit()
        if cur.lastrowid and cur.rowcount > 0:
            return cur.lastrowid
        # 已存在，查询现有 ID
        row = self.conn.execute(
            "SELECT id FROM candidates WHERE platform = ? AND platform_id = ?",
            (platform, platform_id),
        ).fetchone()
        return row["id"] if row else None

    def update_candidate_resume(self, candidate_id: int, resume_text: str):
        """更新候选人简历内容。"""
        self.conn.execute(
            "UPDATE candidates SET resume_text = ? WHERE id = ?",
            (resume_text, candidate_id),
        )
        self.conn.commit()

    def get_candidate(self, candidate_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_candidates(self, platform: str = None, source: str = None,
                        limit: int = 100) -> list[dict]:
        query = "SELECT * FROM candidates WHERE 1=1"
        params = []
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        if source:
            query += " AND source = ?"
            params.append(source)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # -- Match Results --

    def create_match_result(self, job_id: int, candidate_id: int, score: int,
                            reason: str = None, dimensions: dict = None,
                            prompt_version: str = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO match_results (job_id, candidate_id, score, reason, dimensions, prompt_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, candidate_id, score, reason,
             json.dumps(dimensions) if dimensions else None, prompt_version),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_match_results(self, job_id: int = None, candidate_id: int = None,
                          min_score: int = None) -> list[dict]:
        query = "SELECT * FROM match_results WHERE 1=1"
        params = []
        if job_id:
            query += " AND job_id = ?"
            params.append(job_id)
        if candidate_id:
            query += " AND candidate_id = ?"
            params.append(candidate_id)
        if min_score is not None:
            query += " AND score >= ?"
            params.append(min_score)
        query += " ORDER BY score DESC"
        rows = self.conn.execute(query, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            if d.get("dimensions"):
                d["dimensions"] = json.loads(d["dimensions"])
            results.append(d)
        return results

    # -- Conversations --

    def create_conversation(self, candidate_id: int, job_id: int, message: str,
                            direction: str = "sent", status: str = "pending") -> int:
        cur = self.conn.execute(
            "INSERT INTO conversations (candidate_id, job_id, message, direction, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (candidate_id, job_id, message, direction, status),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_conversation_status(self, conv_id: int, new_status: str) -> bool:
        """更新会话状态，校验状态转换合法性。返回是否更新成功。"""
        row = self.conn.execute(
            "SELECT status FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
        if not row:
            return False

        current_status = row["status"]
        allowed = VALID_STATUS_TRANSITIONS.get(current_status, [])
        if new_status not in allowed:
            return False

        self.conn.execute(
            "UPDATE conversations SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, datetime.now().isoformat(), conv_id),
        )
        self.conn.commit()
        return True

    def update_conversation_intent(self, conv_id: int, intent: str):
        self.conn.execute(
            "UPDATE conversations SET intent = ?, updated_at = ? WHERE id = ?",
            (intent, datetime.now().isoformat(), conv_id),
        )
        self.conn.commit()

    def list_conversations(self, status: str = None, candidate_id: int = None,
                           limit: int = 100) -> list[dict]:
        query = "SELECT * FROM conversations WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if candidate_id:
            query += " AND candidate_id = ?"
            params.append(candidate_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_conversation(self, conv_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
        return dict(row) if row else None

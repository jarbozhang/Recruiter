"""AI Recruiter Agent - 入口点"""
from recruiter.config import DB_PATH
from recruiter.db import Database


def get_db() -> Database:
    return Database(DB_PATH)


if __name__ == "__main__":
    db = get_db()
    print(f"Database initialized at {DB_PATH}")
    print(f"Jobs: {len(db.list_jobs())}")
    print(f"Candidates: {len(db.list_candidates())}")
    db.close()

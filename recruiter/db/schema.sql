-- AI Recruiter Agent - Database Schema

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    jd TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'boss',
    match_threshold INTEGER NOT NULL DEFAULT 60,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'closed')),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_id TEXT,
    name TEXT,
    resume_text TEXT,
    source TEXT NOT NULL CHECK (source IN ('inbound', 'outbound')),
    resume_downloaded INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, platform_id)
);

CREATE TABLE IF NOT EXISTS match_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    score INTEGER NOT NULL CHECK (score >= -1 AND score <= 100),
    reason TEXT,
    dimensions TEXT,  -- JSON: {"tech_stack": 80, "years": 60, ...}
    prompt_version TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    message TEXT,
    direction TEXT NOT NULL CHECK (direction IN ('sent', 'received')),
    intent TEXT CHECK (intent IN ('high', 'medium', 'low', 'rejected', NULL)),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'sending', 'sent', 'failed', 'timeout', 'replied')),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_candidates_platform ON candidates(platform, platform_id);
CREATE INDEX IF NOT EXISTS idx_match_results_job ON match_results(job_id);
CREATE INDEX IF NOT EXISTS idx_match_results_candidate ON match_results(candidate_id);
CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(status);
CREATE INDEX IF NOT EXISTS idx_conversations_candidate ON conversations(candidate_id);

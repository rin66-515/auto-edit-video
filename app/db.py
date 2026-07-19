import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from .config import DATA

DB_PATH = DATA / "vlog.db"
LOCK = threading.RLock()

def now(): return datetime.now(timezone.utc).isoformat()

@contextmanager
def connection():
    with LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally: conn.close()

def init_db():
    with connection() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL, source_dir TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', mode TEXT NOT NULL DEFAULT 'existing', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, upload_confirmed_at TEXT, notes TEXT NOT NULL DEFAULT '', settings TEXT NOT NULL DEFAULT '{}', error TEXT);
        CREATE TABLE IF NOT EXISTS assets (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, path TEXT UNIQUE NOT NULL, filename TEXT NOT NULL, bytes INTEGER NOT NULL, duration REAL, width INTEGER, height INTEGER, fps REAL, codec TEXT, proxy_path TEXT, audio_path TEXT, thumbnail_path TEXT, analysis TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id));
        CREATE TABLE IF NOT EXISTS revisions (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, resolved_at TEXT, FOREIGN KEY(project_id) REFERENCES projects(id));
        CREATE TABLE IF NOT EXISTS exports (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, version TEXT NOT NULL, format TEXT NOT NULL, path TEXT, status TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, approved_at TEXT, FOREIGN KEY(project_id) REFERENCES projects(id));
        CREATE TABLE IF NOT EXISTS platform_uploads (project_id INTEGER NOT NULL, platform TEXT NOT NULL, completed_at TEXT, PRIMARY KEY(project_id, platform));
        """)

def rows(sql, params=()):
    with connection() as c: return [dict(r) for r in c.execute(sql, params).fetchall()]
def row(sql, params=()):
    with connection() as c:
        r=c.execute(sql,params).fetchone(); return dict(r) if r else None
def execute(sql, params=()):
    with connection() as c: return c.execute(sql,params).lastrowid
def project_detail(project_id):
    project=row("SELECT * FROM projects WHERE id=?",(project_id,))
    if not project: return None
    project["settings"]=json.loads(project.get("settings") or "{}")
    project["assets"]=rows("SELECT * FROM assets WHERE project_id=? ORDER BY id",(project_id,))
    project["revisions"]=rows("SELECT * FROM revisions WHERE project_id=? ORDER BY id DESC",(project_id,))
    project["exports"]=rows("SELECT * FROM exports WHERE project_id=? ORDER BY id DESC",(project_id,))
    project["uploads"]=rows("SELECT * FROM platform_uploads WHERE project_id=? ORDER BY platform",(project_id,))
    return project


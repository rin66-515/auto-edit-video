import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from .config import DATA

DB_PATH = DATA / "vlog.db"
LOCK = threading.RLock()

RESUME_STATUS = {
    "waiting_start": "ready_for_audio",
    "ready_for_audio": "ready_for_audio",
    "audio_cleaning": "ready_for_audio",
    "audio_failed": "ready_for_audio",
    "ready_for_ai": "ready_for_ai",
    "transcribing": "ready_for_ai",
    "asr_failed": "ready_for_ai",
    "ready_for_visual": "ready_for_visual",
    "visual_analyzing": "ready_for_visual",
    "visual_failed": "ready_for_visual",
    "draft_ready": None,
    "revision_requested": "revision_requested",
    "revision_planning": "revision_requested",
    "render_requested": "render_requested",
    "rendering": "render_requested",
    "render_failed": "render_requested",
    "subtitle_render_requested": "subtitle_render_requested",
    "subtitle_rendering": "subtitle_render_requested",
    "subtitle_render_failed": "subtitle_render_requested",
}

ACTIVE_STATUSES = {"audio_cleaning", "transcribing", "visual_analyzing", "revision_planning", "rendering", "subtitle_rendering"}
WORKERS = {"audio":"音频清理", "asr":"语音识别", "visual":"画面分析与双语字幕"}
RECOVERY_STATUSES = {"app":{"rendering","subtitle_rendering"},"audio":{"audio_cleaning"},"asr":{"transcribing"},"visual":{"visual_analyzing","revision_planning"}}

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
        CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL, source_dir TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', mode TEXT NOT NULL DEFAULT 'existing', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, upload_confirmed_at TEXT, raw_deleted_at TEXT, intermediates_deleted_at TEXT, notes TEXT NOT NULL DEFAULT '', settings TEXT NOT NULL DEFAULT '{}', error TEXT);
        CREATE TABLE IF NOT EXISTS assets (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, path TEXT UNIQUE NOT NULL, filename TEXT NOT NULL, bytes INTEGER NOT NULL, duration REAL, width INTEGER, height INTEGER, fps REAL, codec TEXT, proxy_path TEXT, audio_path TEXT, thumbnail_path TEXT, analysis TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id));
        CREATE TABLE IF NOT EXISTS revisions (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', source_export_id INTEGER, source_version TEXT, applied_export_id INTEGER, applied_version TEXT, created_at TEXT NOT NULL, resolved_at TEXT, FOREIGN KEY(project_id) REFERENCES projects(id));
        CREATE TABLE IF NOT EXISTS exports (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, version TEXT NOT NULL, format TEXT NOT NULL, path TEXT, status TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 0, timeline_snapshot TEXT, caption_overrides TEXT NOT NULL DEFAULT '{}', render_options TEXT NOT NULL DEFAULT '{}', source_export_id INTEGER, master_manifest TEXT NOT NULL DEFAULT '{}', caption_revision INTEGER NOT NULL DEFAULT 0, render_mode TEXT NOT NULL DEFAULT 'full', caption_locked_at TEXT, created_at TEXT NOT NULL, approved_at TEXT, FOREIGN KEY(project_id) REFERENCES projects(id), FOREIGN KEY(source_export_id) REFERENCES exports(id));
        CREATE TABLE IF NOT EXISTS platform_uploads (project_id INTEGER NOT NULL, platform TEXT NOT NULL, completed_at TEXT, PRIMARY KEY(project_id, platform));
        CREATE TABLE IF NOT EXISTS project_control (project_id INTEGER PRIMARY KEY, desired_state TEXT NOT NULL DEFAULT 'stopped', resume_status TEXT, stage TEXT, item TEXT, render_scope TEXT, updated_at TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id));
        CREATE TABLE IF NOT EXISTS project_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, level TEXT NOT NULL, stage TEXT NOT NULL, event TEXT NOT NULL, message TEXT NOT NULL, details TEXT, created_at TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id));
        CREATE TABLE IF NOT EXISTS worker_heartbeats (worker TEXT PRIMARY KEY, stage TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_project_logs_project_id ON project_logs(project_id,id DESC);
        """)
        columns={value["name"] for value in c.execute("PRAGMA table_info(projects)").fetchall()}
        if "raw_deleted_at" not in columns:c.execute("ALTER TABLE projects ADD COLUMN raw_deleted_at TEXT")
        if "intermediates_deleted_at" not in columns:c.execute("ALTER TABLE projects ADD COLUMN intermediates_deleted_at TEXT")
        export_columns={value["name"] for value in c.execute("PRAGMA table_info(exports)").fetchall()}
        if "timeline_snapshot" not in export_columns:c.execute("ALTER TABLE exports ADD COLUMN timeline_snapshot TEXT")
        if "caption_overrides" not in export_columns:c.execute("ALTER TABLE exports ADD COLUMN caption_overrides TEXT NOT NULL DEFAULT '{}'")
        if "render_options" not in export_columns:c.execute("ALTER TABLE exports ADD COLUMN render_options TEXT NOT NULL DEFAULT '{}'")
        if "source_export_id" not in export_columns:c.execute("ALTER TABLE exports ADD COLUMN source_export_id INTEGER")
        if "master_manifest" not in export_columns:c.execute("ALTER TABLE exports ADD COLUMN master_manifest TEXT NOT NULL DEFAULT '{}'")
        if "caption_revision" not in export_columns:c.execute("ALTER TABLE exports ADD COLUMN caption_revision INTEGER NOT NULL DEFAULT 0")
        if "render_mode" not in export_columns:c.execute("ALTER TABLE exports ADD COLUMN render_mode TEXT NOT NULL DEFAULT 'full'")
        if "caption_locked_at" not in export_columns:c.execute("ALTER TABLE exports ADD COLUMN caption_locked_at TEXT")
        revision_columns={value["name"] for value in c.execute("PRAGMA table_info(revisions)").fetchall()}
        if "source_export_id" not in revision_columns:c.execute("ALTER TABLE revisions ADD COLUMN source_export_id INTEGER")
        if "source_version" not in revision_columns:c.execute("ALTER TABLE revisions ADD COLUMN source_version TEXT")
        if "applied_export_id" not in revision_columns:c.execute("ALTER TABLE revisions ADD COLUMN applied_export_id INTEGER")
        if "applied_version" not in revision_columns:c.execute("ALTER TABLE revisions ADD COLUMN applied_version TEXT")
        control_columns={value["name"] for value in c.execute("PRAGMA table_info(project_control)").fetchall()}
        if "render_scope" not in control_columns:c.execute("ALTER TABLE project_control ADD COLUMN render_scope TEXT")
        stamp=now()
        for project in c.execute("SELECT id,status FROM projects WHERE id NOT IN (SELECT project_id FROM project_control)").fetchall():
            status=project["status"]
            c.execute(
                "INSERT INTO project_control(project_id,desired_state,resume_status,stage,item,updated_at) VALUES(?,?,?,?,?,?)",
                (project["id"],"stopped",resume_status_for(status),stage_for(status),None,stamp),
            )
        c.execute("""UPDATE project_control
                     SET desired_state='stopped',resume_status=NULL,stage='人工审核',item=COALESCE(item,'等待人工审核'),updated_at=?
                     WHERE desired_state='running' AND project_id IN (SELECT id FROM projects WHERE status IN ('caption_review_ready','review_ready','approved','published'))""",(stamp,))
        for project in c.execute("SELECT id,settings FROM projects").fetchall():
            try:plan=json.loads(project["settings"] or "{}").get("story_plan") or {}
            except json.JSONDecodeError:continue
            for fmt in ("long_16x9","short_9x16"):
                latest=c.execute("SELECT id FROM exports WHERE project_id=? AND format=? AND timeline_snapshot IS NULL ORDER BY id DESC LIMIT 1",(project["id"],fmt)).fetchone()
                if not latest:continue
                snapshot={"long":plan.get("long",{}).get("timeline",[])} if fmt=="long_16x9" else {f"short-{index+1}":value.get("timeline",[]) for index,value in enumerate(plan.get("shorts") or []) if isinstance(value,dict)}
                if any(snapshot.values()):c.execute("UPDATE exports SET timeline_snapshot=? WHERE id=?",(json.dumps(snapshot,ensure_ascii=False),latest["id"]))

def rows(sql, params=()):
    with connection() as c: return [dict(r) for r in c.execute(sql, params).fetchall()]
def row(sql, params=()):
    with connection() as c:
        r=c.execute(sql,params).fetchone(); return dict(r) if r else None
def execute(sql, params=()):
    with connection() as c: return c.execute(sql,params).lastrowid

def worker_heartbeat(worker,stage=None):
    if worker not in WORKERS:raise ValueError(f"未知工作器：{worker}")
    execute(
        "INSERT INTO worker_heartbeats(worker,stage,updated_at) VALUES(?,?,?) ON CONFLICT(worker) DO UPDATE SET stage=excluded.stage,updated_at=excluded.updated_at",
        (worker,stage or WORKERS[worker],now()),
    )

def start_worker_heartbeat(worker,stage=None,interval=10):
    worker_heartbeat(worker,stage)
    def loop():
        while True:
            time.sleep(interval)
            try:worker_heartbeat(worker,stage)
            except Exception:pass
    threading.Thread(target=loop,name=f"{worker}-heartbeat",daemon=True).start()

def worker_statuses(max_age_seconds=45):
    current=datetime.now(timezone.utc);stored={value["worker"]:value for value in rows("SELECT * FROM worker_heartbeats")};result=[]
    for worker,stage in WORKERS.items():
        value=stored.get(worker);last_seen=value.get("updated_at") if value else None;online=False
        if last_seen:
            try:online=(current-datetime.fromisoformat(last_seen)).total_seconds()<=max_age_seconds
            except ValueError:pass
        result.append({"worker":worker,"stage":value.get("stage",stage) if value else stage,"online":online,"last_seen":last_seen})
    return result

def worker_online(worker,max_age_seconds=45):
    return any(value["worker"]==worker and value["online"] for value in worker_statuses(max_age_seconds))

def required_worker_for_status(status):
    if status in {"waiting_start","ready_for_audio","audio_cleaning","audio_failed"}:return "audio"
    if status in {"ready_for_ai","transcribing","asr_failed"}:return "asr"
    if status in {"ready_for_visual","visual_analyzing","visual_failed","revision_requested","revision_planning"}:return "visual"
    return None

def resume_status_for(status): return RESUME_STATUS.get(status,status if status and status.startswith("ready_for_") else None)

def stage_for(status):
    if status in {"waiting_start","preprocessing"}:return "素材导入"
    if status in {"ready_for_audio","audio_cleaning","audio_failed"}:return "音频清理"
    if status in {"ready_for_ai","transcribing","asr_failed"}:return "语音转写"
    if status in {"ready_for_visual","visual_analyzing","visual_failed"}:return "画面分析与双语字幕"
    if status=="draft_ready":return "等待选择版本"
    if status in {"revision_requested","revision_planning"}:return "修改剪辑方案"
    if status in {"render_requested","rendering","render_failed"}:return "成片渲染"
    if status in {"caption_review_ready"}:return "成片字幕校对"
    if status in {"subtitle_render_requested","subtitle_rendering","subtitle_render_failed"}:return "字幕快速生成"
    if status in {"review_ready","approved"}:return "人工审核"
    if status=="published":return "已发布"
    return status or "等待"

_KEEP_RENDER_SCOPE=object()

def create_control(project_id,desired_state="stopped",resume_status=None,stage=None,item=None,render_scope=_KEEP_RENDER_SCOPE):
    if render_scope is _KEEP_RENDER_SCOPE:
        current=row("SELECT render_scope FROM project_control WHERE project_id=?",(project_id,))
        render_scope=current.get("render_scope") if current else None
    if render_scope not in {None,"long_16x9","short_9x16"}:raise ValueError(f"未知渲染范围：{render_scope}")
    execute(
        "INSERT INTO project_control(project_id,desired_state,resume_status,stage,item,render_scope,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET desired_state=excluded.desired_state,resume_status=excluded.resume_status,stage=excluded.stage,item=excluded.item,render_scope=excluded.render_scope,updated_at=excluded.updated_at",
        (project_id,desired_state,resume_status,stage,item,render_scope,now()),
    )

def control(project_id):
    value=row("SELECT * FROM project_control WHERE project_id=?",(project_id,))
    if value:return value
    project=row("SELECT status FROM projects WHERE id=?",(project_id,))
    if not project:return None
    create_control(project_id,"stopped",resume_status_for(project["status"]),stage_for(project["status"]))
    return row("SELECT * FROM project_control WHERE project_id=?",(project_id,))

def log_event(project_id,level,stage,event,message,details=None):
    if details is not None and not isinstance(details,str):details=json.dumps(details,ensure_ascii=False)
    log_id=execute(
        "INSERT INTO project_logs(project_id,level,stage,event,message,details,created_at) VALUES(?,?,?,?,?,?,?)",
        (project_id,level,stage or "系统",event,message,details,now()),
    )
    execute("DELETE FROM project_logs WHERE project_id=? AND id NOT IN (SELECT id FROM project_logs WHERE project_id=? ORDER BY id DESC LIMIT 2000)",(project_id,project_id))
    return log_id

def set_progress(project_id,resume_status,stage,item=None):
    execute(
        "UPDATE project_control SET resume_status=?,stage=?,item=?,updated_at=? WHERE project_id=?",
        (resume_status,stage,item,now(),project_id),
    )

def _settle_control(project_id,desired_state,resume_status,stage,item=None):
    final_state="paused" if desired_state in {"pause_requested","paused"} else "stopped"
    current=control(project_id)
    execute(
        "UPDATE project_control SET desired_state=?,resume_status=?,stage=?,item=?,updated_at=? WHERE project_id=?",
        (final_state,resume_status,stage,item,now(),project_id),
    )
    if not current or current["desired_state"]!=final_state:
        verb="暂停" if final_state=="paused" else "停止"
        location=f"{stage} · {item}" if item else stage
        log_event(project_id,"warning",stage,final_state,f"项目已在 {location} 安全{verb}")
    return False

def checkpoint(project_id,resume_status,stage,item=None):
    set_progress(project_id,resume_status,stage,item)
    current=control(project_id)
    desired=current["desired_state"] if current else "stopped"
    if desired in {"pause_requested","paused","stop_requested","stopped"}:
        return _settle_control(project_id,desired,resume_status,stage,item)
    return desired=="running"

def begin_stage(project_id,active_status,resume_status,stage,item=None):
    if not checkpoint(project_id,resume_status,stage,item):return False
    execute("UPDATE projects SET status=?,updated_at=?,error=NULL WHERE id=?",(active_status,now(),project_id))
    log_event(project_id,"info",stage,"stage_started",f"开始{stage}",{"resume_status":resume_status})
    return True

def finish_stage(project_id,next_status,stage,message):
    execute("UPDATE projects SET status=?,updated_at=?,error=NULL WHERE id=?",(next_status,now(),project_id))
    set_progress(project_id,resume_status_for(next_status),stage_for(next_status),None)
    log_event(project_id,"success",stage,"stage_completed",message,{"next_status":next_status})

def fail_stage(project_id,failed_status,resume_status,stage,message,details=None):
    execute("UPDATE projects SET status=?,error=?,updated_at=? WHERE id=?",(failed_status,message,now(),project_id))
    failure_item=f"已失败：{message[:160]}"
    create_control(project_id,"stopped",resume_status,stage,failure_item)
    log_event(project_id,"error",stage,"error",message,details)

def recover_interrupted_projects(owner=None):
    statuses=ACTIVE_STATUSES if owner is None else RECOVERY_STATUSES.get(owner)
    if statuses is None:raise ValueError(f"未知恢复责任方：{owner}")
    placeholders=','.join('?' for _ in statuses)
    for project in rows(f"SELECT p.id,p.status,c.desired_state,c.resume_status,c.stage,c.item FROM projects p JOIN project_control c ON c.project_id=p.id WHERE p.status IN ({placeholders})",tuple(statuses)):
        desired="paused" if project["desired_state"] in {"pause_requested","paused"} else "stopped"
        resume=project.get("resume_status") or resume_status_for(project["status"])
        create_control(project["id"],desired,resume,project.get("stage") or stage_for(project["status"]),project.get("item"))
        execute("UPDATE projects SET status=?,updated_at=? WHERE id=?",(resume,now(),project["id"]))
        log_event(project["id"],"warning",project.get("stage") or stage_for(project["status"]),"recovered","检测到上次运行中断，已恢复到安全检查点",{"from_status":project["status"],"resume_status":resume})

def render_queue(project_id):
    result={fmt:{"pending":0,"rendering":0,"failed":0,"ready":0,"next_version":None} for fmt in ("long_16x9","short_9x16")}
    for value in rows("""SELECT format,
                                SUM(CASE WHEN status='render_requested' THEN 1 ELSE 0 END) AS pending,
                                SUM(CASE WHEN status='rendering' THEN 1 ELSE 0 END) AS rendering,
                                SUM(CASE WHEN status='render_failed' THEN 1 ELSE 0 END) AS failed
                         FROM exports WHERE project_id=? GROUP BY format""",(project_id,)):
        if value["format"] not in result:continue
        item=result[value["format"]]
        for key in ("pending","rendering","failed"):item[key]=int(value.get(key) or 0)
        item["ready"]=item["pending"]+item["failed"]
    for fmt,item in result.items():
        next_export=row("SELECT version FROM exports WHERE project_id=? AND format=? AND status IN ('render_requested','render_failed') ORDER BY id LIMIT 1",(project_id,fmt))
        item["next_version"]=next_export.get("version") if next_export else None
    return result

def project_summaries():
    return rows("SELECT p.*,c.desired_state AS control_state,c.resume_status,c.stage AS control_stage,c.item AS control_item FROM projects p LEFT JOIN project_control c ON c.project_id=p.id ORDER BY p.id DESC")

def project_detail(project_id):
    project=row("SELECT * FROM projects WHERE id=?",(project_id,))
    if not project: return None
    project["settings"]=json.loads(project.get("settings") or "{}")
    project["assets"]=rows("SELECT * FROM assets WHERE project_id=? ORDER BY id",(project_id,))
    project["revisions"]=rows("SELECT * FROM revisions WHERE project_id=? ORDER BY id DESC",(project_id,))
    project["exports"]=rows("SELECT * FROM exports WHERE project_id=? ORDER BY id DESC",(project_id,))
    project["uploads"]=rows("SELECT * FROM platform_uploads WHERE project_id=? ORDER BY platform",(project_id,))
    project["control"]=control(project_id)
    project["render_queue"]=render_queue(project_id)
    project["workers"]=worker_statuses()
    project["required_worker"]=required_worker_for_status((project["control"] or {}).get("resume_status") or project["status"])
    project["logs"]=rows("SELECT * FROM project_logs WHERE project_id=? ORDER BY id DESC LIMIT 200",(project_id,))
    return project

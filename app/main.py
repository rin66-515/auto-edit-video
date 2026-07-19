import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI,HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from . import db
from .config import INBOX,PROXIES,OUTPUTS,MIN_FREE_GIB
from .pipeline import start_background,scan_once

PLATFORMS=("youtube","bilibili","douyin","xiaohongshu")
@asynccontextmanager
async def lifespan(app):
    db.init_db();start_background();yield
app=FastAPI(title="Vlog Automation",lifespan=lifespan)
STATIC=Path(__file__).parent/"static";app.mount("/static",StaticFiles(directory=STATIC),name="static")
class RevisionIn(BaseModel):kind:str="edit";body:str
class SettingsIn(BaseModel):mode:str="existing";notes:str="";settings:dict=Field(default_factory=dict)
@app.get("/")
def index():return FileResponse(STATIC/"index.html")
@app.get("/api/health")
def health():
    usage=shutil.disk_usage(INBOX);return {"ok":True,"free_gib":round(usage.free/1024**3,1),"minimum_free_gib":MIN_FREE_GIB}
@app.post("/api/scan")
def scan():scan_once();return {"ok":True}
@app.get("/api/projects")
def projects():return db.rows("SELECT * FROM projects ORDER BY id DESC")
@app.get("/api/projects/{project_id}")
def project(project_id:int):
    item=db.project_detail(project_id)
    if not item:raise HTTPException(404,"项目不存在")
    return item
@app.get("/api/assets/{asset_id}/proxy")
def asset_proxy(asset_id:int):
    asset=db.row("SELECT proxy_path FROM assets WHERE id=?",(asset_id,))
    if not asset or not asset.get('proxy_path'):raise HTTPException(404,"代理视频不存在")
    path=Path(asset['proxy_path']).resolve()
    if not path.is_relative_to(PROXIES.resolve()) or not path.exists():raise HTTPException(404,"代理视频不存在")
    return FileResponse(path,media_type='video/mp4')
@app.get("/api/exports/{export_id}/files/{file_index}")
def export_file(export_id:int,file_index:int):
    export=db.row("SELECT path FROM exports WHERE id=?",(export_id,))
    if not export or not export.get('path'):raise HTTPException(404,"成片不存在")
    try:paths=json.loads(export['path'])
    except json.JSONDecodeError:paths=[export['path']]
    if file_index<0 or file_index>=len(paths):raise HTTPException(404,"成片不存在")
    path=Path(paths[file_index]).resolve()
    if not path.is_relative_to(OUTPUTS.resolve()) or not path.exists():raise HTTPException(404,"成片不存在")
    return FileResponse(path,media_type='video/mp4',filename=path.name)
@app.put("/api/projects/{project_id}")
def update_project(project_id:int,body:SettingsIn):
    db.execute("UPDATE projects SET mode=?,notes=?,settings=?,updated_at=? WHERE id=?",(body.mode,body.notes,json.dumps(body.settings,ensure_ascii=False),db.now(),project_id));return db.project_detail(project_id)
@app.post("/api/projects/{project_id}/revisions")
def add_revision(project_id:int,body:RevisionIn):
    rid=db.execute("INSERT INTO revisions(project_id,kind,body,created_at) VALUES(?,?,?,?)",(project_id,body.kind,body.body,db.now()));db.execute("UPDATE projects SET status='revision_requested',updated_at=? WHERE id=?",(db.now(),project_id));return {"id":rid,"ok":True}
@app.post("/api/projects/{project_id}/exports/{fmt}")
def request_export(project_id:int,fmt:str):
    if fmt not in ("long_16x9","short_9x16"):raise HTTPException(400,"未知格式")
    version=f"v{len(db.rows('SELECT id FROM exports WHERE project_id=?',(project_id,)))+1}";eid=db.execute("INSERT INTO exports(project_id,version,format,status,created_at) VALUES(?,?,?,?,?)",(project_id,version,fmt,"render_requested",db.now()));db.execute("UPDATE projects SET status='render_requested',updated_at=? WHERE id=?",(db.now(),project_id));return {"id":eid,"version":version,"status":"render_requested"}
@app.post("/api/exports/{export_id}/approve")
def approve_export(export_id:int):db.execute("UPDATE exports SET status='approved',locked=1,approved_at=? WHERE id=?",(db.now(),export_id));return {"ok":True}
@app.post("/api/projects/{project_id}/uploads/{platform}")
def upload_done(project_id:int,platform:str):
    if platform not in PLATFORMS:raise HTTPException(400,"未知平台")
    stamp=db.now();db.execute("INSERT INTO platform_uploads(project_id,platform,completed_at) VALUES(?,?,?) ON CONFLICT(project_id,platform) DO UPDATE SET completed_at=excluded.completed_at",(project_id,platform,stamp));done=db.rows("SELECT platform FROM platform_uploads WHERE project_id=? AND completed_at IS NOT NULL",(project_id,))
    if {r['platform'] for r in done}==set(PLATFORMS):db.execute("UPDATE projects SET upload_confirmed_at=?,status='published',updated_at=? WHERE id=?",(stamp,stamp,project_id))
    return {"ok":True,"completed":[r['platform'] for r in done]}

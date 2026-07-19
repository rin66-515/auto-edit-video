import json
import re
import shutil
import threading
import time
from datetime import datetime,timedelta,timezone
from pathlib import Path
from . import db
from .config import INBOX,PROJECTS,PROXIES,AUDIO,OUTPUTS,MUSIC,VIDEO_EXTENSIONS,SCAN_SECONDS,STABLE_SECONDS,MIN_FREE_GIB,RAW_RETENTION_DAYS,FINAL_RETENTION_DAYS
from .media import probe,create_derivatives,render_timeline

FOLDER_STATE={}

def slugify(name):
    cleaned=re.sub(r"[^\w\-\u3000-\u9fff]+","-",name,flags=re.UNICODE).strip("-")
    return cleaned[:80] or f"project-{int(time.time())}"
def free_gib(): return shutil.disk_usage(INBOX).free/1024**3

def import_project(folder:Path):
    if db.row("SELECT id FROM projects WHERE source_dir=?",(str(folder),)) or free_gib()<MIN_FREE_GIB: return
    videos=[p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]
    if not videos:return
    slug=slugify(folder.name)
    if db.row("SELECT id FROM projects WHERE slug=?",(slug,)):slug=f"{slug}-{int(time.time())}"
    stamp=db.now();pid=db.execute("INSERT INTO projects(slug,title,source_dir,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",(slug,folder.name,str(folder),"preprocessing",stamp,stamp))
    for idx,source in enumerate(videos,1):
        try:
            info=probe(source);key=f"{pid}-{idx:04d}";proxy=PROXIES/slug/f"{key}.mp4";audio=AUDIO/slug/f"{key}.wav";thumb=PROJECTS/slug/"thumbnails"/f"{key}.jpg"
            create_derivatives(source,proxy,audio,thumb)
            db.execute("INSERT INTO assets(project_id,path,filename,bytes,duration,width,height,fps,codec,proxy_path,audio_path,thumbnail_path,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(pid,str(source),source.name,source.stat().st_size,info['duration'],info['width'],info['height'],info['fps'],info['codec'],str(proxy),str(audio),str(thumb),db.now()))
        except Exception as exc: db.execute("UPDATE projects SET error=? WHERE id=?",(f"{source.name}: {exc}",pid))
    db.execute("UPDATE projects SET status='ready_for_audio',updated_at=? WHERE id=?",(db.now(),pid))

def scan_once():
    for folder in INBOX.iterdir():
        if not folder.is_dir():continue
        files=[p for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]
        if not files:continue
        signature=tuple(sorted((str(p.relative_to(folder)),p.stat().st_size,p.stat().st_mtime_ns) for p in files));previous=FOLDER_STATE.get(str(folder));now=time.time()
        if (folder/'READY.txt').exists() or (previous and previous[0]==signature and now-previous[1]>=STABLE_SECONDS):import_project(folder)
        elif not previous or previous[0]!=signature:FOLDER_STATE[str(folder)]=(signature,now)

def retention_once():
    now=datetime.now(timezone.utc)
    for p in db.rows("SELECT * FROM projects"):
        if p.get("upload_confirmed_at") and now>=datetime.fromisoformat(p['upload_confirmed_at'])+timedelta(days=RAW_RETENTION_DAYS):
            for asset in db.rows("SELECT path,proxy_path,audio_path FROM assets WHERE project_id=?",(p['id'],)):
                for key in ('path','proxy_path','audio_path'):
                    target=Path(asset[key]) if asset.get(key) else None
                    if target and target.exists() and (str(target).startswith(str(INBOX)) or "_automation" in str(target)):target.unlink(missing_ok=True)
        for export in db.rows("SELECT * FROM exports WHERE project_id=? AND approved_at IS NOT NULL",(p['id'],)):
            if now>=datetime.fromisoformat(export['approved_at'])+timedelta(days=FINAL_RETENTION_DAYS) and export.get('path'):
                try: targets=json.loads(export['path'])
                except json.JSONDecodeError: targets=[export['path']]
                for target in targets:Path(target).unlink(missing_ok=True)

def render_once():
    export=db.row("SELECT e.*,p.slug,p.settings FROM exports e JOIN projects p ON p.id=e.project_id WHERE e.status='render_requested' ORDER BY e.id LIMIT 1")
    if not export:return
    db.execute("UPDATE exports SET status='rendering' WHERE id=?",(export['id'],));db.execute("UPDATE projects SET status='rendering',updated_at=? WHERE id=?",(db.now(),export['project_id']))
    try:
        plan=json.loads(export.get('settings') or '{}').get('story_plan',{});assets=db.rows("SELECT * FROM assets WHERE project_id=? ORDER BY id",(export['project_id'],));outdir=OUTPUTS/export['slug']/export['version'];paths=[]
        timelines=[('long',plan.get('long',{}).get('timeline',[]))] if export['format']=='long_16x9' else [(f"short-{i+1}",x.get('timeline',[])) for i,x in enumerate(plan.get('shorts',[]))]
        for name,timeline in timelines:
            target=outdir/f"{name}.mp4";music=[p for p in MUSIC.iterdir() if p.suffix.lower() in {'.mp3','.wav','.m4a','.aac','.flac'}];render_timeline(assets,timeline,export['format'],target,PROJECTS/export['slug']/"render-temp"/f"{export['id']}-{name}",music);paths.append(str(target))
        db.execute("UPDATE exports SET status='review_ready',path=? WHERE id=?",(json.dumps(paths,ensure_ascii=False),export['id']));db.execute("UPDATE projects SET status='review_ready',updated_at=? WHERE id=?",(db.now(),export['project_id']))
    except Exception as exc:
        db.execute("UPDATE exports SET status='render_failed' WHERE id=?",(export['id'],));db.execute("UPDATE projects SET status='render_failed',error=?,updated_at=? WHERE id=?",(str(exc),db.now(),export['project_id']))

def loop():
    while True:
        try:scan_once();render_once();retention_once()
        except Exception as exc:print(f"background loop: {exc}",flush=True)
        time.sleep(SCAN_SECONDS)
def start_background():threading.Thread(target=loop,name="vlog-watcher",daemon=True).start()

import json
import re
import shutil
import threading
import time
import traceback
from datetime import datetime,timedelta,timezone
from pathlib import Path
from . import db
from .config import INBOX,PROJECTS,PROXIES,AUDIO,OUTPUTS,MUSIC,VIDEO_EXTENSIONS,SCAN_SECONDS,STABLE_SECONDS,MIN_FREE_GIB,RAW_RETENTION_DAYS,FINAL_RETENTION_DAYS
from .media import apply_text_overlays,burn_subtitles,probe,create_derivatives,render_timeline,trim_after_final_black,write_bilingual_srt
from .privacy import anonymize_video,automatic_privacy_enabled,privacy_enabled

FOLDER_STATE={}
SCAN_LOCK=threading.Lock()

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
    db.create_control(pid,"importing","ready_for_audio","素材导入",None)
    db.log_event(pid,"info","素材导入","import_started",f"开始导入 {len(videos)} 个视频",{"source_dir":str(folder)})
    for idx,source in enumerate(videos,1):
        try:
            info=probe(source);key=f"{pid}-{idx:04d}";proxy=PROXIES/slug/f"{key}.mp4";audio=AUDIO/slug/f"{key}.wav";thumb=PROJECTS/slug/"thumbnails"/f"{key}.jpg"
            create_derivatives(source,proxy,audio,thumb)
            db.execute("INSERT INTO assets(project_id,path,filename,bytes,duration,width,height,fps,codec,proxy_path,audio_path,thumbnail_path,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(pid,str(source),source.name,source.stat().st_size,info['duration'],info['width'],info['height'],info['fps'],info['codec'],str(proxy),str(audio),str(thumb),db.now()))
            db.create_control(pid,"importing","ready_for_audio","素材导入",source.name)
            db.log_event(pid,"info","素材导入","asset_imported",f"素材已导入：{source.name}",{"index":idx,"total":len(videos)})
        except Exception as exc:
            message=f"{source.name}: {exc}";db.execute("UPDATE projects SET error=? WHERE id=?",(message,pid));db.log_event(pid,"error","素材导入","asset_error",message,traceback.format_exc())
    imported=db.row("SELECT COUNT(*) AS count FROM assets WHERE project_id=?",(pid,))["count"]
    if not imported:
        db.fail_stage(pid,"preprocess_failed","preprocessing","素材导入","没有素材成功导入")
        return
    db.execute("UPDATE projects SET status='waiting_start',updated_at=? WHERE id=?",(db.now(),pid))
    db.create_control(pid,"stopped","ready_for_audio","音频清理",None)
    db.log_event(pid,"success","素材导入","import_completed",f"成功导入 {imported}/{len(videos)} 个视频，等待点击“启动项目”")

def scan_once():
    if not SCAN_LOCK.acquire(blocking=False):return False
    try:
        for folder in INBOX.iterdir():
            if not folder.is_dir():continue
            files=[p for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]
            if not files:continue
            signature=tuple(sorted((str(p.relative_to(folder)),p.stat().st_size,p.stat().st_mtime_ns) for p in files));previous=FOLDER_STATE.get(str(folder));now=time.time()
            if (folder/'READY.txt').exists() or (previous and previous[0]==signature and now-previous[1]>=STABLE_SECONDS):import_project(folder)
            elif not previous or previous[0]!=signature:FOLDER_STATE[str(folder)]=(signature,now)
        return True
    finally:SCAN_LOCK.release()

def _tree_bytes(path:Path):
    if not path.exists():return 0
    return sum(value.stat().st_size for value in path.rglob("*") if value.is_file())

def _remove_tree(path:Path,root:Path):
    path=path.resolve();root=root.resolve()
    if not path.is_relative_to(root) or path==root:raise RuntimeError(f"拒绝清理越界目录：{path}")
    released=_tree_bytes(path)
    shutil.rmtree(path,ignore_errors=False) if path.exists() else None
    return released

def _project_cleanup_safe(project_id):
    control=db.control(project_id) or {"desired_state":"stopped"}
    return control.get("desired_state") in {"stopped","paused"} and not db.row("SELECT id FROM exports WHERE project_id=? AND status='rendering' LIMIT 1",(project_id,))

def cleanup_project_temp(project):
    if not _project_cleanup_safe(project["id"]):return 0
    targets=[PROJECTS/project["slug"]/name for name in ("render-temp","masters","caption-version-backups")];released=0;removed=[]
    for target in targets:
        if target.exists():released+=_remove_tree(target,PROJECTS);removed.append(str(target))
    if not removed:return 0
    db.log_event(project["id"],"info","存储清理","temporary_files_deleted",f"四平台均已确认，已清理渲染缓存与快速修正母版，释放 {released/1024**3:.2f} GiB",{"released_bytes":released,"paths":removed})
    return released

def cleanup_project_intermediates(project):
    if not _project_cleanup_safe(project["id"]):return 0
    released=0;deleted_files=0;inbox=INBOX.resolve();source_dir=Path(project["source_dir"]).resolve()
    if not source_dir.is_relative_to(inbox):raise RuntimeError(f"项目原片目录不在 inbox 内：{source_dir}")
    for asset in db.rows("SELECT path FROM assets WHERE project_id=?",(project["id"],)):
        target=Path(asset["path"]).resolve()
        if not target.is_relative_to(source_dir) or target.suffix.lower() not in VIDEO_EXTENSIONS:raise RuntimeError(f"原片路径校验失败：{target}")
        if target.exists():released+=target.stat().st_size;target.unlink();deleted_files+=1
    for target,root in ((PROXIES/project["slug"],PROXIES),(AUDIO/project["slug"],AUDIO),(PROJECTS/project["slug"],PROJECTS)):
        if target.exists():released+=_remove_tree(target,root)
    stamp=db.now()
    db.execute("UPDATE projects SET raw_deleted_at=COALESCE(raw_deleted_at,?),intermediates_deleted_at=?,updated_at=? WHERE id=?",(stamp,stamp,stamp,project["id"]))
    db.log_event(project["id"],"warning","存储清理","intermediates_deleted",f"四平台确认已满 {RAW_RETENTION_DAYS} 天，原片和中间文件已自动清理，释放 {released/1024**3:.2f} GiB",{"released_bytes":released,"raw_files":deleted_files})
    return released

def cleanup_expired_export(project,export):
    released=0;deleted_files=0;output_root=OUTPUTS.resolve()
    try:targets=json.loads(export.get("path") or "[]")
    except json.JSONDecodeError:targets=[export["path"]]
    if isinstance(targets,str):targets=[targets]
    for value in targets:
        target=Path(value).resolve()
        if not target.is_relative_to(output_root):raise RuntimeError(f"成片路径不在 outputs 内：{target}")
        for candidate in (target,target.with_suffix(".zh-ja.srt")):
            if candidate.exists() and candidate.is_file():released+=candidate.stat().st_size;candidate.unlink();deleted_files+=1
    db.execute("UPDATE exports SET status='expired',path='[]' WHERE id=?",(export["id"],))
    db.log_event(project["id"],"warning","存储清理","final_expired",f"{export['version']} 成片保留满 {FINAL_RETENTION_DAYS} 天，已自动删除，释放 {released/1024**3:.2f} GiB",{"export_id":export["id"],"released_bytes":released,"deleted_files":deleted_files})
    return released

def retention_once():
    now=datetime.now(timezone.utc)
    for p in db.rows("SELECT * FROM projects"):
        if p.get("upload_confirmed_at"):
            cleanup_project_temp(p)
            if not p.get("intermediates_deleted_at") and now>=datetime.fromisoformat(p['upload_confirmed_at'])+timedelta(days=RAW_RETENTION_DAYS):cleanup_project_intermediates(p)
            uploaded_at=datetime.fromisoformat(p["upload_confirmed_at"])
            for export in db.rows("SELECT * FROM exports WHERE project_id=? AND approved_at IS NOT NULL AND status!='expired'",(p['id'],)):
                retention_start=max(uploaded_at,datetime.fromisoformat(export["approved_at"]))
                if _project_cleanup_safe(p["id"]) and now>=retention_start+timedelta(days=FINAL_RETENTION_DAYS) and export.get("path"):cleanup_expired_export(p,export)

def _selected_music(render_options):
    requested=render_options.get("bgm_filename") if isinstance(render_options,dict) else None
    if requested:
        candidate=(MUSIC/Path(str(requested)).name).resolve()
        if not candidate.is_relative_to(MUSIC.resolve()) or not candidate.is_file():raise RuntimeError(f"缺少指定 BGM：{requested}；请放入 {MUSIC}")
        return [candidate]
    return []

def subtitle_render_once():
    export=db.row("""SELECT e.*,p.slug,p.settings,c.render_scope
                     FROM exports e
                     JOIN projects p ON p.id=e.project_id
                     JOIN project_control c ON c.project_id=p.id
                     WHERE e.status='subtitle_render_requested' AND c.desired_state='running'
                       AND p.status IN ('subtitle_render_requested','subtitle_rendering')
                       AND (c.render_scope IS NULL OR c.render_scope=e.format)
                     ORDER BY e.id LIMIT 1""")
    if not export:return
    stage="字幕快速生成"
    if free_gib()<MIN_FREE_GIB:
        db.create_control(export["project_id"],"stopped","subtitle_render_requested",stage,export["version"])
        db.log_event(export["project_id"],"error","磁盘保护","low_disk",f"D盘低于 {MIN_FREE_GIB} GiB 安全线，字幕渲染未启动")
        return
    if not db.begin_stage(export["project_id"],"subtitle_rendering","subtitle_render_requested",stage,export["version"]):return
    db.execute("UPDATE exports SET status='subtitle_rendering' WHERE id=?",(export["id"],))
    try:
        try:manifest=json.loads(export.get("master_manifest") or "{}")
        except json.JSONDecodeError:manifest={}
        outputs=manifest.get("outputs") if isinstance(manifest,dict) else None
        if not isinstance(outputs,list) or not outputs:raise RuntimeError("该版本没有可复用的马赛克母版")
        try:snapshot=json.loads(export.get("timeline_snapshot") or "{}")
        except json.JSONDecodeError:snapshot={}
        try:overrides=json.loads(export.get("caption_overrides") or "{}")
        except json.JSONDecodeError:overrides={}
        assets=db.rows("SELECT * FROM assets WHERE project_id=? ORDER BY id",(export["project_id"],))
        outdir=OUTPUTS/export["slug"]/export["version"];paths=[]
        for index,item in enumerate(outputs,1):
            name=str(item.get("name") or f"output-{index}");privacy=Path(str(item.get("privacy") or "")).resolve()
            if not privacy.is_relative_to(PROJECTS.resolve()) or not privacy.is_file():raise RuntimeError(f"马赛克母版不存在：{name}")
            timeline=snapshot.get(name)
            if not isinstance(timeline,list) or not timeline:raise RuntimeError(f"成片时间线不存在：{name}")
            target=outdir/f"{name}.mp4";srt=target.with_suffix(".zh-ja.srt")
            write_bilingual_srt(assets,timeline,srt,export["format"],overrides,name)
            db.set_progress(export["project_id"],"subtitle_render_requested",stage,f"{export['version']} · {name} · 正在烧录最终字幕")
            burn_subtitles(privacy,target,srt,export["format"]);paths.append(str(target))
            db.log_event(export["project_id"],"success",stage,"subtitle_output_completed",f"字幕快速版已生成：{export['version']} · {target.name}",{"path":str(target),"output":name,"caption_revision":int(export.get("caption_revision") or 0)})
        db.execute("UPDATE exports SET status='review_ready',path=?,render_mode='subtitle_only' WHERE id=?",(json.dumps(paths,ensure_ascii=False),export["id"]))
        pending_subtitle=db.row("SELECT id FROM exports WHERE project_id=? AND status='subtitle_render_requested' LIMIT 1",(export["project_id"],))
        pending_full=db.row("SELECT id FROM exports WHERE project_id=? AND status='render_requested' LIMIT 1",(export["project_id"],))
        if pending_subtitle:
            next_status="subtitle_render_requested";resume="subtitle_render_requested";item=f"{export['version']} 字幕版完成；其他字幕任务等待"
        elif pending_full:
            next_status="render_requested";resume="render_requested";item=f"{export['version']} 字幕版完成；其他母版任务等待"
        else:
            next_status="review_ready";resume=None;item=f"{export['version']} 字幕版完成，等待最终审核"
        db.finish_stage(export["project_id"],next_status,stage,item)
        db.create_control(export["project_id"],"stopped",resume,db.stage_for(next_status),item,render_scope=None)
        db.log_event(export["project_id"],"success","人工审核","subtitle_review_waiting",f"{export['version']} 已完成字幕快速生成，项目已自动停止")
    except InterruptedError:
        db.execute("UPDATE exports SET status='subtitle_render_requested' WHERE id=?",(export["id"],))
    except Exception as exc:
        db.execute("UPDATE exports SET status='subtitle_render_failed' WHERE id=?",(export["id"],))
        db.fail_stage(export["project_id"],"subtitle_render_failed","subtitle_render_requested",stage,str(exc),traceback.format_exc())

def render_once():
    export=db.row("""SELECT e.*,p.slug,p.settings,c.render_scope
                     FROM exports e
                     JOIN projects p ON p.id=e.project_id
                     JOIN project_control c ON c.project_id=p.id
                     WHERE e.status='render_requested' AND c.desired_state='running'
                       AND p.status IN ('render_requested','rendering')
                       AND (c.render_scope IS NULL OR c.render_scope=e.format)
                     ORDER BY e.id LIMIT 1""")
    if not export:return
    stage="成片渲染"
    if free_gib()<MIN_FREE_GIB:
        db.create_control(export['project_id'],"stopped","render_requested",stage,export['version'])
        db.log_event(export['project_id'],"error","磁盘保护","low_disk",f"D盘低于 {MIN_FREE_GIB} GiB 安全线，渲染未启动")
        return
    if not db.begin_stage(export['project_id'],"rendering","render_requested",stage,export['version']):return
    db.execute("UPDATE exports SET status='rendering' WHERE id=?",(export['id'],))
    try:
        project_settings=json.loads(export.get('settings') or '{}');plan=project_settings.get('story_plan',{});assets=db.rows("SELECT * FROM assets WHERE project_id=? ORDER BY id",(export['project_id'],));masterdir=PROJECTS/export['slug']/"masters"/str(export["id"]);manifest_outputs=[]
        try:render_options=json.loads(export.get('render_options') or '{}')
        except json.JSONDecodeError:render_options={}
        privacy_only=export.get("render_mode")=="privacy_only"
        source_outputs={}
        if privacy_only:
            source=db.row("SELECT master_manifest FROM exports WHERE id=? AND project_id=?",(export.get("source_export_id"),export["project_id"]))
            if not source:raise RuntimeError("隐私快速版找不到来源版本")
            try:source_manifest=json.loads(source.get("master_manifest") or "{}")
            except json.JSONDecodeError:source_manifest={}
            source_outputs={str(value.get("name") or ""):value for value in source_manifest.get("outputs",[]) if isinstance(value,dict)}
            if not source_outputs:raise RuntimeError("来源版本没有可复用的无字幕剪辑母版")
        try:snapshot=json.loads(export.get('timeline_snapshot') or '{}')
        except json.JSONDecodeError:snapshot={}
        if not snapshot:snapshot={"long":plan.get('long',{}).get('timeline',[])} if export['format']=='long_16x9' else {f"short-{i+1}":x.get('timeline',[]) for i,x in enumerate(plan.get('shorts',[])) if isinstance(x,dict)}
        timelines=list(snapshot.items())
        try:caption_overrides=json.loads(export.get('caption_overrides') or '{}')
        except json.JSONDecodeError:caption_overrides={}
        music=_selected_music(render_options)
        timelines=[x for x in timelines if x[1]]
        if not timelines:raise RuntimeError("剪辑方案没有可渲染的时间线")
        long_total=1 if export['format']=='long_16x9' else 0
        short_total=len(timelines) if export['format']=='short_9x16' else 0
        latest_long=db.row("SELECT status FROM exports WHERE project_id=? AND format='long_16x9' ORDER BY id DESC LIMIT 1",(export['project_id'],))
        long_done=1 if long_total and latest_long and latest_long['status'] in {'review_ready','approved'} else 0;short_done=0
        def render_summary(detail=None):
            summary=f"成片进度：长篇 {long_done}/{long_total}｜短篇 {short_done}/{short_total}"
            return f"{summary} · {detail}" if detail else summary
        db.set_progress(export['project_id'],"render_requested",stage,render_summary(f"准备 {export['version']}"))
        db.log_event(export['project_id'],"info",stage,"render_progress",render_summary(f"开始 {export['version']}"),{"long_done":long_done,"long_total":long_total,"short_done":short_done,"short_total":short_total,"version":export['version']})
        last_render_heartbeat=[time.monotonic()]
        def render_checkpoint(item=None,asset=None):
            current=render_summary(asset['filename'] if asset else f"{export['version']} 合成无字幕母版")
            now=time.monotonic()
            if now-last_render_heartbeat[0]>=600:
                last_render_heartbeat[0]=now
                db.log_event(export['project_id'],"info",stage,"render_heartbeat",current,{"long_done":long_done,"long_total":long_total,"short_done":short_done,"short_total":short_total,"version":export['version']})
            if free_gib()<MIN_FREE_GIB:
                db.create_control(export['project_id'],"stopped","render_requested",stage,current)
                db.log_event(export['project_id'],"error","磁盘保护","low_disk",f"D盘低于 {MIN_FREE_GIB} GiB 安全线，已在 {current} 后停止渲染")
                return False
            return db.checkpoint(export['project_id'],"render_requested",stage,current)
        for output_index,(name,timeline) in enumerate(timelines,1):
            db.set_progress(export['project_id'],"render_requested",stage,render_summary(f"正在渲染 {export['version']} · {name}"))
            db.log_event(export['project_id'],"info",stage,"output_started",render_summary(f"开始渲染 {export['version']} · {name}"),{"long_done":long_done,"long_total":long_total,"short_done":short_done,"short_total":short_total,"version":export['version'],"output":name})
            edit_master=masterdir/f"{name}.edit-master.mp4";privacy_master=masterdir/f"{name}.privacy-master.mp4"
            if privacy_only:
                source_item=source_outputs.get(name) or {}
                source_edit=Path(str(source_item.get("edit") or "")).resolve()
                if not source_edit.is_relative_to(PROJECTS.resolve()) or not source_edit.is_file():raise RuntimeError(f"来源版本的无字幕剪辑母版不存在：{name}")
                edit_master.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source_edit,edit_master)
                db.log_event(export['project_id'],"info","人工马赛克校对","edit_master_reused",f"已复用来源版本剪辑母版：{name}",{"source_edit":str(source_edit),"target_edit":str(edit_master)})
            else:
                render_timeline(assets,timeline,export['format'],edit_master,PROJECTS/export['slug']/"render-temp"/f"{export['id']}-{name}",music,render_checkpoint,caption_overrides,name,burn_captions=False,mix_options=render_options)
            text_overlays=render_options.get("text_overlays") if isinstance(render_options,dict) else None
            if text_overlays and not privacy_only:
                overlay_stats=apply_text_overlays(edit_master,text_overlays);db.log_event(export['project_id'],"success","动态文字","text_overlays_completed",f"已为 {edit_master.name} 加入 {overlay_stats['applied']} 处动态文字",overlay_stats)
            black_trim=render_options.get("final_black_trim") if isinstance(render_options,dict) else None
            if not privacy_only and isinstance(black_trim,dict) and black_trim.get("enabled"):
                trim_stats=trim_after_final_black(edit_master,black_trim.get("search_seconds",5.0))
                db.log_event(export['project_id'],"success" if trim_stats.get("trimmed") else "info","成片收尾","final_black_trimmed",f"已在最后黑屏处收尾：{edit_master.name}" if trim_stats.get("trimmed") else f"结尾未发现需要删除的黑屏后回闪：{edit_master.name}",trim_stats)
            privacy_master.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(edit_master,privacy_master)
            privacy_rules=render_options.get("privacy_rules") if isinstance(render_options,dict) else None
            automatic_privacy=automatic_privacy_enabled(project_settings)
            if privacy_enabled(project_settings,privacy_rules):
                privacy_stage="人脸隐私处理";db.log_event(export['project_id'],"info",privacy_stage,"privacy_started",f"开始按人工意见添加普通马赛克：{privacy_master.name}" if not automatic_privacy else f"开始自动识别人脸并添加普通马赛克：{privacy_master.name}")
                last_privacy_heartbeat=[time.monotonic()]
                def privacy_checkpoint(frames,total):
                    percent=round(frames/max(total,1)*100,1) if total else 0
                    item=f"{export['version']} {privacy_master.name} · {percent}%"
                    if free_gib()<MIN_FREE_GIB:
                        db.create_control(export['project_id'],"stopped","render_requested",privacy_stage,item)
                        db.log_event(export['project_id'],"error","磁盘保护","low_disk",f"D盘低于 {MIN_FREE_GIB} GiB，已停止人脸隐私处理")
                        return False
                    return db.checkpoint(export['project_id'],"render_requested",privacy_stage,item)
                def privacy_progress(frames,total,owner_faces,covered_faces):
                    now=time.monotonic()
                    if now-last_privacy_heartbeat[0]<600:return
                    last_privacy_heartbeat[0]=now;percent=round(frames/max(total,1)*100,1) if total else 0
                    db.log_event(export['project_id'],"info",privacy_stage,"privacy_heartbeat",f"普通马赛克处理仍在运行：{privacy_master.name} · {percent}%",{"frames":frames,"total_frames":total,"owner_faces":owner_faces,"mosaic_covered_faces":covered_faces})
                stats=anonymize_video(privacy_master,checkpoint=privacy_checkpoint,progress=privacy_progress,manual_rules=privacy_rules,automatic=automatic_privacy)
                db.log_event(export['project_id'],"success",privacy_stage,"privacy_completed",f"人脸隐私处理完成：{privacy_master.name}",stats)
            manifest_outputs.append({"name":name,"edit":str(edit_master),"privacy":str(privacy_master)})
            if name=='long':long_done=1
            else:short_done=output_index
            db.set_progress(export['project_id'],"render_requested",stage,render_summary(f"母版已生成 {name}"))
            db.log_event(export['project_id'],"success",stage,"master_output_completed",render_summary(f"无字幕与马赛克母版已生成：{name}"),{"edit_master":str(edit_master),"privacy_master":str(privacy_master),"long_done":long_done,"long_total":long_total,"short_done":short_done,"short_total":short_total,"version":export['version']})
        if not manifest_outputs:raise RuntimeError("没有生成任何母版文件")
        manifest={"schema":1,"created_at":db.now(),"outputs":manifest_outputs}
        db.execute("UPDATE exports SET status='caption_review_ready',path=NULL,master_manifest=?,render_mode='master_then_subtitle',caption_locked_at=NULL WHERE id=?",(json.dumps(manifest,ensure_ascii=False),export['id']))
        render_scope=export.get("render_scope")
        pending_any=db.row("SELECT id FROM exports WHERE project_id=? AND status='render_requested' LIMIT 1",(export['project_id'],))
        pending_selected=pending_any
        if render_scope:
            pending_selected=db.row("SELECT id FROM exports WHERE project_id=? AND status='render_requested' AND format=? LIMIT 1",(export['project_id'],render_scope))
        next_status="render_requested" if pending_any else "caption_review_ready"
        if pending_selected:
            db.finish_stage(export['project_id'],next_status,stage,f"{export['version']} 渲染完成，继续下一个版本")
        elif pending_any:
            label="长篇" if render_scope=="long_16x9" else "短篇"
            db.finish_stage(export['project_id'],next_status,stage,f"{export['version']} 渲染完成，本次{label}独立渲染已结束")
            db.create_control(export['project_id'],"stopped","render_requested",stage,f"{label}已完成；其他类型仍在等待",render_scope=None)
            db.log_event(export['project_id'],"success",stage,"render_scope_completed",f"{label}待渲染版本已全部生成，项目已自动停止；其他类型未启动",{"render_scope":render_scope})
        else:
            db.finish_stage(export['project_id'],next_status,stage,f"{export['version']} 母版完成，可以校对马赛克与成片字幕")
            db.create_control(export['project_id'],"stopped",None,"成片字幕校对",f"{export['version']} 母版完成；字幕可多次校对，锁定后再快速生成",render_scope=None)
            db.log_event(export['project_id'],"success","成片字幕校对","caption_review_waiting","母版已生成，项目已自动停止；可多次导入 Excel 修正时间点与中日字幕，确认后锁定并快速生成")
    except InterruptedError:
        db.execute("UPDATE exports SET status='render_requested' WHERE id=?",(export['id'],))
    except Exception as exc:
        try:db.log_event(export['project_id'],"error",stage,"render_progress_failed",render_summary(f"{export['version']} 失败：{exc}"),{"long_done":long_done,"long_total":long_total,"short_done":short_done,"short_total":short_total,"version":export['version']})
        except UnboundLocalError:pass
        db.execute("UPDATE exports SET status='render_failed' WHERE id=?",(export['id'],));db.fail_stage(export['project_id'],"render_failed","render_requested",stage,str(exc),traceback.format_exc())

def loop():
    while True:
        try:scan_once();subtitle_render_once();render_once();retention_once()
        except Exception as exc:print(f"background loop: {exc}",flush=True)
        time.sleep(SCAN_SECONDS)
def start_background():threading.Thread(target=loop,name="vlog-watcher",daemon=True).start()

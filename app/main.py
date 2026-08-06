import base64
import binascii
import hashlib
import json
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from fastapi import FastAPI,HTTPException
from fastapi.responses import FileResponse,Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from . import db
from .caption_clean import format_timecode,is_standalone_filler
from .caption_workbook import build_caption_workbook,read_caption_workbook
from .config import INBOX,PROXIES,OUTPUTS,PROJECTS,MIN_FREE_GIB,VIDEO_EXTENSIONS
from .pipeline import start_background,scan_once,cleanup_project_temp
from .render_plan import RenderPlanError,requested_snapshot
from .revision_intent import parse_revision_intent,revision_mode
from .privacy_intent import parse_privacy_intent
from .manual_revision import create_privacy_revision
from .short_revision import release_scheduled_short
from .version_revision import create_inherited_revision,validate_inherited_revision

PLATFORMS=("youtube","bilibili","douyin","xiaohongshu")
@asynccontextmanager
async def lifespan(app):
    db.init_db();db.recover_interrupted_projects("app");start_background();yield
app=FastAPI(title="Vlog Automation",lifespan=lifespan)
STATIC=Path(__file__).parent/"static";app.mount("/static",StaticFiles(directory=STATIC),name="static")
@app.middleware("http")
async def disable_ui_cache_during_testing(request,call_next):
    response=await call_next(request)
    if request.url.path=="/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"]="no-store, no-cache, must-revalidate"
        response.headers["Pragma"]="no-cache"
    return response
class RevisionIn(BaseModel):
    kind:str="edit"
    body:str
    source_export_id:int|None=None
class RevisionApplyIn(BaseModel):
    confirm_full_replan:bool=False
class SettingsIn(BaseModel):mode:str="existing";notes:str="";settings:dict=Field(default_factory=dict)
class CaptionWorkbookImportIn(BaseModel):filename:str="captions.xlsx";xlsx_base64:str
@app.get("/")
def index():return FileResponse(STATIC/"index.html")
@app.get("/api/health")
def health():
    usage=shutil.disk_usage(INBOX);return {"ok":True,"free_gib":round(usage.free/1024**3,1),"minimum_free_gib":MIN_FREE_GIB,"workers":db.worker_statuses()}
@app.post("/api/scan")
def scan():
    free_gib=shutil.disk_usage(INBOX).free/1024**3
    if free_gib<MIN_FREE_GIB:raise HTTPException(507,f"D盘空间不足：{free_gib:.1f} GiB；至少需要 {MIN_FREE_GIB} GiB")
    scan_once();return {"ok":True}
@app.get("/api/projects")
def projects():return db.project_summaries()
def _revision_source_clip_count(export):
    if not export or export.get("format")!="short_9x16":return None
    try:snapshot=json.loads(export.get("timeline_snapshot") or "{}")
    except json.JSONDecodeError:return None
    timeline=snapshot.get("short-1") if isinstance(snapshot,dict) else None
    return len(timeline) if isinstance(timeline,list) and timeline else None
def _parse_revision(revisions,source=None):
    if any(str(value.get("kind") or "")=="privacy" for value in revisions if isinstance(value,dict)):
        return parse_privacy_intent(revisions)
    return parse_revision_intent(revisions,_revision_source_clip_count(source))

@app.get("/api/projects/{project_id}")
def project(project_id:int):
    item=db.project_detail(project_id)
    if not item:raise HTTPException(404,"项目不存在")
    exports={int(value["id"]):value for value in item.get("exports") or []}
    for revision in item.get("revisions") or []:
        source=exports.get(int(revision.get("source_export_id") or 0))
        revision["parsed_intent"]=_parse_revision([revision],source)
    return item

@app.get("/api/projects/{project_id}/live")
def project_live(project_id:int):
    item=db.row("SELECT id,status,error FROM projects WHERE id=?",(project_id,))
    if not item:raise HTTPException(404,"项目不存在")
    item["control"]=db.control(project_id);item["render_queue"]=db.render_queue(project_id);item["workers"]=db.worker_statuses();resume=(item["control"] or {}).get("resume_status") or item["status"];item["required_worker"]=db.required_worker_for_status(resume);item["logs"]=db.rows("SELECT * FROM project_logs WHERE project_id=? ORDER BY id DESC LIMIT 200",(project_id,))
    item["locked_short"]=db.row("SELECT id,version,status FROM exports WHERE project_id=? AND format='short_9x16' AND locked=1 AND status='approved' ORDER BY id DESC LIMIT 1",(project_id,))
    counts=db.row("SELECT COUNT(*) AS total,SUM(CASE WHEN CAST(COALESCE(json_extract(analysis,'$.caption_version'),0) AS INTEGER)>=3 THEN 1 ELSE 0 END) AS ready FROM assets WHERE project_id=?",(project_id,)) or {"total":0,"ready":0}
    item["caption_assets_total"]=int(counts.get("total") or 0);item["caption_assets_ready"]=int(counts.get("ready") or 0);item["captions_complete"]=item["caption_assets_total"]>0 and item["caption_assets_ready"]==item["caption_assets_total"]
    return item

def _timeline_snapshot(export,settings=None):
    try:snapshot=json.loads(export.get("timeline_snapshot") or "{}")
    except json.JSONDecodeError:snapshot={}
    if snapshot:return snapshot
    if export.get("id"):return {}
    plan=(settings or {}).get("story_plan") or {}
    if export["format"]=="long_16x9":return {"long":plan.get("long",{}).get("timeline",[])}
    return {f"short-{index+1}":value.get("timeline",[]) for index,value in enumerate((plan.get("shorts") or [])[:1]) if isinstance(value,dict)}

def _requested_snapshot(project_id,version,fmt,settings,fallback_snapshot=None):
    try:
        return requested_snapshot(project_id,version,fmt,settings,fallback_snapshot)
    except RenderPlanError as exc:
        raise HTTPException(exc.status_code,str(exc)) from exc

def _caption_override_key(output_name,timeline_index,asset_id,caption_index):
    return f"{output_name}|{timeline_index}|{asset_id}|{caption_index}"

def _merge_export_caption_rows(rows):
    merged=[]
    for row in rows:
        previous=merged[-1] if merged else None
        same_caption=previous and all(previous.get(field)==row.get(field) for field in ("成片文件","素材ID","字幕序号","原识别","中文字幕","日文字幕","需复核"))
        contiguous=same_caption and row["_out_start"]<=previous["_out_end"]+0.08 and row["_out_start"]>=previous["_out_start"]-0.08
        if contiguous:
            previous["_out_end"]=max(previous["_out_end"],row["_out_end"])
            previous["成片结束秒"]=f"{previous['_out_end']:.3f}"
            previous["成片结束时间码"]=format_timecode(previous["_out_end"])
            previous["_keys"].extend(row["_keys"])
            previous["_members"].extend(row["_members"])
            previous["_positions"].extend(row["_positions"])
            previous["合并片段序号"]=",".join(str(value) for value in previous["_positions"])
            continue
        merged.append(row)
    return merged

def _export_caption_rows(export,settings=None,clean=True):
    assets={int(value["id"]):value for value in db.rows("SELECT id,filename,analysis FROM assets WHERE project_id=? ORDER BY id",(export["project_id"],))}
    try:overrides=json.loads(export.get("caption_overrides") or "{}")
    except json.JSONDecodeError:overrides={}
    rows=[]
    for output_name,timeline in _timeline_snapshot(export,settings).items():
        offset=0.0
        for position,item in enumerate(timeline if isinstance(timeline,list) else [],1):
            if position>1 and export["format"]=="short_9x16" and str(item.get("transition") or "cut")!="cut":offset-=max(0.0,min(float(item.get("transition_duration") or 0),0.6))
            try:asset_id=int(item.get("asset_id"));clip_start=float(item.get("start"));clip_end=float(item.get("end"))
            except (TypeError,ValueError,AttributeError):continue
            asset=assets.get(asset_id)
            if not asset or clip_end<=clip_start:continue
            if export["format"]=="short_9x16" and item.get("show_captions") is False:
                offset+=clip_end-clip_start
                continue
            try:analysis=json.loads(asset.get("analysis") or "{}")
            except json.JSONDecodeError:analysis={}
            for caption_index,caption in enumerate(analysis.get("bilingual_captions") or [],1):
                try:start=float(caption.get("start") or 0);end=float(caption.get("end") or 0)
                except (TypeError,ValueError):continue
                if end<=clip_start or start>=clip_end:continue
                key=_caption_override_key(output_name,position,asset_id,caption_index);override=overrides.get(key) or {}
                if override.get("omit"):continue
                zh=override.get("zh",caption.get("zh") or "");ja=override.get("ja",caption.get("ja") or "")
                if clean and is_standalone_filler(zh,ja):continue
                out_start=offset+max(start,clip_start)-clip_start;out_end=offset+min(end,clip_end)-clip_start
                if override.get("output_start") is not None:out_start=float(override["output_start"])
                if override.get("output_end") is not None:out_end=float(override["output_end"])
                row={"项目ID":export["project_id"],"成片版本":export["version"],"成片格式":export["format"],"成片文件":output_name,"成片开始时间码":format_timecode(out_start),"成片结束时间码":format_timecode(out_end),"中文字幕":zh,"日文字幕":ja,"处理方式":"保留","需复核":"是" if override.get("needs_review",caption.get("needs_review")) else "否","合并片段序号":str(position),"时间线片段序号":position,"素材ID":asset_id,"素材文件名":asset["filename"],"字幕序号":caption_index,"素材开始秒":f"{start:.3f}","素材结束秒":f"{end:.3f}","成片开始秒":f"{out_start:.3f}","成片结束秒":f"{out_end:.3f}","原识别":caption.get("source") or "","_key":key,"_keys":[key],"_positions":[position],"_out_start":out_start,"_out_end":out_end}
                row["_members"]=[dict(row)]
                rows.append(row)
            offset+=clip_end-clip_start
    return _merge_export_caption_rows(rows)

def _caption_review_token(export_id,row):
    payload=json.dumps([export_id,row.get("_keys") or [row.get("_key")]],ensure_ascii=False,separators=(",",":"))
    return f"E{export_id}-{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:12].upper()}"

def _parse_review_timecode(value):
    text=str(value or "").strip();parts=text.split(":")
    if len(parts)!=3:raise ValueError("时间必须使用 HH:MM:SS.mmm 格式")
    try:hours=int(parts[0]);minutes=int(parts[1]);seconds=float(parts[2])
    except (TypeError,ValueError):raise ValueError("时间必须使用 HH:MM:SS.mmm 格式")
    if hours<0 or minutes<0 or minutes>=60 or seconds<0 or seconds>=60:raise ValueError("时间值超出有效范围")
    return hours*3600+minutes*60+seconds

def _snapshot_output_durations(export,settings):
    durations={}
    for output_name,timeline in _timeline_snapshot(export,settings).items():
        total=0.0
        for position,item in enumerate(timeline if isinstance(timeline,list) else []):
            try:length=max(0.0,float(item.get("end"))-float(item.get("start")))
            except (TypeError,ValueError,AttributeError):continue
            if position and export["format"]=="short_9x16" and str(item.get("transition") or "cut")!="cut":total-=max(0.0,min(float(item.get("transition_duration") or 0),0.6))
            total+=length
        durations[output_name]=max(0.0,total)
    return durations

@app.get("/api/exports/{export_id}/captions.xlsx")
def export_version_caption_xlsx(export_id:int):
    export=db.row("SELECT e.*,p.slug,p.settings FROM exports e JOIN projects p ON p.id=e.project_id WHERE e.id=?",(export_id,))
    if not export:raise HTTPException(404,"输出版本不存在")
    if not export.get("timeline_snapshot"):raise HTTPException(409,"该旧版本生成时尚未保存独立时间线，无法可靠导出成片级字幕；请使用最新版本或重新请求成片")
    settings=json.loads(export.get("settings") or "{}");caption_rows=_export_caption_rows(export,settings)
    rows=[{"序号":index,"成片文件":row["成片文件"],"开始时间":row["成片开始时间码"],"结束时间":row["成片结束时间码"],"中文字幕":row["中文字幕"],"日文字幕":row["日文字幕"],"处理方式":row["处理方式"],"需复核":row["需复核"],"原识别":row["原识别"],"来源素材":row["素材文件名"],"校验ID":_caption_review_token(export_id,row)} for index,row in enumerate(caption_rows,1)]
    content=build_caption_workbook(rows);filename=f"{export['slug']}-{export['version']}-成片字幕审核.xlsx"
    db.log_event(export["project_id"],"info","成片字幕复核","export_caption_workbook_exported",f"已导出 {export['version']} 成片实际使用的 {len(caption_rows)} 条字幕审核表",{"export_id":export_id,"rows":len(caption_rows)})
    return Response(content=content,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",headers={"Content-Disposition":f"attachment; filename*=UTF-8''{quote(filename)}"})

@app.get("/api/exports/{export_id}/caption-summary")
def export_caption_summary(export_id:int):
    export=db.row("SELECT e.*,p.settings FROM exports e JOIN projects p ON p.id=e.project_id WHERE e.id=?",(export_id,))
    if not export:raise HTTPException(404,"输出版本不存在")
    if not export.get("timeline_snapshot"):raise HTTPException(409,"该版本没有独立成片时间线")
    settings=json.loads(export.get("settings") or "{}");rows=_export_caption_rows(export,settings)
    flagged=[row for row in rows if row.get("需复核")=="是"]
    output_counts={}
    for row in rows:output_counts[row["成片文件"]]=output_counts.get(row["成片文件"],0)+1
    return {
        "export_id":export_id,
        "version":export["version"],
        "caption_revision":int(export.get("caption_revision") or 0),
        "total":len(rows),
        "needs_review":len(flagged),
        "outputs":[{"name":name,"captions":count} for name,count in output_counts.items()],
        "rows":[{
            "output":row["成片文件"],
            "start":row["成片开始时间码"],
            "end":row["成片结束时间码"],
            "zh":row["中文字幕"],
            "ja":row["日文字幕"],
            "source":row["原识别"],
            "asset":row["素材文件名"],
        } for row in flagged],
    }

@app.post("/api/exports/{export_id}/captions/import")
def import_version_caption_workbook(export_id:int,body:CaptionWorkbookImportIn):
    export=db.row("SELECT e.*,p.slug,p.settings FROM exports e JOIN projects p ON p.id=e.project_id WHERE e.id=?",(export_id,))
    if not export:raise HTTPException(404,"输出版本不存在")
    if not export.get("timeline_snapshot"):raise HTTPException(409,"该旧版本没有独立时间线，不能安全导入成片字幕；请从最新版本导出修正")
    control=db.control(export["project_id"]) or {}
    if control.get("desired_state") not in {"stopped","paused"}:raise HTTPException(409,"请先暂停或停止项目，再导入成片字幕")
    if not body.filename.lower().endswith(".xlsx"):raise HTTPException(400,"请导入由该版本导出的 .xlsx 字幕审核表")
    try:content=base64.b64decode(body.xlsx_base64,validate=True)
    except (binascii.Error,ValueError):raise HTTPException(400,"XLSX 文件编码无效")
    if len(content)>20*1024*1024:raise HTTPException(413,"XLSX超过20 MiB，已拒绝导入")
    try:workbook_rows=read_caption_workbook(content)
    except Exception as error:raise HTTPException(400,f"无法读取字幕审核表：{error}")
    settings=json.loads(export.get("settings") or "{}");expected_rows=_export_caption_rows(export,settings,clean=True);expected_tokens={_caption_review_token(export_id,row):row for row in expected_rows};output_durations=_snapshot_output_durations(export,settings)
    try:existing=json.loads(export.get("caption_overrides") or "{}")
    except json.JSONDecodeError:existing={}
    updates={};errors=[];changed_rows=0;seen_tokens=set()
    for row in workbook_rows:
        row_number=int(row.get("_excel_row") or 0)
        try:
            token=str(row["校验ID"] or "").strip();merged_row=expected_tokens.get(token)
            if not merged_row:raise ValueError("校验ID不属于当前成片版本，请重新导出审核表")
            if token in seen_tokens:raise ValueError("校验ID重复")
            seen_tokens.add(token)
            if str(row["成片文件"] or "").strip()!=str(merged_row["成片文件"]):raise ValueError("成片文件与校验ID不匹配")
            if str(row["原识别"] or "").strip()!=str(merged_row["原识别"] or "").strip():raise ValueError("原识别与校验ID不匹配")
            if str(row["来源素材"] or "").strip()!=str(merged_row["素材文件名"] or "").strip():raise ValueError("来源素材与校验ID不匹配")
            out_start=_parse_review_timecode(row["开始时间"]);out_end=_parse_review_timecode(row["结束时间"])
            zh=str(row["中文字幕"] or "").strip();ja=str(row["日文字幕"] or "").strip();needs_review=_review_boolean(row["需复核"],row_number)
            action=str(row.get("处理方式") or "保留").strip()
            if action not in {"保留","省略"}:raise ValueError("处理方式必须填写 保留/省略")
            omit=action=="省略" or (not zh and not ja)
            base_value={"zh":"" if omit else zh,"ja":"" if omit else ja,"omit":omit,"needs_review":False if omit else needs_review,"manual_corrected":True,"source_export_id":export_id}
            timing_changed=abs(out_start-float(merged_row["_out_start"]))>0.001 or abs(out_end-float(merged_row["_out_end"]))>0.001
            if timing_changed:
                if out_end-out_start<0.10:raise ValueError("结束时间必须至少比开始时间晚0.1秒")
                output_duration=float(output_durations.get(merged_row["成片文件"]) or 0)
                if out_end>output_duration+0.05:raise ValueError(f"结束时间超过成片时长 {format_timecode(output_duration)}")
            row_changed=omit or zh!=str(merged_row["中文字幕"] or "").strip() or ja!=str(merged_row["日文字幕"] or "").strip() or needs_review!=(merged_row["需复核"]=="是") or timing_changed
            if not row_changed:continue
            for key_index,override_key in enumerate(merged_row["_keys"]):
                if timing_changed and key_index:
                    value={**base_value,"zh":"","ja":"","omit":True,"needs_review":False,"timing_continuation_omitted":True}
                else:
                    value=dict(base_value)
                    if timing_changed:value.update({"output_start":round(out_start,3),"output_end":round(out_end,3),"timing_adjusted":True})
                updates[override_key]=value
            changed_rows+=1
        except (TypeError,ValueError,KeyError) as error:errors.append(f"第 {row_number} 行：{error}")
        if len(errors)>=30:break
    missing_rows=len(expected_tokens)-len(seen_tokens)
    if missing_rows:errors.append(f"审核表缺少 {missing_rows} 条字幕，请勿删除行")
    if errors:raise HTTPException(400,"XLSX校验失败，未保存任何修改："+"；".join(errors))
    if not updates:return {"ok":True,"changed_rows":0,"message":"XLSX与该成片版本字幕一致，没有需要保存的修改"}
    changed_segments=len(updates)
    existing.update(updates)
    if export["status"]=="caption_review_ready" and export.get("master_manifest") not in {None,"","{}"}:
        revision=int(export.get("caption_revision") or 0)+1;stamp=db.now()
        backup_dir=PROJECTS/export["slug"]/"caption-version-backups"/f"export-{export_id}";backup_dir.mkdir(parents=True,exist_ok=True)
        backup_name=f"revision-{revision:03d}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        (backup_dir/backup_name).write_text(json.dumps({"export_id":export_id,"version":export["version"],"created_at":stamp,"source_file":body.filename,"previous_caption_revision":int(export.get("caption_revision") or 0),"caption_overrides":json.loads(export.get("caption_overrides") or "{}")},ensure_ascii=False,indent=2),encoding="utf-8")
        db.execute("UPDATE exports SET caption_overrides=?,caption_revision=? WHERE id=?",(json.dumps(existing,ensure_ascii=False),revision,export_id))
        db.log_event(export["project_id"],"success","成片字幕校对","caption_workbook_imported",f"{export['version']} 已完成第 {revision} 轮字幕与时间点校对；母版未重渲染",{"export_id":export_id,"filename":body.filename,"changed_rows":changed_rows,"changed_segments":changed_segments,"caption_revision":revision,"backup":str(backup_dir/backup_name)})
        return {"ok":True,"changed_rows":changed_rows,"changed_segments":changed_segments,"export_id":export_id,"version":export["version"],"status":"caption_review_ready","caption_revision":revision,"same_version":True,"backup":backup_name}
    numbers=[int(value["version"][1:]) for value in db.rows("SELECT version FROM exports WHERE project_id=?",(export["project_id"],)) if value["version"].startswith("v") and value["version"][1:].isdigit()];version=f"v{max(numbers,default=0)+1}";snapshot=json.dumps(_timeline_snapshot(export,settings),ensure_ascii=False)
    new_id=db.execute("INSERT INTO exports(project_id,version,format,status,timeline_snapshot,caption_overrides,render_options,source_export_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(export["project_id"],version,export["format"],"render_requested",snapshot,json.dumps(existing,ensure_ascii=False),export.get("render_options") or "{}",export_id,db.now()))
    db.execute("UPDATE projects SET status='render_requested',updated_at=? WHERE id=?",(db.now(),export["project_id"]));db.set_progress(export["project_id"],"render_requested","成片字幕重渲染",f"{version} 等待启动")
    db.log_event(export["project_id"],"success","成片字幕复核","caption_workbook_version_created",f"已导入 {changed_rows} 条字幕/时间点修正并创建 {version}；请点击启动项目重渲染",{"source_export_id":export_id,"new_export_id":new_id,"filename":body.filename,"changed_rows":changed_rows,"changed_segments":changed_segments})
    return {"ok":True,"changed_rows":changed_rows,"changed_segments":changed_segments,"new_export_id":new_id,"version":version,"status":"render_requested"}

def _review_boolean(value,row_number):
    normalized=str(value or "").strip().lower()
    if normalized in {"1","true","yes","y","是","需要","需复核"}:return True
    if normalized in {"0","false","no","n","否","不需要","已修正"}:return False
    raise ValueError(f"第 {row_number} 行“需复核”必须填写 是/否")

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

def _master_outputs(export):
    try:manifest=json.loads(export.get("master_manifest") or "{}")
    except json.JSONDecodeError:manifest={}
    outputs=manifest.get("outputs") if isinstance(manifest,dict) else None
    return outputs if isinstance(outputs,list) else []

def _discard_master_files(export):
    project_root=(PROJECTS/export["slug"]).resolve()
    master_root=(project_root/"masters"/str(export["id"])).resolve()
    if not master_root.is_relative_to(project_root) or master_root==project_root:raise HTTPException(400,"版本母版路径校验失败，拒绝删除")
    if not master_root.exists():return 0,0
    files=[value for value in master_root.rglob("*") if value.is_file()]
    released=sum(value.stat().st_size for value in files);shutil.rmtree(master_root)
    return len(files),released

@app.get("/api/exports/{export_id}/masters/{file_index}")
def export_master_file(export_id:int,file_index:int):
    export=db.row("SELECT master_manifest FROM exports WHERE id=?",(export_id,))
    if not export:raise HTTPException(404,"输出版本不存在")
    outputs=_master_outputs(export)
    if file_index<0 or file_index>=len(outputs):raise HTTPException(404,"母版不存在")
    path=Path(str(outputs[file_index].get("privacy") or "")).resolve()
    if not path.is_relative_to(PROJECTS.resolve()) or not path.is_file():raise HTTPException(404,"马赛克母版不存在")
    return FileResponse(path,media_type="video/mp4",filename=path.name)

@app.get("/api/exports/{export_id}/captions/{file_index}.vtt")
def export_caption_vtt(export_id:int,file_index:int):
    export=db.row("SELECT e.*,p.settings FROM exports e JOIN projects p ON p.id=e.project_id WHERE e.id=?",(export_id,))
    if not export:raise HTTPException(404,"输出版本不存在")
    outputs=_master_outputs(export)
    if file_index<0 or file_index>=len(outputs):raise HTTPException(404,"母版不存在")
    output_name=str(outputs[file_index].get("name") or "")
    settings=json.loads(export.get("settings") or "{}");rows=[row for row in _export_caption_rows(export,settings) if row["成片文件"]==output_name]
    lines=["WEBVTT",""]
    for index,row in enumerate(rows,1):
        text="\n".join(value for value in (str(row.get("中文字幕") or "").strip(),str(row.get("日文字幕") or "").strip()) if value)
        if not text:continue
        lines.extend([str(index),f"{row['成片开始时间码'].replace(',','.')} --> {row['成片结束时间码'].replace(',','.')}",text,""])
    return Response(content="\n".join(lines),media_type="text/vtt; charset=utf-8",headers={"Cache-Control":"no-store"})

@app.post("/api/exports/{export_id}/captions/lock")
def lock_export_captions(export_id:int):
    export=db.row("SELECT * FROM exports WHERE id=?",(export_id,))
    if not export:raise HTTPException(404,"输出版本不存在")
    if export["status"]!="caption_review_ready":raise HTTPException(409,"该版本当前不在字幕校对阶段")
    control=db.control(export["project_id"]) or {}
    if control.get("desired_state") not in {"stopped","paused"}:raise HTTPException(409,"请先暂停或停止项目，再锁定字幕")
    if not _master_outputs(export):raise HTTPException(409,"该版本没有可复用的马赛克母版")
    stamp=db.now();db.execute("UPDATE exports SET status='subtitle_render_requested',caption_locked_at=? WHERE id=?",(stamp,export_id))
    db.execute("UPDATE projects SET status='subtitle_render_requested',error=NULL,updated_at=? WHERE id=?",(stamp,export["project_id"]))
    db.create_control(export["project_id"],"running","subtitle_render_requested","字幕快速生成",f"{export['version']} 字幕已锁定，准备生成",render_scope=export["format"])
    db.log_event(export["project_id"],"success","成片字幕校对","captions_locked",f"{export['version']} 第 {int(export.get('caption_revision') or 0)} 轮字幕已锁定，开始快速生成成片",{"export_id":export_id,"caption_revision":int(export.get("caption_revision") or 0)})
    return {"ok":True,"export_id":export_id,"version":export["version"],"status":"subtitle_render_requested","caption_revision":int(export.get("caption_revision") or 0)}

@app.post("/api/exports/{export_id}/master/discard")
def discard_export_master(export_id:int):
    export=db.row("SELECT e.*,p.slug FROM exports e JOIN projects p ON p.id=e.project_id WHERE e.id=?",(export_id,))
    if not export:raise HTTPException(404,"输出版本不存在")
    if export["status"]!="caption_review_ready":raise HTTPException(409,"只有处于母版审核/字幕校对阶段的版本才能废弃母版")
    control=db.control(export["project_id"]) or {}
    if control.get("desired_state") not in {"stopped","paused"}:raise HTTPException(409,"请先暂停或停止项目，再废弃母版")
    if not _master_outputs(export):raise HTTPException(409,"该版本没有可废弃的母版")
    deleted_files,released=_discard_master_files(export)
    stamp=db.now()
    db.execute("UPDATE exports SET status='render_requested',path=NULL,master_manifest='{}',render_mode='full',caption_locked_at=NULL WHERE id=?",(export_id,))
    db.execute("UPDATE projects SET status='render_requested',error=NULL,updated_at=? WHERE id=?",(stamp,export["project_id"]))
    db.create_control(export["project_id"],"stopped","render_requested","成片渲染",f"{export['version']} 母版已废弃，等待重新渲染",render_scope=export["format"])
    db.log_event(export["project_id"],"warning","母版审核","master_discarded_for_rerender",f"{export['version']} 当前母版已废弃；时间线和第 {int(export.get('caption_revision') or 0)} 轮字幕修正已保留",{"export_id":export_id,"format":export["format"],"deleted_files":deleted_files,"released_bytes":released,"caption_revision":int(export.get("caption_revision") or 0)})
    return {"ok":True,"export_id":export_id,"version":export["version"],"status":"render_requested","deleted_files":deleted_files,"released_bytes":released,"caption_revision":int(export.get("caption_revision") or 0)}

@app.post("/api/exports/{export_id}/revisions/apply")
def apply_version_revisions(export_id:int,body:RevisionApplyIn):
    export=db.row("SELECT e.*,p.slug,p.settings FROM exports e JOIN projects p ON p.id=e.project_id WHERE e.id=?",(export_id,))
    if not export:raise HTTPException(404,"输出版本不存在")
    if export["status"]!="caption_review_ready":raise HTTPException(409,"只有处于母版审核/字幕校对阶段的版本才能应用版本意见")
    control=db.control(export["project_id"]) or {}
    if control.get("desired_state") not in {"stopped","paused"}:raise HTTPException(409,"请先暂停或停止项目，再应用版本意见")
    revisions=db.rows("SELECT id,kind,body,source_version FROM revisions WHERE project_id=? AND source_export_id=? AND status='open' AND kind!='privacy' ORDER BY id",(export["project_id"],export_id))
    if not revisions:raise HTTPException(409,f"请先在“审核修改意见”中选择 {export['version']} 并至少提交一条意见")
    mode=revision_mode(revisions);revision_ids=[int(value["id"]) for value in revisions]
    if mode=="incremental":
        try:result=create_inherited_revision(export["project_id"],export_id,revisions,revision_ids=revision_ids)
        except ValueError as exc:
            db.log_event(export["project_id"],"warning","应用修改意见","revision_apply_rejected",f"{export['version']} 的局部修改未创建新版本：{exc}",{"export_id":export_id,"version":export["version"],"revision_ids":revision_ids,"error":str(exc)})
            raise HTTPException(409,str(exc)) from exc
        return {**result,"revision_ids":revision_ids}
    parsed=_parse_revision(revisions,export)
    if parsed.get("has_local_timeline_edits"):
        message="检测到明确的成片删除或插入时间码，不能执行完整重规划。请删除这条完整重规划意见，并改用‘局部剪辑调整’"
        db.log_event(export["project_id"],"warning","完整重规划","local_timecode_replan_blocked",f"{export['version']} 已阻止误用完整重规划",{"export_id":export_id,"version":export["version"],"revision_ids":revision_ids,"output_deletions":parsed.get("output_deletions") or [],"insertions":parsed.get("insertions") or []})
        raise HTTPException(409,message)
    if not body.confirm_full_replan:
        raise HTTPException(409,"完整重规划必须在页面二次确认；来源版本将保留，不会自动删除")
    try:settings=json.loads(export.get("settings") or "{}")
    except json.JSONDecodeError:settings={}
    stamp=db.now();request={"source_export_id":export_id,"source_version":export["version"],"format":export["format"],"revision_ids":revision_ids,"requested_at":stamp,"mode":"full_replan"}
    settings["replan_request"]=request
    db.execute("UPDATE exports SET status='replan_requested' WHERE id=?",(export_id,))
    db.execute("UPDATE projects SET status='revision_requested',settings=?,error=NULL,updated_at=? WHERE id=?",(json.dumps(settings,ensure_ascii=False),stamp,export["project_id"]))
    db.create_control(export["project_id"],"stopped","revision_requested","完整重规划",f"{export['version']} 来源母版已保留，待读取 {len(revisions)} 条版本意见",render_scope=export["format"])
    db.log_event(export["project_id"],"warning","完整重规划","full_replan_requested",f"{export['version']} 已明确请求完整重规划；来源时间线和母版保留到新版本建立后",{"export_id":export_id,"format":export["format"],"revision_ids":revision_ids,"source_master_preserved":True})
    return {"ok":True,"mode":"full_replan","export_id":export_id,"version":export["version"],"status":"replan_requested","revision_ids":revision_ids,"source_master_preserved":True}

@app.delete("/api/exports/{export_id}")
def delete_export(export_id:int):
    export=db.row("SELECT e.*,p.status AS project_status,p.slug,p.settings AS project_settings FROM exports e JOIN projects p ON p.id=e.project_id WHERE e.id=?",(export_id,))
    if not export:raise HTTPException(404,"输出版本不存在")
    control=db.control(export["project_id"])
    if control["desired_state"] not in {"stopped","paused"}:raise HTTPException(409,"请先暂停或停止项目再删除历史版本")
    if export["status"]=="rendering":raise HTTPException(409,"该版本正在渲染，暂时不能删除")
    paths=[]
    if export.get("path"):
        try:paths=json.loads(export["path"])
        except json.JSONDecodeError:paths=[export["path"]]
    deleted=[];released=0;output_root=OUTPUTS.resolve()
    for value in paths:
        target=Path(value).resolve()
        if not target.is_relative_to(output_root):raise HTTPException(400,"输出文件路径校验失败，拒绝删除")
        for candidate in (target,target.with_suffix('.zh-ja.srt')):
            if candidate.exists() and candidate.is_file():released+=candidate.stat().st_size;candidate.unlink();deleted.append(str(candidate))
    for folder in (PROJECTS/export["slug"]/"masters"/str(export_id),PROJECTS/export["slug"]/"caption-version-backups"/f"export-{export_id}"):
        resolved=folder.resolve();project_root=(PROJECTS/export["slug"]).resolve()
        if not resolved.is_relative_to(project_root) or resolved==project_root:raise HTTPException(400,"版本母版路径校验失败，拒绝删除")
        if resolved.exists():
            released+=sum(value.stat().st_size for value in resolved.rglob("*") if value.is_file())
            deleted.extend(str(value) for value in resolved.rglob("*") if value.is_file());shutil.rmtree(resolved)
    db.execute("UPDATE exports SET source_export_id=NULL WHERE source_export_id=?",(export_id,))
    db.execute("DELETE FROM exports WHERE id=?",(export_id,))
    remaining=db.row("SELECT id FROM exports WHERE project_id=? AND status IN ('render_requested','rendering') LIMIT 1",(export["project_id"],))
    try:project_settings=json.loads(export.get("project_settings") or "{}")
    except json.JSONDecodeError:project_settings={}
    replan_request=project_settings.get("replan_request")
    has_replan_request=isinstance(replan_request,dict) and bool(replan_request)
    if not remaining and not has_replan_request and export["project_status"] in {"render_requested","rendering"}:
        next_status="review_ready" if db.row("SELECT id FROM exports WHERE project_id=? AND status IN ('review_ready','approved') AND path IS NOT NULL AND path!='[]' LIMIT 1",(export["project_id"],)) else "draft_ready"
        db.execute("UPDATE projects SET status=?,updated_at=? WHERE id=?",(next_status,db.now(),export["project_id"]))
        db.create_control(export["project_id"],"stopped",None,"人工审核","没有待渲染版本",render_scope=None)
    db.log_event(export["project_id"],"warning","输出版本","export_deleted",f"已删除 {export['version']} · {export['format']}",{"status":export["status"],"deleted_files":deleted,"released_bytes":released})
    return {"ok":True,"deleted_files":len(deleted),"released_bytes":released}
@app.put("/api/projects/{project_id}")
def update_project(project_id:int,body:SettingsIn):
    project=db.row("SELECT settings FROM projects WHERE id=?",(project_id,))
    if not project:raise HTTPException(404,"项目不存在")
    try:current=json.loads(project.get("settings") or "{}")
    except json.JSONDecodeError:current={}
    incoming=body.settings if isinstance(body.settings,dict) else {}
    protected={"story_plan","story_model","replan_request"}
    merged=dict(current)
    merged.update({key:value for key,value in incoming.items() if key not in protected})
    preserved=sorted(key for key in protected if key in incoming and incoming.get(key)!=current.get(key))
    db.execute("UPDATE projects SET mode=?,notes=?,settings=?,updated_at=? WHERE id=?",(body.mode,body.notes,json.dumps(merged,ensure_ascii=False),db.now(),project_id))
    db.log_event(project_id,"info","项目设置","settings_updated","制作设定已保存",{"preserved_internal_keys":preserved})
    return db.project_detail(project_id)

@app.post("/api/projects/{project_id}/control/{action}")
def control_project(project_id:int,action:str):
    project=db.row("SELECT * FROM projects WHERE id=?",(project_id,))
    if not project:raise HTTPException(404,"项目不存在")
    current=db.control(project_id)
    desired=current["desired_state"]
    resume=current.get("resume_status") or db.resume_status_for(project["status"])
    stage=current.get("stage") or db.stage_for(project["status"])
    if project["status"]=="waiting_start":resume="ready_for_audio"
    if action in {"start","continue"}:
        expected="stopped" if action=="start" else "paused"
        if desired!=expected:raise HTTPException(409,f"项目当前状态不能{('启动' if action=='start' else '继续')}")
        if not resume:raise HTTPException(409,"当前没有可执行的后续阶段")
        if resume=="revision_requested":
            try:settings=json.loads(project.get("settings") or "{}")
            except json.JSONDecodeError:settings={}
            request=settings.get("replan_request") if isinstance(settings.get("replan_request"),dict) else None
            project_revision=db.row("SELECT id FROM revisions WHERE project_id=? AND status='open' AND source_export_id IS NULL AND kind!='privacy' LIMIT 1",(project_id,))
            if not request and not project_revision:
                version_revisions=db.rows("SELECT DISTINCT source_version FROM revisions WHERE project_id=? AND status='open' AND source_export_id IS NOT NULL AND kind!='privacy' ORDER BY source_version",(project_id,))
                if version_revisions:
                    labels="、".join(str(value.get("source_version") or "该版本") for value in version_revisions)
                    db.log_event(project_id,"warning","修改剪辑方案","version_replan_not_requested",f"仅存在版本级意见：{labels}；已阻止误启动项目级重规划",{"versions":[value.get("source_version") for value in version_revisions]})
                    raise HTTPException(409,f"当前只有 {labels} 的版本级意见。请在该版本点击“应用修改意见”；版本意见默认继承来源时间线，不会直接执行项目级重规划")
        required_worker=db.required_worker_for_status(resume)
        if required_worker and not db.worker_online(required_worker):
            label=db.WORKERS[required_worker]
            db.log_event(project_id,"error","工作器检查","worker_offline",f"{label}工作器离线，已阻止项目假运行",{"worker":required_worker,"resume_status":resume})
            raise HTTPException(503,f"{label}工作器离线。请运行 scripts\\start.ps1 启动完整 Vlog 服务后重试")
        free_gib=shutil.disk_usage(INBOX).free/1024**3
        if free_gib<MIN_FREE_GIB:
            db.log_event(project_id,"error","磁盘保护","low_disk",f"D盘仅剩 {free_gib:.1f} GiB，低于 {MIN_FREE_GIB} GiB 安全线，已阻止启动")
            raise HTTPException(507,f"D盘空间不足：{free_gib:.1f} GiB；至少需要 {MIN_FREE_GIB} GiB")
        if resume=="render_requested":
            if not db.row("SELECT id FROM exports WHERE project_id=? AND status='render_requested'",(project_id,)):
                failed=db.row("SELECT id FROM exports WHERE project_id=? AND status='render_failed' ORDER BY id DESC LIMIT 1",(project_id,))
                if failed:db.execute("UPDATE exports SET status='render_requested' WHERE id=?",(failed["id"],))
                else:raise HTTPException(409,"没有可继续的渲染任务")
        # A version-bound replan finishes in the stopped state with the intended
        # format stored as its scope.  Preserve that scope for both Start and
        # Continue so an older pending export cannot run before the rebuilt one.
        next_scope=current.get("render_scope")
        db.create_control(project_id,"running",resume,db.stage_for(resume),None,render_scope=next_scope)
        db.execute("UPDATE projects SET status=?,error=NULL,updated_at=? WHERE id=?",(resume,db.now(),project_id))
        verb="启动" if action=="start" else "继续"
        scope_label={"long_16x9":"长篇","short_9x16":"短篇"}.get(next_scope)
        message=f"项目已{verb}，将从 {db.stage_for(resume)} 继续"
        if scope_label:message+=f"（仅{scope_label}）"
        db.log_event(project_id,"info",db.stage_for(resume),action,message,{"resume_status":resume,"render_scope":next_scope})
    elif action in {"pause","stop"}:
        if desired not in {"running","pause_requested","paused"}:raise HTTPException(409,"项目当前没有正在运行或暂停的任务")
        if not resume:resume=db.resume_status_for(project["status"])
        if not resume:raise HTTPException(409,"当前阶段无需暂停或停止")
        active=project["status"] in db.ACTIVE_STATUSES
        requested="pause_requested" if action=="pause" else "stop_requested"
        final="paused" if action=="pause" else "stopped"
        next_state=requested if active else final
        db.create_control(project_id,next_state,resume,stage,current.get("item"))
        verb="暂停" if action=="pause" else "停止"
        if active:
            db.log_event(project_id,"warning",stage,requested,f"已请求{verb}，将在当前素材处理完成后生效",{"item":current.get("item")})
        else:
            db.log_event(project_id,"warning",stage,final,f"项目已在 {stage} 安全{verb}",{"item":current.get("item")})
    else:raise HTTPException(400,"未知控制操作")
    return db.project_detail(project_id)
@app.post("/api/projects/{project_id}/revisions/preview")
def preview_revision(project_id:int,body:RevisionIn):
    if not db.row("SELECT id FROM projects WHERE id=?",(project_id,)):raise HTTPException(404,"项目不存在")
    source=None
    if body.source_export_id is not None:
        source=db.row("SELECT id,format,timeline_snapshot FROM exports WHERE id=? AND project_id=?",(body.source_export_id,project_id))
        if not source:raise HTTPException(404,"关联版本不存在或不属于当前项目")
    return _parse_revision([{"kind":body.kind,"body":body.body}],source)

@app.post("/api/projects/{project_id}/revisions")
def add_revision(project_id:int,body:RevisionIn):
    project=db.row("SELECT id FROM projects WHERE id=?",(project_id,))
    if not project:raise HTTPException(404,"项目不存在")
    control=db.control(project_id) or {}
    if control.get("desired_state") not in {"stopped","paused"}:raise HTTPException(409,"请先暂停或停止项目，再提交修改意见")
    if body.kind not in {"edit","shot","duration","style","audio","privacy","full_replan"}:raise HTTPException(400,"未知意见类型")
    source=None
    if body.source_export_id is not None:
        source=db.row("SELECT id,version,format,status,timeline_snapshot,master_manifest FROM exports WHERE id=? AND project_id=?",(body.source_export_id,project_id))
        if not source:raise HTTPException(404,"关联版本不存在或不属于当前项目")
    if body.kind=="privacy" and not source:raise HTTPException(400,"马赛克校对必须关联一个已有母版版本")
    if body.kind in {"audio","full_replan"} and not source:raise HTTPException(400,"音频精调和完整重规划必须关联一个已有母版版本")
    parsed=_parse_revision([{"kind":body.kind,"body":body.body}],source)
    if body.kind=="full_replan" and parsed.get("has_local_timeline_edits"):
        raise HTTPException(409,"检测到明确的成片删除或插入时间码。请选择‘局部剪辑调整’，系统会继承当前版本并只修改指定位置")
    if body.kind=="privacy":
        rules=parsed.get("privacy_rules") or {}
        if not rules.get("force_cover") and not rules.get("suppress"):raise HTTPException(400,"没有解析到有效的成片时间段；请填写如：22.1秒到22.8秒，右侧人物加马赛克")
        if source.get("status")!="caption_review_ready" or not _master_outputs(source):raise HTTPException(409,"马赛克校对只能关联处于母版审核阶段的版本")
    if source and body.kind not in {"privacy","full_replan"}:
        pending=db.rows("SELECT id,kind,body FROM revisions WHERE project_id=? AND source_export_id=? AND status='open' AND kind!='privacy' ORDER BY id",(project_id,source["id"]))
        candidate={"kind":body.kind,"body":body.body}
        combined=[*pending,candidate]
        if revision_mode(combined)=="full_replan":
            raise HTTPException(409,"该版本已有完整重规划意见；请先处理或删除它，再提交局部修改")
        try:validate_inherited_revision(project_id,source["id"],combined)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    stamp=db.now();rid=db.execute(
        "INSERT INTO revisions(project_id,kind,body,source_export_id,source_version,created_at) VALUES(?,?,?,?,?,?)",
        (project_id,body.kind,body.body,source["id"] if source else None,source["version"] if source else None,stamp),
    )
    label=f"{source['version']} 的版本意见" if source else "项目级意见"
    if source:
        waiting="apply_privacy" if body.kind=="privacy" else "apply_revision"
        action="点击该版本的‘应用马赛克意见’" if body.kind=="privacy" else "点击该版本的‘应用修改意见’"
        db.log_event(project_id,"info","人工审核","revision_added",f"收到{body.kind}{label}；请{action}后应用",{"revision_id":rid,"body":body.body,"source_export_id":source["id"],"source_version":source["version"]})
        return {"id":rid,"ok":True,"source_export_id":source["id"],"source_version":source["version"],"waiting_for":waiting,"parsed_intent":parsed}
    db.execute("UPDATE projects SET status='revision_requested',updated_at=? WHERE id=?",(stamp,project_id))
    db.create_control(project_id,"stopped","revision_requested","修改剪辑方案",f"{label}已记录，等待启动",render_scope=None)
    db.set_progress(project_id,"revision_requested","修改剪辑方案",label)
    db.log_event(project_id,"info","人工审核","revision_added",f"收到{body.kind}{label}",{"revision_id":rid,"body":body.body,"source_export_id":None,"source_version":None})
    return {"id":rid,"ok":True,"source_export_id":None,"source_version":None,"parsed_intent":parsed}

@app.post("/api/exports/{export_id}/privacy-revisions/apply")
def apply_privacy_revisions(export_id:int):
    export=db.row("SELECT e.*,p.slug FROM exports e JOIN projects p ON p.id=e.project_id WHERE e.id=?",(export_id,))
    if not export:raise HTTPException(404,"输出版本不存在")
    if export.get("status")!="caption_review_ready":raise HTTPException(409,"只有处于母版审核阶段的版本才能应用马赛克意见")
    control=db.control(export["project_id"]) or {}
    if control.get("desired_state") not in {"stopped","paused"}:raise HTTPException(409,"请先暂停或停止项目，再应用马赛克意见")
    revisions=db.rows("SELECT id,kind,body FROM revisions WHERE project_id=? AND source_export_id=? AND status='open' AND kind='privacy' ORDER BY id",(export["project_id"],export_id))
    if not revisions:raise HTTPException(409,f"{export['version']} 没有待应用的马赛克校对意见")
    parsed=parse_privacy_intent(revisions);rules=parsed.get("privacy_rules") or {}
    if not rules.get("force_cover") and not rules.get("suppress"):raise HTTPException(409,"待应用意见没有有效的成片马赛克时间段")
    outputs=_master_outputs(export)
    if not outputs:raise HTTPException(409,"来源版本没有可复用的母版")
    for output in outputs:
        edit=Path(str(output.get("edit") or "")).resolve()
        if not edit.is_relative_to(PROJECTS.resolve()) or not edit.is_file():raise HTTPException(409,f"来源版本的无字幕剪辑母版不存在：{output.get('name') or '成片'}")
    try:result=create_privacy_revision(export["project_id"],export_id,rules,parsed.get("text") or "人工马赛克校对",revision_ids=[value["id"] for value in revisions])
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    return {"ok":True,**result}

@app.delete("/api/projects/{project_id}/revisions/{revision_id}")
def delete_revision(project_id:int,revision_id:int):
    project=db.row("SELECT id,status,settings FROM projects WHERE id=?",(project_id,))
    if not project:raise HTTPException(404,"项目不存在")
    control=db.control(project_id) or {}
    if control.get("desired_state") not in {"stopped","paused"}:raise HTTPException(409,"请先暂停或停止项目，再删除规划意见")
    revision=db.row("SELECT * FROM revisions WHERE id=? AND project_id=?",(revision_id,project_id))
    if not revision:raise HTTPException(404,"规划意见不存在")
    try:settings=json.loads(project.get("settings") or "{}")
    except json.JSONDecodeError:settings={}
    request=settings.get("replan_request") if isinstance(settings.get("replan_request"),dict) else {}
    locked_ids={int(value) for value in request.get("revision_ids") or [] if str(value).isdigit()}
    if revision["status"]=="open" and revision_id in locked_ids:
        raise HTTPException(409,"这条意见已锁定到等待执行的版本重规划；请先完成或取消该重规划")
    db.execute("DELETE FROM revisions WHERE id=?",(revision_id,))
    remaining_project_open=db.row("SELECT id FROM revisions WHERE project_id=? AND status='open' AND source_export_id IS NULL LIMIT 1",(project_id,))
    if revision["status"]=="open" and revision.get("source_export_id") is None and not remaining_project_open and project["status"]=="revision_requested":
        stamp=db.now();db.execute("UPDATE projects SET status='draft_ready',updated_at=? WHERE id=?",(stamp,project_id));db.create_control(project_id,"stopped",None,"等待选择版本","已删除最后一条待应用的项目级意见")
    db.log_event(project_id,"warning","剪辑方案重规划","revision_deleted",f"已删除规划意见 #{revision_id}",{"revision_id":revision_id,"status":revision["status"],"source_version":revision.get("source_version"),"applied_version":revision.get("applied_version")})
    return {"ok":True,"id":revision_id,"status":revision["status"]}

def _locked_short_export(project_id:int):
    return db.row("""SELECT id,version,status FROM exports
                     WHERE project_id=? AND format='short_9x16' AND locked=1 AND status='approved'
                     ORDER BY id DESC LIMIT 1""",(project_id,))

def _pending_version_revisions(project_id:int,formats):
    formats=tuple(value for value in formats if value in {"long_16x9","short_9x16"})
    if not formats:return []
    placeholders=",".join("?" for _ in formats)
    return db.rows(
        f"""SELECT r.id,r.kind,r.source_version,e.format
              FROM revisions r JOIN exports e ON e.id=r.source_export_id
             WHERE r.project_id=? AND r.status='open' AND r.kind!='privacy'
               AND e.format IN ({placeholders})
             ORDER BY r.id""",
        (project_id,*formats),
    )

def _raise_if_pending_version_revisions(project_id:int,formats,requested_format=None):
    pending=_pending_version_revisions(project_id,formats)
    if not pending:return
    grouped={}
    for value in pending:grouped.setdefault(value["source_version"],set()).add(value["format"])
    labels="、".join(f"{version} · {'长篇' if 'long_16x9' in fmts else '短篇'}" for version,fmts in grouped.items())
    db.log_event(project_id,"warning","成片渲染","pending_revision_render_blocked",f"存在待应用版本意见，已阻止直接渲染：{labels}",{"requested_format":requested_format,"revisions":pending})
    raise HTTPException(409,f"{labels} 存在待应用意见。请先在该版本点击“应用修改意见”；马赛克与字幕快速修正不受影响")

def _create_export(project_id:int,fmt:str):
    if fmt not in ("long_16x9","short_9x16"):raise HTTPException(400,"未知格式")
    project=db.row("SELECT settings FROM projects WHERE id=?",(project_id,))
    if not project:raise HTTPException(404,"项目不存在")
    _raise_if_pending_version_revisions(project_id,(fmt,),fmt)
    if fmt=="short_9x16":
        locked=_locked_short_export(project_id)
        if locked:raise HTTPException(409,f"短篇 {locked['version']} 已批准锁定；请先取消锁定，再生成新的短篇版本")
    settings=json.loads(project.get("settings") or "{}");plan=settings.get("story_plan")
    if not plan:raise HTTPException(409,"剪辑方案尚未生成，不能请求导出")
    numbers=[int(value["version"][1:]) for value in db.rows("SELECT version FROM exports WHERE project_id=?",(project_id,)) if value["version"].startswith("v") and value["version"][1:].isdigit()]
    version=f"v{max(numbers,default=0)+1}";snapshot,render_options=_requested_snapshot(project_id,version,fmt,settings)
    eid=db.execute("INSERT INTO exports(project_id,version,format,status,timeline_snapshot,render_options,created_at) VALUES(?,?,?,?,?,?,?)",(project_id,version,fmt,"render_requested",json.dumps(snapshot,ensure_ascii=False),json.dumps(render_options,ensure_ascii=False),db.now()))
    db.execute("UPDATE projects SET status='render_requested',updated_at=? WHERE id=?",(db.now(),project_id));db.set_progress(project_id,"render_requested","成片渲染")
    details={"format":fmt,"outputs":len(snapshot)}
    if render_options:details.update(render_options)
    db.log_event(project_id,"info","成片渲染","export_requested",f"已请求 {version} · {fmt}",details);return {"id":eid,"version":version,"status":"render_requested"}

@app.post("/api/projects/{project_id}/generate/{fmt}")
def generate_export(project_id:int,fmt:str):
    if fmt not in ("long_16x9","short_9x16","both"):raise HTTPException(400,"未知格式")
    project=db.row("SELECT id,status,settings FROM projects WHERE id=?",(project_id,))
    if not project:raise HTTPException(404,"项目不存在")
    requested_formats=("long_16x9","short_9x16") if fmt=="both" else (fmt,)
    _raise_if_pending_version_revisions(project_id,requested_formats,fmt)
    if fmt in {"short_9x16","both"}:
        locked=_locked_short_export(project_id)
        if locked:
            db.log_event(project_id,"warning","成片渲染","locked_short_render_blocked",f"短篇 {locked['version']} 已批准锁定，已阻止新的短篇渲染请求",{"format":fmt,"export_id":locked["id"],"version":locked["version"]})
            raise HTTPException(409,f"短篇 {locked['version']} 已批准锁定；请先点击“取消锁定”，再生成新的短篇版本")
    control=db.control(project_id) or {}
    if control.get("desired_state") not in {"stopped","paused"}:raise HTTPException(409,"项目正在运行，不能同时启动另一个版本")
    free_gib=shutil.disk_usage(INBOX).free/1024**3
    if free_gib<MIN_FREE_GIB:raise HTTPException(507,f"D盘空间不足：{free_gib:.1f} GiB；至少需要 {MIN_FREE_GIB} GiB")
    settings=json.loads(project.get("settings") or "{}")
    replan_request=settings.get("replan_request") if isinstance(settings.get("replan_request"),dict) else None
    if replan_request:
        source_version=str(replan_request.get("source_version") or "该版本")
        db.log_event(project_id,"warning","修改剪辑方案","replan_render_blocked",f"{source_version} 正在等待版本重规划，已阻止直接请求成片",{"format":fmt,"source_export_id":replan_request.get("source_export_id")})
        raise HTTPException(409,f"{source_version} 已废弃并等待重规划；请先点击“启动项目”完成方案重建，再渲染新版本")
    if fmt=="both":
        continuable_statuses=("render_requested","rendering","render_failed","subtitle_render_requested","subtitle_rendering","subtitle_render_failed","scheduled")
        placeholders=",".join("?" for _ in continuable_statuses)
        continuable=db.rows(f"SELECT version,format,status FROM exports WHERE project_id=? AND status IN ({placeholders}) ORDER BY id",(project_id,*continuable_statuses))
        if continuable:
            labels="、".join(f"{value['version']} · {'长篇' if value['format']=='long_16x9' else '短篇'}" for value in continuable)
            db.log_event(project_id,"warning","成片渲染","combined_render_blocked",f"存在待继续任务，已阻止长短篇一起渲染：{labels}",{"format":fmt,"continuable_exports":continuable})
            raise HTTPException(409,f"已有待继续的渲染任务：{labels}。请单独继续、单独渲染，或先删除/废弃该任务后再选择长短篇一起渲染")
    formats=("long_16x9","short_9x16") if fmt=="both" else (fmt,)
    pending_by_format={value:db.row("SELECT id,version,status,render_options FROM exports WHERE project_id=? AND format=? AND status IN ('render_requested','render_failed') ORDER BY id LIMIT 1",(project_id,value)) for value in formats}
    pending_short=pending_by_format.get("short_9x16")
    short_bgm=settings.get("short_bgm") if isinstance(settings.get("short_bgm"),dict) else {}
    requested_bgm=str(short_bgm.get("filename") or "").strip()
    if pending_short and pending_short["status"]=="render_requested":
        try:existing_options=json.loads(pending_short.get("render_options") or "{}")
        except json.JSONDecodeError:existing_options={}
        existing_bgm=str(existing_options.get("bgm_filename") or "").strip()
        if requested_bgm!=existing_bgm:
            try:
                fallback_snapshot=json.loads(db.row("SELECT timeline_snapshot FROM exports WHERE id=?",(pending_short["id"],)).get("timeline_snapshot") or "{}")
            except (AttributeError,TypeError,json.JSONDecodeError):
                fallback_snapshot={}
            snapshot,bgm_options=_requested_snapshot(project_id,pending_short["version"],"short_9x16",settings,fallback_snapshot)
            for key in ("bgm_filename","bgm_duration","short_style_profile","short_voice_mode","short_voice_reason","short_voice_clips","short_flash_bursts","caption_policy","snapshot_fallback"):existing_options.pop(key,None)
            existing_options.update(bgm_options)
            if bgm_options.get("snapshot_fallback"):
                db.log_event(project_id,"warning","成片渲染","story_plan_snapshot_fallback",f"{pending_short['version']} 未找到当前剪辑方案主题，已安全复用该版本已有短篇时间线；不会丢失待渲染任务",{"export_id":pending_short["id"],"version":pending_short["version"]})
            db.execute("UPDATE exports SET timeline_snapshot=?,render_options=? WHERE id=?",(json.dumps(snapshot,ensure_ascii=False),json.dumps(existing_options,ensure_ascii=False),pending_short["id"]))
            db.log_event(project_id,"info","成片渲染","pending_short_bgm_updated",f"{pending_short['version']} 已在启动前匹配短篇 BGM：{requested_bgm or '无 BGM'}",{"export_id":pending_short["id"],"bgm_filename":requested_bgm or None,"render_options":bgm_options})
    missing=[value for value in formats if not pending_by_format[value]]
    if missing:
        if not settings.get("story_plan"):raise HTTPException(409,"剪辑方案尚未生成，不能请求导出")
        numbers=[int(value["version"][1:]) for value in db.rows("SELECT version FROM exports WHERE project_id=?",(project_id,)) if value["version"].startswith("v") and value["version"][1:].isdigit()]
        next_number=max(numbers,default=0)
        for offset,value in enumerate(missing,1):_requested_snapshot(project_id,f"v{next_number+offset}",value,settings)
    jobs=[]
    for value in formats:
        pending=pending_by_format[value];created=False
        if not pending:
            result=_create_export(project_id,value);pending={"id":result["id"],"version":result["version"],"status":"render_requested"};created=True
        elif pending["status"]=="render_failed":db.execute("UPDATE exports SET status='render_requested' WHERE id=?",(pending["id"],))
        jobs.append({"id":pending["id"],"version":pending["version"],"format":value,"created":created})
    label={"long_16x9":"长篇","short_9x16":"短篇","both":"长篇 + 短篇"}[fmt]
    item="、".join(f"{job['version']} · {'长篇' if job['format']=='long_16x9' else '短篇'}" for job in jobs)
    db.create_control(project_id,"running","render_requested","成片渲染",item,render_scope=None if fmt=="both" else fmt)
    db.execute("UPDATE projects SET status='render_requested',error=NULL,updated_at=? WHERE id=?",(db.now(),project_id))
    db.log_event(project_id,"info","成片渲染","version_generate_started",f"已启动{label}渲染；将处理 "+item,{"requested_mode":fmt,"jobs":jobs})
    return {"ok":True,"mode":fmt,"jobs":jobs,"status":"render_requested"}
@app.post("/api/exports/{export_id}/approve")
def approve_export(export_id:int):
    export=db.row("SELECT project_id,version FROM exports WHERE id=?",(export_id,))
    if not export:raise HTTPException(404,"输出版本不存在")
    db.execute("UPDATE exports SET status='approved',locked=1,approved_at=? WHERE id=?",(db.now(),export_id));db.log_event(export["project_id"],"success","人工审核","export_approved",f"{export['version']} 已批准锁定");return {"ok":True}

@app.post("/api/exports/{export_id}/unlock")
def unlock_export(export_id:int):
    export=db.row("""SELECT e.*,p.raw_deleted_at,p.upload_confirmed_at
                     FROM exports e JOIN projects p ON p.id=e.project_id WHERE e.id=?""",(export_id,))
    if not export:raise HTTPException(404,"导出版本不存在")
    if export.get("status")!="approved" or not int(export.get("locked") or 0):
        raise HTTPException(409,"该版本当前没有处于批准锁定状态")
    control=db.control(export["project_id"]) or {}
    if control.get("desired_state") not in {"stopped","paused"}:
        raise HTTPException(409,"请先暂停或停止项目，再取消版本锁定")
    if export.get("raw_deleted_at"):
        raise HTTPException(409,"本项目原片已经删除，不能取消锁定后重新剪辑")
    confirmed=db.row("SELECT platform FROM platform_uploads WHERE project_id=? AND completed_at IS NOT NULL LIMIT 1",(export["project_id"],))
    if export.get("upload_confirmed_at") or confirmed:
        raise HTTPException(409,"已有平台上传确认；请先取消全部平台确认，再取消版本锁定")
    stamp=db.now()
    db.execute("UPDATE exports SET status='review_ready',locked=0,approved_at=NULL WHERE id=?",(export_id,))
    db.execute("UPDATE projects SET status=CASE WHEN status='published' THEN 'review_ready' ELSE status END,updated_at=? WHERE id=?",(stamp,export["project_id"]))
    db.log_event(export["project_id"],"warning","人工审核","export_unlocked",f"{export['version']} 已取消批准锁定；现有成片继续保留",{"export_id":export_id,"version":export["version"],"format":export["format"]})
    return {"ok":True,"id":export_id,"version":export["version"],"status":"review_ready","locked":False}

@app.post("/api/exports/{export_id}/release-scheduled")
def release_scheduled_export(export_id:int):
    try:return release_scheduled_short(export_id)
    except RuntimeError as error:raise HTTPException(409,str(error))

@app.post("/api/projects/{project_id}/raw/delete")
def delete_project_raw(project_id:int):
    project=db.project_detail(project_id)
    if not project:raise HTTPException(404,"项目不存在")
    if project.get("raw_deleted_at"):return {"ok":True,"already_deleted":True,"raw_deleted_at":project["raw_deleted_at"]}
    if project["control"]["desired_state"] not in {"stopped","paused"}:raise HTTPException(409,"请先停止项目再删除原片")
    approved={value["format"] for value in project["exports"] if value["status"]=="approved" and value.get("path") and value["path"]!="[]"}
    if not {"long_16x9","short_9x16"}.issubset(approved):raise HTTPException(409,"必须先批准有效的长篇和短篇版本")
    source_dir=Path(project["source_dir"]).resolve();inbox=INBOX.resolve()
    if not source_dir.is_relative_to(inbox):raise HTTPException(400,"项目原片目录不在 inbox 内，拒绝删除")
    targets=[];total_bytes=0
    for asset in project["assets"]:
        path=Path(asset["path"]).resolve()
        if not path.is_relative_to(source_dir) or path.suffix.lower() not in VIDEO_EXTENSIONS:raise HTTPException(400,f"原片路径校验失败：{asset['filename']}")
        if path.exists():targets.append(path);total_bytes+=path.stat().st_size
    for path in targets:path.unlink()
    stamp=db.now();db.execute("UPDATE projects SET raw_deleted_at=?,updated_at=? WHERE id=?",(stamp,stamp,project_id))
    db.log_event(project_id,"warning","存储清理","raw_deleted",f"人工确认审核完成后，已永久删除 {len(targets)} 个原片",{"bytes":total_bytes,"source_dir":str(source_dir)})
    return {"ok":True,"deleted_files":len(targets),"deleted_bytes":total_bytes,"raw_deleted_at":stamp}
@app.post("/api/projects/{project_id}/uploads/{platform}")
def upload_done(project_id:int,platform:str):
    if platform not in PLATFORMS:raise HTTPException(400,"未知平台")
    project=db.row("SELECT * FROM projects WHERE id=?",(project_id,))
    if not project:raise HTTPException(404,"项目不存在")
    existing={r["platform"] for r in db.rows("SELECT platform FROM platform_uploads WHERE project_id=? AND completed_at IS NOT NULL",(project_id,))}
    if existing|{platform}==set(PLATFORMS):
        approved={value["format"] for value in db.rows("SELECT format,path FROM exports WHERE project_id=? AND status='approved'",(project_id,)) if value.get("path") and value["path"]!="[]"}
        if not {"long_16x9","short_9x16"}.issubset(approved):raise HTTPException(409,"必须先批准有效的长篇和短篇版本，才能完成四平台确认")
        control=db.control(project_id) or {"desired_state":"stopped"}
        if control.get("desired_state") not in {"stopped","paused"}:raise HTTPException(409,"请先暂停或停止项目，再完成第四个平台确认")
    stamp=db.now();db.execute("INSERT INTO platform_uploads(project_id,platform,completed_at) VALUES(?,?,?) ON CONFLICT(project_id,platform) DO UPDATE SET completed_at=excluded.completed_at",(project_id,platform,stamp));done=db.rows("SELECT platform FROM platform_uploads WHERE project_id=? AND completed_at IS NOT NULL",(project_id,))
    db.log_event(project_id,"success","平台发布","upload_confirmed",f"已确认上传 {platform}")
    if {r['platform'] for r in done}==set(PLATFORMS):
        db.execute("UPDATE projects SET upload_confirmed_at=?,status='published',updated_at=? WHERE id=?",(stamp,stamp,project_id));project["upload_confirmed_at"]=stamp;cleanup_project_temp(project)
    return {"ok":True,"completed":[r['platform'] for r in done]}

@app.delete("/api/projects/{project_id}/uploads/{platform}")
def cancel_upload_done(project_id:int,platform:str):
    if platform not in PLATFORMS:raise HTTPException(400,"未知平台")
    if not db.row("SELECT id FROM projects WHERE id=?",(project_id,)):raise HTTPException(404,"项目不存在")
    db.execute("DELETE FROM platform_uploads WHERE project_id=? AND platform=?",(project_id,platform))
    stamp=db.now();db.execute("UPDATE projects SET upload_confirmed_at=NULL,status=CASE WHEN status='published' THEN 'review_ready' ELSE status END,updated_at=? WHERE id=?",(stamp,project_id))
    done=db.rows("SELECT platform FROM platform_uploads WHERE project_id=? AND completed_at IS NOT NULL",(project_id,))
    db.log_event(project_id,"warning","平台发布","upload_confirmation_cancelled",f"已取消上传确认 {platform}")
    return {"ok":True,"completed":[r['platform'] for r in done]}

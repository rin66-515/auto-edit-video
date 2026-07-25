import json

from . import db
from .media import rendered_time_to_timeline,shift_interval_after_cuts,trim_timeline


def shift_privacy_rules(rules,cut_intervals,end_at=None):
    shifted={}
    for key,values in (rules or {}).items():
        if key not in {"suppress","force_cover","force_owner"}:
            shifted[key]=values
            continue
        output=[]
        for rule in values or []:
            if not isinstance(rule,dict):continue
            for start,end in shift_interval_after_cuts(rule.get("start",0),rule.get("end",0),cut_intervals,end_at):
                value=dict(rule);value["start"]=start;value["end"]=end;output.append(value)
        shifted[key]=output
    return shifted

def _map_timed_values(values,mapper):
    output=[]
    for value in values or []:
        if not isinstance(value,dict):continue
        mapped=dict(value);mapped["start"]=mapper(value.get("start",0));mapped["end"]=mapper(value.get("end",0));output.append(mapped)
    return output

def _shift_timed_values(values,cut_intervals,end_at=None):
    output=[]
    for value in values or []:
        if not isinstance(value,dict):continue
        for start,end in shift_interval_after_cuts(value.get("start",0),value.get("end",0),cut_intervals,end_at):
            shifted=dict(value);shifted["start"]=start;shifted["end"]=end;output.append(shifted)
    return output

def create_manual_revision(project_id,source_export_id,cut_intervals,end_at,privacy_rules,instruction_text,text_overlays=None,source_actual_duration=None,final_black_trim=True):
    db.init_db()
    source=db.row("SELECT * FROM exports WHERE id=? AND project_id=?",(source_export_id,project_id))
    if not source:raise ValueError("找不到源成片版本")
    if source["format"]!="long_16x9":raise ValueError("当前人工时间码修订只支持长篇")
    pending=db.row("SELECT id,version FROM exports WHERE project_id=? AND status IN ('render_requested','rendering')",(project_id,))
    if pending:raise ValueError(f"已有待处理版本 {pending['version']}")
    try:snapshot=json.loads(source.get("timeline_snapshot") or "{}")
    except json.JSONDecodeError:snapshot={}
    timeline=snapshot.get("long") if isinstance(snapshot,dict) else None
    if not timeline:raise ValueError("源版本没有可复用的长篇时间线")
    original_cut_intervals=cut_intervals;original_end_at=end_at
    if source_actual_duration:
        mapper=lambda seconds:rendered_time_to_timeline(timeline,seconds,source_actual_duration)
        cut_intervals=[(mapper(start),mapper(end)) for start,end in cut_intervals];end_at=mapper(end_at) if end_at is not None else None
        mapped_rules=dict(privacy_rules or {})
        for key in ("suppress","force_cover","force_owner"):mapped_rules[key]=_map_timed_values(mapped_rules.get(key),mapper)
        privacy_rules=mapped_rules;text_overlays=_map_timed_values(text_overlays,mapper)
    trimmed=trim_timeline(timeline,cut_intervals,end_at)
    if not trimmed:raise ValueError("人工裁剪后时间线为空")
    shifted_rules=shift_privacy_rules(privacy_rules,cut_intervals,end_at)
    shifted_overlays=_shift_timed_values(text_overlays,cut_intervals,end_at)
    versions=[int(value["version"][1:]) for value in db.rows("SELECT version FROM exports WHERE project_id=?",(project_id,)) if value["version"].startswith("v") and value["version"][1:].isdigit()]
    version=f"v{max(versions,default=0)+1}";stamp=db.now()
    options={
        "privacy_rules":shifted_rules,
        "text_overlays":shifted_overlays,
        "final_black_trim":{"enabled":bool(final_black_trim and end_at is not None),"search_seconds":5.0},
        "manual_revision":{"source_export_id":source_export_id,"source_version":source["version"],"time_basis":"rendered_source_export" if source_actual_duration else "source_export","source_actual_duration":source_actual_duration,"requested_cut_intervals":original_cut_intervals,"requested_end_at":original_end_at,"mapped_cut_intervals":cut_intervals,"mapped_end_at":end_at,"instruction":instruction_text},
    }
    export_id=db.execute(
        "INSERT INTO exports(project_id,version,format,status,timeline_snapshot,caption_overrides,render_options,source_export_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (project_id,version,source["format"],"render_requested",json.dumps({"long":trimmed},ensure_ascii=False),source.get("caption_overrides") or "{}",json.dumps(options,ensure_ascii=False),source_export_id,stamp),
    )
    db.execute("INSERT INTO revisions(project_id,kind,body,status,created_at,resolved_at) VALUES(?,?,?,?,?,?)",(project_id,"privacy_and_cut",instruction_text,"resolved",stamp,stamp))
    db.execute("UPDATE projects SET status='render_requested',error=NULL,updated_at=? WHERE id=?",(stamp,project_id))
    db.create_control(project_id,"stopped","render_requested","成片人工修订",f"{version} 等待启动")
    old_duration=sum(float(value.get("end",0))-float(value.get("start",0)) for value in timeline);new_duration=sum(float(value.get("end",0))-float(value.get("start",0)) for value in trimmed)
    db.log_event(project_id,"success","成片人工修订","manual_revision_created",f"已根据实际成片时间码创建 {version} 长篇修订任务",{"source_export_id":source_export_id,"new_export_id":export_id,"cut_seconds":round(old_duration-new_duration,3),"new_duration":round(new_duration,3),"privacy_suppress_rules":len(shifted_rules.get('suppress',[])),"privacy_force_rules":len(shifted_rules.get('force_cover',[])),"privacy_owner_rules":len(shifted_rules.get('force_owner',[])),"text_overlays":len(shifted_overlays),"source_actual_duration":source_actual_duration})
    return {"export_id":export_id,"version":version,"status":"render_requested","old_duration":round(old_duration,3),"new_duration":round(new_duration,3),"privacy_rules":shifted_rules,"text_overlays":shifted_overlays,"mapped_cut_intervals":cut_intervals,"mapped_end_at":end_at}

def create_short_privacy_revision(project_id,source_export_id,privacy_rules,instruction_text,output_names=None):
    db.init_db();source=db.row("SELECT * FROM exports WHERE id=? AND project_id=?",(source_export_id,project_id))
    if not source:raise ValueError("找不到源短篇版本")
    if source["format"]!="short_9x16":raise ValueError("来源版本不是短篇")
    try:snapshot=json.loads(source.get("timeline_snapshot") or "{}")
    except json.JSONDecodeError:snapshot={}
    if not snapshot or not all(isinstance(value,list) and value for value in snapshot.values()):raise ValueError("源短篇没有可复用的独立时间线")
    if output_names:
        requested={str(value) for value in output_names};snapshot={name:timeline for name,timeline in snapshot.items() if name in requested}
        if not snapshot:raise ValueError("指定保留的短篇不在源版本中")
    versions=[int(value["version"][1:]) for value in db.rows("SELECT version FROM exports WHERE project_id=?",(project_id,)) if value["version"].startswith("v") and value["version"][1:].isdigit()];version=f"v{max(versions,default=0)+1}";stamp=db.now()
    try:options=json.loads(source.get("render_options") or "{}")
    except json.JSONDecodeError:options={}
    options.pop("queue_after_export_id",None);options["privacy_rules"]=privacy_rules;options["kept_outputs"]=list(snapshot);options["manual_revision"]={"source_export_id":source_export_id,"source_version":source["version"],"time_basis":"short_output","instruction":instruction_text}
    export_id=db.execute("INSERT INTO exports(project_id,version,format,status,timeline_snapshot,caption_overrides,render_options,source_export_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(project_id,version,source["format"],"render_requested",json.dumps(snapshot,ensure_ascii=False),source.get("caption_overrides") or "{}",json.dumps(options,ensure_ascii=False),source_export_id,stamp))
    db.execute("INSERT INTO revisions(project_id,kind,body,status,created_at,resolved_at) VALUES(?,?,?,?,?,?)",(project_id,"short_privacy",instruction_text,"resolved",stamp,stamp));db.execute("UPDATE projects SET status='render_requested',error=NULL,updated_at=? WHERE id=?",(stamp,project_id));pending=[value["version"] for value in db.rows("SELECT version FROM exports WHERE project_id=? AND status='render_requested' ORDER BY id",(project_id,))];db.create_control(project_id,"stopped","render_requested","成片人工修订","、".join(pending)+" 等待启动");db.log_event(project_id,"success","短篇人工修订","short_privacy_revision_created",f"已创建 {version} 单条短篇隐私修订任务；首镜头本人不遮挡",{"source_export_id":source_export_id,"new_export_id":export_id,"outputs":list(snapshot),"privacy_suppress_rules":len(privacy_rules.get('suppress',[])),"privacy_force_rules":len(privacy_rules.get('force_cover',[]))})
    return {"export_id":export_id,"version":version,"status":"render_requested","outputs":list(snapshot),"privacy_rules":privacy_rules}

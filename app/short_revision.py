import json

from . import db


def _json(value,default):
    try:return json.loads(value or "")
    except (TypeError,json.JSONDecodeError):return default


def release_scheduled_short(export_id):
    export=db.row("SELECT * FROM exports WHERE id=?",(export_id,))
    if not export:raise RuntimeError("短篇队列版本不存在")
    if export["status"]!="scheduled":raise RuntimeError(f"短篇队列当前不是等待状态：{export['status']}")
    options=_json(export.get("render_options"),{});wait_id=int(options.get("queue_after_export_id") or export.get("source_export_id") or 0);wait_for=db.row("SELECT id,version,status FROM exports WHERE id=? AND project_id=?",(wait_id,export["project_id"]))
    if not wait_for or wait_for["status"] not in {"review_ready","approved"}:raise RuntimeError("前序长篇尚未完成审核文件生成")
    control=db.control(export["project_id"]) or {}
    if control.get("desired_state") not in {"stopped","paused"}:raise RuntimeError("项目尚未在前序长篇结束后安全停止")
    active=db.row("SELECT id FROM exports WHERE project_id=? AND status IN ('render_requested','rendering')",(export["project_id"],))
    if active:raise RuntimeError("项目仍有其他渲染任务")
    stamp=db.now();db.execute("UPDATE exports SET status='render_requested' WHERE id=?",(export_id,));db.execute("UPDATE projects SET status='render_requested',error=NULL,updated_at=? WHERE id=?",(stamp,export["project_id"]));db.create_control(export["project_id"],"stopped","render_requested","成片渲染",f"{export['version']} 短篇队列已释放，等待启动")
    db.log_event(export["project_id"],"success","短篇编辑队列","scheduled_short_released",f"前序 {wait_for['version']} 已完成，{export['version']} 的短篇队列已释放并等待启动",{"export_id":export_id,"wait_for_export_id":wait_id})
    return {"ok":True,"project_id":export["project_id"],"export_id":export_id,"version":export["version"],"status":"render_requested"}

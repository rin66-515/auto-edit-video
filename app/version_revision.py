import copy
import json

from . import db
from .revision_intent import parse_audio_revision,parse_revision_intent


def _json(value,default):
    try:return json.loads(value or "")
    except (TypeError,json.JSONDecodeError):return copy.deepcopy(default)


def _next_version(project_id):
    versions=[
        int(value["version"][1:])
        for value in db.rows("SELECT version FROM exports WHERE project_id=?",(project_id,))
        if str(value["version"]).startswith("v") and str(value["version"])[1:].isdigit()
    ]
    return f"v{max(versions,default=0)+1}"


def _timeline_positions(timeline,fmt):
    positions=[];cursor=0.0
    for index,item in enumerate(timeline):
        if index and fmt=="short_9x16" and str(item.get("transition") or "cut")!="cut":
            cursor-=max(0.0,min(float(item.get("transition_duration") or 0),0.6))
        length=max(0.0,float(item.get("end") or 0)-float(item.get("start") or 0))
        positions.append((cursor,cursor+length))
        cursor+=length
    return positions


def _caption_onset(item,asset,output_start,requested_after):
    analysis=_json(asset.get("analysis"),{})
    candidates=[]
    clip_start=float(item.get("start") or 0);clip_end=float(item.get("end") or 0)
    for caption in analysis.get("bilingual_captions") or []:
        try:start=float(caption.get("start") or 0);end=float(caption.get("end") or 0)
        except (TypeError,ValueError):continue
        if end<=clip_start or start>=clip_end:continue
        source_start=max(start,clip_start)
        candidates.append((output_start+source_start-clip_start,source_start))
    candidates.sort()
    if requested_after is not None:
        selected=next((value for value in candidates if value[0]>=requested_after-0.03),None)
    else:selected=candidates[0] if candidates else None
    return selected


def _apply_audio_revision(timeline,fmt,assets_by_id,audio,options):
    if not audio.get("matched"):return []
    positions=_timeline_positions(timeline,fmt);target_index=audio.get("target_clip_index")
    candidates=[]
    for index,item in enumerate(timeline):
        if target_index and index+1!=int(target_index):continue
        if str(item.get("audio_mode") or "")=="dialogue":candidates.append(index)
    requested=audio.get("requested_after_seconds")
    if not candidates and target_index and 1<=int(target_index)<=len(timeline):
        candidates=[int(target_index)-1]
    if requested is not None and len(candidates)>1:
        containing=[index for index in candidates if positions[index][0]<=requested<=positions[index][1]]
        if containing:candidates=containing
    operations=[]
    if audio.get("duck_on_dialogue_onset") and candidates:
        index=candidates[0];item=timeline[index];asset=assets_by_id.get(int(item.get("asset_id") or 0))
        onset=_caption_onset(item,asset or {},positions[index][0],requested) if asset else None
        if onset is None:
            output_onset=max(positions[index][0],float(requested or positions[index][0]))
            source_onset=float(item.get("start") or 0)+(output_onset-positions[index][0])
        else:output_onset,source_onset=onset
        offset=max(0.0,min(float(item.get("end") or 0)-float(item.get("start") or 0)-0.08,source_onset-float(item.get("start") or 0)))
        item["audio_mode"]="dialogue";item["duck_bgm"]=True
        item["dialogue_start_offset"]=round(offset,6);item["dialogue_fade_seconds"]=0.08
        if audio.get("background_cleanup"):item["background_cleanup"]=True
        operations.append({
            "kind":"audio_duck_on_dialogue","clip_index":index+1,
            "output_start":round(positions[index][0],3),
            "dialogue_output_start":round(positions[index][0]+offset,3),
            "dialogue_source_start":round(float(item.get("start") or 0)+offset,3),
        })
    if audio.get("reduce_bgm") and not operations:
        options["bgm_volume"]=min(float(options.get("bgm_volume") or 0.26),0.18)
        operations.append({"kind":"bgm_volume","value":options["bgm_volume"]})
    return operations


def _apply_targeted_shots(timeline,assets,intent):
    operations=[]
    for recommendation in intent.get("recommendations") or []:
        target=recommendation.get("target_clip_index")
        if not target:continue
        index=int(target)-1
        if index<0 or index>=len(timeline):raise ValueError(f"镜头{int(target):02d} 不在来源版本时间线中")
        asset=_resolve_asset(assets,recommendation)
        if not asset:raise ValueError(f"找不到指定原素材：{recommendation.get('filename') or recommendation.get('asset_code')}")
        old=timeline[index];length=max(0.25,float(old.get("end") or 0)-float(old.get("start") or 0))
        asset_duration=float(asset.get("duration") or 0);requested=recommendation.get("time_seconds")
        if requested is None:start=float(old.get("start") or 0) if int(old.get("asset_id") or 0)==int(asset["id"]) else 0.0
        elif recommendation.get("time_mode")=="after":start=float(requested)
        else:start=max(0.0,float(requested)-length/2)
        start=max(0.0,min(start,max(0.0,asset_duration-length)));end=min(asset_duration,start+length)
        if end-start<0.25:raise ValueError(f"指定素材时间点没有足够画面：{recommendation.get('filename')}")
        replacement=dict(old);replacement.update({
            "asset_id":int(asset["id"]),"start":round(start,6),"end":round(end,6),
            "audio_mode":recommendation.get("audio_mode") or old.get("audio_mode") or "mute",
            "show_captions":bool(recommendation.get("show_captions",False)),
            "background_cleanup":bool(recommendation.get("background_cleanup")),
            "duck_bgm":bool(recommendation.get("duck_bgm")),
            "reason":recommendation.get("label") or "人工指定镜头替换",
        })
        timeline[index]=replacement
        operations.append({"kind":"replace_shot","clip_index":index+1,"asset_id":int(asset["id"]),"filename":asset["filename"],"start":round(start,3),"end":round(end,3)})
    return operations


def _slice_timeline_item(item,relative_start,relative_end):
    source_start=float(item.get("start") or 0)
    result=dict(item)
    result["start"]=round(source_start+relative_start,6)
    result["end"]=round(source_start+relative_end,6)
    return result


def _delete_output_range(timeline,start,end):
    positions=_timeline_positions(timeline,"long_16x9")
    total=positions[-1][1] if positions else 0.0
    if start<0 or end<=start:raise ValueError("成片删除区间无效")
    if end>total+0.05:raise ValueError(f"成片删除区间超过来源版本长度：{end:.1f} > {total:.1f} 秒")
    result=[];removed=0.0;affected=0
    for item,(output_start,output_end) in zip(timeline,positions):
        overlap=max(0.0,min(end,output_end)-max(start,output_start))
        if overlap<=0:
            result.append(item);continue
        affected+=1;removed+=overlap
        length=max(0.0,output_end-output_start)
        left=max(0.0,min(start,output_end)-output_start)
        right=max(0.0,end-output_start)
        if left>0.000001:result.append(_slice_timeline_item(item,0.0,min(left,length)))
        if right<length-0.000001:result.append(_slice_timeline_item(item,max(0.0,right),length))
    expected=min(end,total)-start
    if removed<expected-0.05:raise ValueError(f"成片删除区间只匹配到 {removed:.1f}/{expected:.1f} 秒")
    timeline[:]=result
    return {
        "kind":"delete_output_range","output_start":round(start,3),"output_end":round(end,3),
        "removed_seconds":round(removed,3),"affected_clips":affected,
    }


def _insertion_index(timeline,output_at):
    positions=_timeline_positions(timeline,"long_16x9")
    total=positions[-1][1] if positions else 0.0
    if output_at<0 or output_at>total+0.05:
        raise ValueError(f"成片插入点超过来源版本长度：{output_at:.1f} > {total:.1f} 秒")
    if output_at>=total-0.001:return len(timeline)
    for index,(output_start,output_end) in enumerate(positions):
        if abs(output_at-output_start)<=0.001:return index
        if abs(output_at-output_end)<=0.001:return index+1
        if output_start<output_at<output_end:
            item=timeline[index];length=output_end-output_start;split=output_at-output_start
            left=_slice_timeline_item(item,0.0,split);right=_slice_timeline_item(item,split,length)
            timeline[index:index+1]=[left,right]
            return index+1
    raise ValueError(f"无法在成片 {output_at:.1f} 秒定位插入点")


def _inherited_audio_mode(timeline,index,options):
    policy=str(options.get("long_audio_policy") or "")
    if policy.startswith("denoise_only"):return "denoise"
    neighbours=[]
    if index:neighbours.append(timeline[index-1])
    if index<len(timeline):neighbours.append(timeline[index])
    return next((str(value.get("audio_mode")) for value in neighbours if value.get("audio_mode")),"mute")


def _insert_source_range(timeline,assets,insertion,options):
    asset=_resolve_asset(assets,insertion)
    if not asset:raise ValueError(f"找不到指定素材：{insertion.get('filename') or insertion.get('asset_code')}")
    start=float(insertion.get("source_start_seconds") or 0);end=float(insertion.get("source_end_seconds") or 0)
    duration=max(0.0,float(asset.get("duration") or 0))
    if start<0 or end<=start:raise ValueError(f"指定素材时间段无效：{asset['filename']}")
    if end>duration+0.05:raise ValueError(f"指定素材时间段超过原片长度：{asset['filename']} · {end:.1f} > {duration:.1f} 秒")
    output_at=float(insertion.get("output_at_seconds") or 0)
    index=_insertion_index(timeline,output_at)
    audio_mode=insertion.get("audio_mode") or _inherited_audio_mode(timeline,index,options)
    item={
        "asset_id":int(asset["id"]),"start":round(start,6),"end":round(min(end,duration),6),
        "audio_mode":audio_mode,"show_captions":bool(insertion.get("show_captions",False)),
        "background_cleanup":bool(insertion.get("background_cleanup") or audio_mode=="denoise"),
        "duck_bgm":bool(insertion.get("duck_bgm")),"transition":"cut",
        "reason":insertion.get("label") or "人工指定成片插入镜头",
    }
    neighbour=timeline[index-1] if index else (timeline[index] if timeline else None)
    if neighbour and neighbour.get("chapter"):item["chapter"]=neighbour["chapter"]
    timeline.insert(index,item)
    return {
        "kind":"insert_source_range","clip_index":index+1,"output_at":round(output_at,3),
        "asset_id":int(asset["id"]),"filename":asset["filename"],"asset_code":insertion.get("asset_code"),
        "source_start":round(start,3),"source_end":round(min(end,duration),3),
        "added_seconds":round(min(end,duration)-start,3),"audio_mode":audio_mode,
    }


def _apply_exact_long_edits(timeline,assets,intent,options):
    actions=[]
    for value in intent.get("output_deletions") or []:
        actions.append((float(value["start_seconds"]),1,"delete",value))
    for value in intent.get("insertions") or []:
        actions.append((float(value["output_at_seconds"]),0,"insert",value))
    operations=[]
    for _,_,kind,value in sorted(actions,key=lambda item:(item[0],item[1]),reverse=True):
        if kind=="delete":
            operations.append(_delete_output_range(timeline,float(value["start_seconds"]),float(value["end_seconds"])))
        else:
            operations.append(_insert_source_range(timeline,assets,value,options))
    return operations


def _resolve_asset(assets,recommendation):
    requested=str(recommendation.get("filename") or "").lower()
    if requested:
        exact=next((value for value in assets if str(value.get("filename") or "").lower()==requested),None)
        if exact:return exact
    code=str(recommendation.get("asset_code") or "").zfill(4)
    if not code.strip("0"):return None
    matches=[
        value for value in assets
        if str(value.get("filename") or "").lower().endswith(f"_{code}_d.mp4")
    ]
    return matches[0] if len(matches)==1 else None


def _asset_intervals(timeline,asset_id):
    return sorted(
        (float(item.get("start") or 0),float(item.get("end") or 0))
        for item in timeline
        if int(item.get("asset_id") or 0)==int(asset_id)
    )


def _highlight_item(asset,timeline,recommendation):
    duration=max(0.0,float(asset.get("duration") or 0))
    intervals=_asset_intervals(timeline,asset["id"])
    gaps=[];cursor=0.0
    for start,end in intervals:
        if start>cursor+0.25:gaps.append((cursor,start))
        cursor=max(cursor,end)
    if cursor<duration-0.25:gaps.append((cursor,duration))
    if not gaps:return None
    requested=recommendation.get("time_seconds")
    if requested is not None:
        requested=max(0.0,min(duration,float(requested)))
        selected=next((gap for gap in gaps if gap[0]<=requested<=gap[1]),None)
    else:selected=None
    if selected is None:selected=max(gaps,key=lambda gap:gap[1]-gap[0])
    available=selected[1]-selected[0]
    if available<0.5:return None
    length=min(available,duration if duration<=16 else 12.0)
    if requested is None:start=selected[0]+(available-length)/2
    elif recommendation.get("time_mode")=="after":start=max(selected[0],min(float(requested),selected[1]-length))
    else:start=max(selected[0],min(float(requested)-length/2,selected[1]-length))
    return {
        "asset_id":int(asset["id"]),
        "start":round(start,6),
        "end":round(start+length,6),
        "reason":recommendation.get("label") or "人工推荐补充镜头",
        "audio_mode":"denoise",
        "transition":"cut",
    }


def _trim_dominant_output_range(timeline,start,end,seconds):
    positions=_timeline_positions(timeline,"long_16x9");by_asset={}
    for index,(output_start,output_end) in enumerate(positions):
        overlap=max(0.0,min(end,output_end)-max(start,output_start))
        if overlap<=0:continue
        asset_id=int(timeline[index].get("asset_id") or 0)
        by_asset[asset_id]=by_asset.get(asset_id,0.0)+overlap
    if not by_asset:raise ValueError(f"成片 {start/60:g}–{end/60:g} 分钟范围没有可修改镜头")
    dominant=max(by_asset,key=by_asset.get);candidates=[]
    for index,(output_start,output_end) in enumerate(positions):
        if int(timeline[index].get("asset_id") or 0)!=dominant:continue
        overlap=max(0.0,min(end,output_end)-max(start,output_start))
        if overlap<=0:continue
        fully_inside=output_start>=start-0.001 and output_end<=end+0.001
        candidates.append((not fully_inside,-output_start,index,overlap,output_start,output_end))
    remaining=seconds;removed=0.0;removed_clips=0
    for _,_,index,overlap,output_start,output_end in sorted(candidates):
        if remaining<=0.001:break
        item=timeline[index]
        if item is None:continue
        length=float(item.get("end") or 0)-float(item.get("start") or 0)
        delta=min(remaining,overlap)
        if delta>=length-0.001:
            timeline[index]=None;removed_clips+=1
        elif output_start>=start-0.001 and output_end<=end+0.001:
            item["end"]=round(float(item["end"])-delta,6)
        elif output_start<start<output_end:
            item["end"]=round(float(item["end"])-delta,6)
        elif output_start<end<output_end:
            item["start"]=round(float(item["start"])+delta,6)
        else:continue
        removed+=delta;remaining-=delta
    timeline[:]=[item for item in timeline if item is not None]
    if remaining>0.05:
        raise ValueError(f"成片区间内主导素材只能削减 {removed:.1f} 秒，不足以容纳推荐镜头")
    return {
        "kind":"rebalance_output_range",
        "output_start":round(start,3),
        "output_end":round(end,3),
        "dominant_asset_id":dominant,
        "removed_seconds":round(removed,3),
        "removed_clips":removed_clips,
    }


def _insert_chronologically(timeline,item):
    key=(int(item["asset_id"]),float(item["start"]))
    index=0
    while index<len(timeline):
        current=(int(timeline[index].get("asset_id") or 0),float(timeline[index].get("start") or 0))
        if current>key:break
        index+=1
    chapter=None
    if index:chapter=timeline[index-1].get("chapter")
    if not chapter and index<len(timeline):chapter=timeline[index].get("chapter")
    if chapter:item["chapter"]=chapter
    timeline.insert(index,item)
    return index


def _apply_long_range_revision(timeline,assets,intent):
    ranges=intent.get("output_ranges") or []
    recommendations=[value for value in intent.get("recommendations") or [] if not value.get("target_clip_index")]
    if not ranges or not recommendations:return []
    additions=[];skipped=[]
    for recommendation in recommendations:
        asset=_resolve_asset(assets,recommendation)
        if not asset:
            if recommendation.get("priority")=="required":
                raise ValueError(f"找不到指定原素材：{recommendation.get('filename') or recommendation.get('asset_code')}")
            skipped.append(recommendation.get("asset_code"));continue
        item=_highlight_item(asset,timeline,recommendation)
        if item is None:
            skipped.append(recommendation.get("asset_code"));continue
        additions.append((asset,item,recommendation))
    if not additions:raise ValueError("推荐素材均已充分使用或无法匹配，没有可插入的新镜头")
    added_seconds=sum(item["end"]-item["start"] for _,item,_ in additions)
    target=ranges[0]
    operations=[_trim_dominant_output_range(timeline,float(target["start_seconds"]),float(target["end_seconds"]),added_seconds)]
    for asset,item,recommendation in sorted(additions,key=lambda value:(int(value[0]["id"]),float(value[1]["start"]))):
        index=_insert_chronologically(timeline,item)
        operations.append({
            "kind":"insert_shot",
            "clip_index":index+1,
            "asset_id":int(asset["id"]),
            "filename":asset["filename"],
            "asset_code":recommendation.get("asset_code"),
            "start":round(item["start"],3),
            "end":round(item["end"],3),
        })
    if skipped:operations[0]["skipped_asset_codes"]=skipped
    return operations


def _prepare_inherited_revision(project_id,source_export_id,revisions,instruction_text=None):
    """Build an inherited timeline in memory without creating versions or logs."""
    source=db.row("SELECT * FROM exports WHERE id=? AND project_id=?",(source_export_id,project_id))
    if not source:raise ValueError("找不到来源版本")
    snapshot=_json(source.get("timeline_snapshot"),{})
    if not snapshot or not all(isinstance(value,list) and value for value in snapshot.values()):
        raise ValueError("来源版本没有可继承的独立时间线")
    snapshot=copy.deepcopy(snapshot);assets=db.rows("SELECT * FROM assets WHERE project_id=? ORDER BY id",(project_id,));assets_by_id={int(value["id"]):value for value in assets}
    options=_json(source.get("render_options"),{});options.pop("queue_after_export_id",None)
    bodies=[str(value.get("body") or "") for value in revisions if isinstance(value,dict)]
    text=instruction_text or "\n".join(value for value in bodies if value).strip()
    source_clip_count=len(next(iter(snapshot.values()))) if source["format"]=="short_9x16" else None
    intent=parse_revision_intent(revisions,source_clip_count);audio=parse_audio_revision(revisions);operations=[]
    for _,timeline in snapshot.items():
        operations.extend(_apply_targeted_shots(timeline,assets,intent))
        if source["format"]=="long_16x9":operations.extend(_apply_long_range_revision(timeline,assets,intent))
        if source["format"]=="long_16x9":operations.extend(_apply_exact_long_edits(timeline,assets,intent,options))
        operations.extend(_apply_audio_revision(timeline,source["format"],assets_by_id,audio,options))
    if not operations:
        raise ValueError("该意见尚未解析到可安全执行的局部修改。请明确镜头编号、音频变化，或选择“完整重规划”")
    return {
        "source":source,"snapshot":snapshot,"options":options,"operations":operations,"text":text,
    }


def validate_inherited_revision(project_id,source_export_id,revisions,instruction_text=None):
    """Validate a version-bound incremental revision without writing to SQLite."""
    prepared=_prepare_inherited_revision(project_id,source_export_id,revisions,instruction_text)
    return {
        "source_version":prepared["source"]["version"],
        "format":prepared["source"]["format"],
        "operations":prepared["operations"],
    }


def create_inherited_revision(project_id,source_export_id,revisions,instruction_text=None,revision_ids=None):
    """Create a new version by patching the source snapshot, never by replanning it."""
    db.init_db()
    prepared=_prepare_inherited_revision(project_id,source_export_id,revisions,instruction_text)
    source=prepared["source"];snapshot=prepared["snapshot"];options=prepared["options"]
    operations=prepared["operations"];text=prepared["text"]
    ids=[int(value) for value in (revision_ids or [])]
    version=_next_version(project_id);stamp=db.now()
    duration_changed=any(value.get("kind") in {"delete_output_range","insert_source_range"} for value in operations)
    preserved=["timeline","clip_order","transitions","duration","bgm","captions","privacy","effects"]
    if duration_changed:preserved.remove("duration")
    options["version_inheritance"]={
        "mode":"incremental","source_export_id":source_export_id,"source_version":source["version"],
        "preserved":preserved,"duration_policy":"explicit_timecode_delta" if duration_changed else "preserved",
        "revision_ids":ids,"operations":operations,"instruction":text,
    }
    export_id=db.execute(
        "INSERT INTO exports(project_id,version,format,status,timeline_snapshot,caption_overrides,render_options,source_export_id,render_mode,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (project_id,version,source["format"],"render_requested",json.dumps(snapshot,ensure_ascii=False),source.get("caption_overrides") or "{}",json.dumps(options,ensure_ascii=False),source_export_id,"inherited_timeline",stamp),
    )
    if ids:
        placeholders=",".join("?" for _ in ids)
        db.execute(f"UPDATE revisions SET status='applied',resolved_at=?,applied_export_id=?,applied_version=? WHERE project_id=? AND status='open' AND id IN ({placeholders})",(stamp,export_id,version,project_id,*ids))
    else:
        kind="audio" if any(value["kind"].startswith(("audio","bgm")) for value in operations) else "edit"
        db.execute("INSERT INTO revisions(project_id,kind,body,status,source_export_id,source_version,applied_export_id,applied_version,created_at,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(project_id,kind,text,"applied",source_export_id,source["version"],export_id,version,stamp,stamp))
    carried_privacy=db.rows("SELECT id FROM revisions WHERE project_id=? AND source_export_id=? AND status='open' AND kind='privacy' ORDER BY id",(project_id,source_export_id))
    if carried_privacy:
        db.execute("UPDATE revisions SET source_export_id=?,source_version=? WHERE project_id=? AND source_export_id=? AND status='open' AND kind='privacy'",(export_id,version,project_id,source_export_id))
        options["carried_privacy_revision_ids"]=[int(value["id"]) for value in carried_privacy]
        db.execute("UPDATE exports SET render_options=? WHERE id=?",(json.dumps(options,ensure_ascii=False),export_id))
    db.execute("UPDATE projects SET status='render_requested',error=NULL,updated_at=? WHERE id=?",(stamp,project_id))
    db.create_control(project_id,"stopped","render_requested","版本增量修改",f"{version} · 基于 {source['version']} 等待启动",render_scope=source["format"])
    db.log_event(project_id,"success","版本增量修改","inherited_revision_created",f"已基于 {source['version']} 原时间线创建 {version}；仅应用 {len(operations)} 项明确修改",{"source_export_id":source_export_id,"new_export_id":export_id,"format":source["format"],"revision_ids":ids,"operations":operations})
    return {"ok":True,"mode":"incremental","export_id":export_id,"version":version,"status":"render_requested","source_version":source["version"],"operations":operations,"carried_privacy_revision_ids":[int(value["id"]) for value in carried_privacy]}

import json
import math
import random


DEFAULT_CHAPTERS=("出发与近况","沿途与街景","朋友相聚与饮食","交流与余韵")


def _analysis(asset):
    value=asset.get("analysis") or {}
    if isinstance(value,dict):return value
    try:return json.loads(value)
    except (TypeError,json.JSONDecodeError):return {}


def _duration(asset):
    try:return max(0.0,float(asset.get("duration") or 0))
    except (TypeError,ValueError):return 0.0


def _captions(asset):
    value=_analysis(asset).get("bilingual_captions") or []
    return value if isinstance(value,list) else []


def timeline_stats(timeline,assets):
    by_id={int(value["id"]):value for value in assets};total=0.0;errors=[];intervals={}
    if not isinstance(timeline,list):return 0.0,["timeline不是数组"]
    for index,item in enumerate(timeline,1):
        if not isinstance(item,dict):errors.append(f"第{index}项不是对象");continue
        try:asset_id=int(item.get("asset_id"));start=float(item.get("start"));end=float(item.get("end"))
        except (TypeError,ValueError):errors.append(f"第{index}项缺少有效asset_id/start/end");continue
        asset=by_id.get(asset_id)
        if not asset:errors.append(f"第{index}项使用不存在的asset_id={asset_id}");continue
        duration=_duration(asset)
        if start<0 or end<=start or end>duration+0.05:
            errors.append(f"第{index}项asset_id={asset_id}越界：start={start}, end={end}, duration={duration:.2f}");continue
        if item.get("audio_mode") not in {"dialogue","ambient","montage","mute"}:errors.append(f"第{index}项缺少有效audio_mode")
        intervals.setdefault(asset_id,[]).append((start,end,index));total+=end-start
    for asset_id,values in intervals.items():
        values.sort()
        for previous,current in zip(values,values[1:]):
            if current[0]<previous[1]-0.01:errors.append(f"asset_id={asset_id}第{previous[2]}项与第{current[2]}项时间重叠")
    return total,errors


def _mode_seconds(timeline,mode):
    total=0.0
    for item in timeline if isinstance(timeline,list) else []:
        if not isinstance(item,dict) or item.get("audio_mode")!=mode:continue
        try:total+=max(0.0,float(item.get("end"))-float(item.get("start")))
        except (TypeError,ValueError):pass
    return total


def _effective_short_seconds(timeline):
    total=0.0
    for position,item in enumerate(timeline if isinstance(timeline,list) else []):
        try:total+=max(0.0,float(item.get("end"))-float(item.get("start")))
        except (AttributeError,TypeError,ValueError):continue
        if position and str(item.get("transition") or "cut")!="cut":
            try:total-=max(0.0,min(float(item.get("transition_duration") or 0),0.6))
            except (TypeError,ValueError):pass
    return max(0.0,total)


def story_plan_errors(plan,assets):
    if not isinstance(plan,dict):return ["计划不是JSON对象"]
    long=plan.get("long") if isinstance(plan.get("long"),dict) else {};shorts=plan.get("shorts") if isinstance(plan.get("shorts"),list) else []
    long_seconds,long_errors=timeline_stats(long.get("timeline"),assets);errors=list(long_errors)
    if long_seconds<600 or long_seconds>3600:errors.append(f"长篇有效时长必须为600–3600秒，当前{long_seconds:.1f}秒")
    if long_seconds and _mode_seconds(long.get("timeline"),"ambient")/long_seconds>0.20:errors.append("长篇ambient环境声超过20%，只允许偶尔凸显")
    if not 1<=len(shorts)<=3:errors.append(f"短篇数量必须为1–3条，当前{len(shorts)}条")
    for index,short in enumerate(shorts,1):
        timeline=short.get("timeline") if isinstance(short,dict) else None;raw_seconds,short_errors=timeline_stats(timeline,assets);seconds=_effective_short_seconds(timeline)
        errors.extend(f"短篇{index}：{value}" for value in short_errors)
        if seconds<10 or seconds>600:errors.append(f"短篇{index}有效时长必须为10–600秒，当前{seconds:.1f}秒")
        if seconds and _mode_seconds(timeline,"ambient")/seconds>0.25:errors.append(f"短篇{index}ambient环境声超过25%")
        if not str(short.get("hook") or "").strip():errors.append(f"短篇{index}缺少真实明确的hook")
        minimum_clips=max(4,min(8,math.ceil(seconds/4))) if seconds else 4
        if isinstance(timeline,list) and len(timeline)<minimum_clips:errors.append(f"短篇{index}至少需要{minimum_clips}个多素材节奏片段，当前{len(timeline)}个")
        if isinstance(timeline,list) and timeline:
            try:first_seconds=float(timeline[0].get("end"))-float(timeline[0].get("start"))
            except (AttributeError,TypeError,ValueError):first_seconds=99
            if first_seconds>3.0:errors.append(f"短篇{index}首个钩子片段必须不超过3秒，当前{first_seconds:.1f}秒")
    return errors


def valid_story_plan(plan,assets):return not story_plan_errors(plan,assets)


def _chapter_names(intent,context):
    values=[]
    for value in intent.get("chapters") or []:
        name=value.get("name") if isinstance(value,dict) else value
        if str(name or "").strip():values.append(str(name).strip())
    if 2<=len(values)<=6:return values
    if "浅草" in context:return ["出发与近况","浅草寺与街景","朋友相聚与饮食","中日手势与余韵"]
    return list(DEFAULT_CHAPTERS)


def _anchor_ids(intent,assets,context):
    valid={int(asset["id"]) for asset in assets};anchor=intent.get("story_anchor") if isinstance(intent.get("story_anchor"),dict) else {}
    result=[]
    for value in anchor.get("asset_ids") or []:
        try:value=int(value)
        except (TypeError,ValueError):continue
        if value in valid and value not in result:result.append(value)
    dialogue=[asset for asset in assets if _captions(asset)]
    if ("手势" in context or "最后一段" in context) and dialogue:
        last=int(max(dialogue,key=lambda asset:int(asset["id"]))["id"])
        if last not in result:result.insert(0,last)
    if not result and dialogue:
        best=max(dialogue,key=lambda asset:(len(_captions(asset)),float((_analysis(asset).get("visual") or {}).get("story_value") or 0),int(asset["id"])))
        result.append(int(best["id"]))
    return result[:4]


def _asset_preferences(intent):
    priorities={};chapters={}
    for value in intent.get("asset_priorities") or []:
        if not isinstance(value,dict):continue
        try:asset_id=int(value.get("asset_id"));priority=max(0.0,min(100.0,float(value.get("priority",50))))
        except (TypeError,ValueError):continue
        priorities[asset_id]=priority
        if str(value.get("chapter") or "").strip():chapters[asset_id]=str(value["chapter"]).strip()
    return priorities,chapters


def _recommended_long_target(intent,assets,anchor_ids):
    available=sum(_duration(asset) for asset in assets);priorities,_=_asset_preferences(intent);useful=0.0
    for asset in assets:
        asset_id=int(asset["id"]);analysis=_analysis(asset);visual=analysis.get("visual") or {}
        try:story=max(0.0,min(100.0,float(visual.get("story_value") or 50)))/100
        except (TypeError,ValueError):story=0.5
        try:quality=max(0.0,min(100.0,float(visual.get("quality") or 50)))/100
        except (TypeError,ValueError):quality=0.5
        caption_signal=min(len(_captions(asset)),30)/30;priority=priorities.get(asset_id,50)/100
        keep_ratio=0.10+0.16*story+0.06*quality+0.12*caption_signal+0.10*priority+(0.06 if asset_id in anchor_ids else 0)
        if str(visual.get("suggested_use") or "")=="弃用":keep_ratio=min(keep_ratio,0.06)
        useful+=min(_duration(asset),420.0 if asset_id in anchor_ids else 360.0)*keep_ratio
    content_target=max(600.0,min(3600.0,available*0.92,useful))
    try:ai_minutes=float(intent.get("long_target_minutes"))
    except (TypeError,ValueError):ai_minutes=0.0
    if 10<=ai_minutes<=60:
        suggested=max(600.0,min(3600.0,available*0.92,ai_minutes*60))
        content_target=content_target*0.72+suggested*0.28
    return max(600.0,min(3600.0,available*0.92,content_target))


def _allocate_budgets(assets,target,intent,anchor_ids):
    priorities,_=_asset_preferences(intent);allocations={int(asset["id"]):0.0 for asset in assets};caps={int(asset["id"]):min(_duration(asset),420.0 if int(asset["id"]) in anchor_ids else 360.0) for asset in assets}
    if sum(caps.values())<target:caps={int(asset["id"]):_duration(asset) for asset in assets}
    weights={}
    for asset in assets:
        asset_id=int(asset["id"]);analysis=_analysis(asset);visual=analysis.get("visual") or {};caption_count=len(_captions(asset));story=float(visual.get("story_value") or 50);priority=priorities.get(asset_id,50)
        weights[asset_id]=1.0+story/100+min(caption_count,30)/12+priority/100+(0.8 if asset_id in anchor_ids else 0)
    remaining=target
    for _ in range(20):
        active=[asset_id for asset_id in allocations if caps[asset_id]-allocations[asset_id]>1e-6]
        if remaining<=1e-5:break
        if not active:raise RuntimeError("素材可用区间不足，无法达到长篇目标时长")
        weight_sum=sum(weights[asset_id] for asset_id in active);added=0.0
        for asset_id in active:
            delta=min(caps[asset_id]-allocations[asset_id],remaining*weights[asset_id]/weight_sum)
            allocations[asset_id]+=delta;added+=delta
        remaining-=added
        if added<=1e-6:break
    if remaining>0.1:raise RuntimeError("无法分配足够的非重复素材区间")
    return allocations


def _caption_ranges(asset):
    ranges=[]
    for caption in _captions(asset):
        try:start=float(caption.get("start"));end=float(caption.get("end"))
        except (TypeError,ValueError):continue
        if end>start:ranges.append((start,end))
    return ranges


def _distributed_segments(asset,budget,chapter):
    duration=_duration(asset)
    if budget<=0.05 or duration<=0:return []
    budget=min(budget,duration);count=max(1,math.ceil(budget/24.0));length=budget/count;gap=(duration-budget)/(count-1) if count>1 else 0;first=(duration-budget)/2 if count==1 else 0;ranges=_caption_ranges(asset);result=[]
    for index in range(count):
        start=first+index*(length+gap) if count>1 else first;end=min(duration,start+length);dialogue=any(start<caption_end and end>caption_start for caption_start,caption_end in ranges)
        result.append({"asset_id":int(asset["id"]),"start":round(start,3),"end":round(end,3),"chapter":chapter,"reason":"保留真实对话与朋友互动" if dialogue else "交代移动、街景、饮食或现场细节","audio_mode":"dialogue" if dialogue else "montage"})
    return result


def _fallback_shorts(assets,anchor_ids,context):
    hand="手势" in context;anchor=anchor_ids[:1]
    first={"title":"中日数字手势差异" if hand else "旅途中最意外的一刻","hook":"中日数字手势，真的一样吗？" if hand else "这段旅程最意外的瞬间是什么？","cover_text":"中日手势竟然不同" if hand else "意外的一刻","core_payoff":"用真实对话和反应给出答案","voice_mode":"selective_dialogue","voice_reason":"主题需要一小段真实对话才能成立","asset_ids":anchor}
    ranked=sorted(assets,key=lambda asset:(len(_captions(asset)),float((_analysis(asset).get("visual") or {}).get("story_value") or 0)),reverse=True);second_ids=[int(asset["id"]) for asset in ranked if int(asset["id"]) not in anchor_ids][:3]
    second={"title":"朋友相聚与东京烟火气","hook":"黄金周的东京，最值得记住的是什么？" if "黄金周" in context else "这一天最值得记住的是什么？","cover_text":"朋友与城市烟火气","core_payoff":"用饮食、街景和交流形成回环","voice_mode":"bgm_only","voice_reason":"画面和音乐足以完成情绪叙事","asset_ids":second_ids}
    return [first,second]


def _short_definitions(intent,assets,anchor_ids,context):
    valid={int(asset["id"]) for asset in assets};definitions=[]
    for value in intent.get("shorts") or []:
        if not isinstance(value,dict):continue
        ids=[]
        for asset_id in value.get("asset_ids") or []:
            try:asset_id=int(asset_id)
            except (TypeError,ValueError):continue
            if asset_id in valid and asset_id not in ids:ids.append(asset_id)
        item=dict(value);item["asset_ids"]=ids;definitions.append(item)
    if not definitions:definitions=_fallback_shorts(assets,anchor_ids,context)
    if "手势" in context and not any("手势" in json.dumps(value,ensure_ascii=False) for value in definitions):definitions=_fallback_shorts(assets,anchor_ids,context)[:1]+definitions
    if "手势" in context:
        hand=next((value for value in definitions if "手势" in json.dumps(value,ensure_ascii=False)),None)
        if hand is not None:
            hand["asset_ids"]=list(dict.fromkeys(anchor_ids+list(hand.get("asset_ids") or [])))
    return definitions[:1]


def _short_profiles(index,style_seed=None):
    profiles=[
        {"name":"culture_hook","target_seconds":48.0,"target_clips":13,"dialogue_limit":4.4,"montage_limit":2.5,"accent_positions":{4:"smoothleft",8:"fadewhite",12:"slideup"}},
        {"name":"warm_rhythm","target_seconds":46.0,"target_clips":14,"dialogue_limit":3.9,"montage_limit":2.8,"accent_positions":{5:"wipeleft",10:"fadeblack",13:"smoothup"}},
        {"name":"story_reveal","target_seconds":52.0,"target_clips":13,"dialogue_limit":4.8,"montage_limit":2.7,"accent_positions":{4:"smoothup",9:"wiperight",12:"fade"}},
        {"name":"meme_reaction","target_seconds":42.0,"target_clips":15,"dialogue_limit":3.6,"montage_limit":2.2,"accent_positions":{3:"fadewhite",7:"slideup",11:"fadeblack",14:"smoothleft"}},
        {"name":"city_pulse","target_seconds":45.0,"target_clips":15,"dialogue_limit":3.8,"montage_limit":2.4,"accent_positions":{5:"smoothleft",10:"smoothup",14:"wiperight"}},
        {"name":"contrast_reveal","target_seconds":50.0,"target_clips":13,"dialogue_limit":4.6,"montage_limit":2.6,"accent_positions":{4:"fadeblack",8:"fadewhite",12:"smoothup"}},
    ]
    if style_seed is not None:random.Random(str(style_seed)).shuffle(profiles)
    return profiles[(max(1,index)-1)%len(profiles)]


def _caption_aligned_start(item,asset,lead=0.25):
    start=float(item["start"]);end=float(item["end"])
    if not asset:return start
    overlap=next(((caption_start,caption_end) for caption_start,caption_end in _caption_ranges(asset) if caption_end>start and caption_start<end),None)
    if not overlap:return start
    return max(start,min(end-0.4,overlap[0]-lead))


def _short_effect(profile,item,clip_index):
    if clip_index==0:return "impact_zoom"
    dialogue=item.get("audio_mode")=="dialogue"
    if profile["name"]=="warm_rhythm":return "warm" if not dialogue or clip_index%3 else "punch_in"
    if profile["name"]=="story_reveal":return "soft_contrast" if dialogue else ("subtle_zoom" if clip_index%2 else "clean")
    if profile["name"]=="meme_reaction":return "impact_zoom" if dialogue and clip_index%4==1 else ("punch_in" if dialogue else "clean")
    if profile["name"]=="city_pulse":return "cool" if not dialogue else ("punch_in" if clip_index%3==1 else "clean")
    if profile["name"]=="contrast_reveal":return "soft_contrast" if dialogue else ("warm" if clip_index%3 else "subtle_zoom")
    return "punch_in" if dialogue and clip_index%3==1 else ("subtle_zoom" if not dialogue and clip_index%2 else "clean")


def _short_voice_mode(definition,bgm_led):
    if not bgm_led:return "dialogue_led"
    requested=str(definition.get("voice_mode") or "").strip().lower()
    aliases={
        "bgm_only":"bgm_only","music_only":"bgm_only","none":"bgm_only",
        "selective_dialogue":"selective_dialogue","selective_voice":"selective_dialogue","voice":"selective_dialogue",
    }
    if requested in aliases:return aliases[requested]
    description=" ".join(str(definition.get(key) or "") for key in ("title","hook","core_payoff","pacing")).lower()
    dialogue_signals=("对话","讨论","解释","反应","交流","说","手势","dialogue","talk","voice")
    return "selective_dialogue" if any(value in description for value in dialogue_signals) else "bgm_only"


def _build_short(definition,long_timeline,index,assets_by_id=None,style_seed=None,target_seconds=None,bgm_led=False,rhythm_marks=None):
    assets_by_id=assets_by_id or {};selected={int(value) for value in definition.get("asset_ids") or []};ordered=[];seen=set()
    voice_mode=_short_voice_mode(definition,bgm_led)
    if bgm_led and voice_mode=="bgm_only":
        groups=([item for item in long_timeline if item["asset_id"] in selected and item["audio_mode"]!="dialogue"],[item for item in long_timeline if item["asset_id"] in selected],[item for item in long_timeline if item["audio_mode"]!="dialogue"],long_timeline)
    else:
        groups=([item for item in long_timeline if item["asset_id"] in selected and item["audio_mode"]=="dialogue"],[item for item in long_timeline if item["asset_id"] in selected],[item for item in long_timeline if item["audio_mode"]=="dialogue"],long_timeline)
    for group in groups:
        for item in group:
            key=(item["asset_id"],item["start"],item["end"])
            if key not in seen:seen.add(key);ordered.append(item)
    hook=next((item for item in ordered if item["asset_id"] in selected and item["audio_mode"]=="dialogue"),next((item for item in ordered if item["audio_mode"]=="dialogue"),ordered[0] if ordered else None))
    if hook:ordered=[hook]+[item for item in ordered if item is not hook]
    # Round-robin prevents one highlight from becoming the entire short and gives
    # the rhythm editor real scene changes to cut between.
    interleaved=[];queues={}
    for item in ordered:queues.setdefault(item["asset_id"],[]).append(item)
    asset_order=list(queues)
    if hook and hook["asset_id"] in asset_order:asset_order.remove(hook["asset_id"]);asset_order.insert(0,hook["asset_id"])
    while any(queues.values()):
        for asset_id in asset_order:
            if queues[asset_id]:interleaved.append(queues[asset_id].pop(0))
    profile=_short_profiles(index,style_seed);requested=float(target_seconds if target_seconds is not None else profile["target_seconds"])
    requested=max(10.0,min(600.0,requested))
    target_clips=max(10,min(48,math.ceil(requested/1.75))) if bgm_led else max(profile["target_clips"],math.ceil(requested/3.5))
    rhythm_limit=max(1.15,min(2.05,requested/target_clips*1.08))
    voice_limit=0 if voice_mode=="bgm_only" else (3 if requested>45 else 2)
    flash_points=sorted({round(float(value),3) for value in (rhythm_marks or []) if 1.0<float(value)<requested-1.5}) if bgm_led else []
    clips=[];effective_total=0.0;previous_asset=None;cursors={};voice_clips=[];flash_bursts=[]
    queues={}
    for item in interleaved:queues.setdefault(int(item["asset_id"]),[]).append(item)
    asset_order=list(queues);transition_palette=["smoothleft","fadewhite","slideup","fadeblack","smoothup","wiperight","fade"]
    if style_seed is not None:random.Random(f"{style_seed}:transitions").shuffle(transition_palette)
    while effective_total<requested-0.03 and any(queues.values()):
        progressed=False
        for asset_id in asset_order:
            if not queues[asset_id] or effective_total>=requested-0.03:continue
            item=queues[asset_id].pop(0);key=(asset_id,float(item["start"]),float(item["end"]))
            start=cursors.get(key)
            if start is None:start=_caption_aligned_start(item,assets_by_id.get(asset_id)) if item.get("audio_mode")=="dialogue" else float(item["start"])
            end=float(item["end"]);available=end-start
            if available<0.4:continue
            clip_index=len(clips)
            if bgm_led:
                limit=min(2.15,rhythm_limit*1.18) if not clips else rhythm_limit
            else:
                limit=2.25 if not clips else (profile["dialogue_limit"] if item.get("audio_mode")=="dialogue" else profile["montage_limit"])
            flash_point=next((value for value in flash_points if value<=effective_total+limit and value+1.35>effective_total),None)
            flash_active=flash_point is not None and effective_total>=flash_point-0.03
            if flash_point is not None and effective_total<flash_point:limit=min(limit,max(0.4,flash_point-effective_total))
            elif flash_active:limit=0.42+0.07*((clip_index+int(flash_point*10))%3)
            transition="cut"
            if clip_index and previous_asset!=asset_id:
                transition=profile["accent_positions"].get(clip_index,"cut")
                if transition=="cut" and clip_index%4==0:transition=transition_palette[(clip_index//4-1)%len(transition_palette)]
            if flash_active:transition="cut"
            transition_duration=0.0 if transition=="cut" else (0.20 if transition in {"fadewhite","fadeblack"} else 0.24)
            needed=requested-effective_total+transition_duration
            length=min(available,limit,needed)
            if length<0.4:continue
            length=round(length,3)
            has_spoken_caption=any(start<caption_end and start+length>caption_start for caption_start,caption_end in _caption_ranges(assets_by_id.get(asset_id) or {}))
            keep_voice=item.get("audio_mode")=="dialogue" and voice_mode!="bgm_only" and (not bgm_led or (has_spoken_caption and len(voice_clips)<voice_limit and (not voice_clips or effective_total>=requested*0.55)))
            audio_mode=("dialogue" if item.get("audio_mode")=="dialogue" else "montage") if not bgm_led else ("dialogue" if keep_voice else "mute")
            show_captions=audio_mode=="dialogue"
            effect="flash_frame" if flash_active and clip_index%2==0 else _short_effect(profile,{**item,"audio_mode":audio_mode},clip_index)
            clips.append({"asset_id":asset_id,"start":round(start,3),"end":round(start+length,3),"reason":"BGM能量点快闪" if flash_active else ("前2秒真实对话钩子" if not clips and keep_voice else "按BGM节拍在动作、反应和环境细节之间推进"),"audio_mode":audio_mode,"show_captions":show_captions,"effect":effect,"transition":transition,"transition_duration":transition_duration})
            if flash_active and flash_point not in flash_bursts:flash_bursts.append(flash_point)
            if keep_voice:voice_clips.append(clip_index+1)
            effective_total=_effective_short_seconds(clips);previous_asset=asset_id;progressed=True
            cursors[key]=clips[-1]["end"]
            if end-cursors[key]>=0.4:queues[asset_id].append(item)
            if len(clips)>=max(target_clips,math.ceil(requested/0.4)+2):break
        if not progressed:break
    residual=requested-_effective_short_seconds(clips)
    if 0<residual<0.4 and clips:
        last=clips[-1];source_limit=max((float(value["end"]) for value in long_timeline if int(value["asset_id"])==int(last["asset_id"]) and float(value["start"])<=float(last["end"])<float(value["end"])),default=float(last["end"]))
        extension=min(residual,max(0.0,source_limit-float(last["end"])))
        if extension>0:last["end"]=round(float(last["end"])+extension,3)
        effective_total=_effective_short_seconds(clips)
    minimum_clips=max(4,min(8,math.ceil(requested/4)))
    if len(clips)<minimum_clips or effective_total<requested*0.92:raise RuntimeError(f"短篇{index}多素材片段不足，目标{requested:.1f}秒、可编排{effective_total:.1f}秒")
    return {"title":str(definition.get("title") or f"短篇 {index}"),"hook":str(definition.get("hook") or "这段真实经历最后发生了什么？"),"cover_text":str(definition.get("cover_text") or definition.get("title") or f"短篇 {index}"),"core_payoff":str(definition.get("core_payoff") or "用真实画面和对话回答开头问题"),"pacing":str(definition.get("pacing") or "按BGM节拍密集切换，精选人声只保留关键一句"),"editorial_style":"douyin_polished_v3_bgm_led" if bgm_led else "douyin_polished_v2","style_profile":profile["name"],"voice_mode":voice_mode,"voice_reason":str(definition.get("voice_reason") or ("主题需要真实人声点题" if voice_mode=="selective_dialogue" else "画面与BGM足以完成叙事")),"caption_policy":"voice_only" if voice_clips else "none","voice_clips":voice_clips,"flash_bursts":flash_bursts,"timeline":clips}


def rebuild_short_plans(existing_shorts,long_timeline,assets,style_seed,target_seconds=None,max_outputs=1,bgm_led=False,rhythm_marks=None):
    """Rebuild short edits from an already approved/revised long timeline.

    The selected source-video set comes from each prior short, while every actual
    time range is reselected from ``long_timeline``. This keeps manual removals
    (smoking, phone operation, etc.) out of subsequent short versions.
    """
    assets_by_id={int(asset["id"]):asset for asset in assets};valid=set(assets_by_id);definitions=[]
    for index,short in enumerate(existing_shorts or [],1):
        if not isinstance(short,dict):continue
        asset_ids=[]
        for item in short.get("timeline") or []:
            try:asset_id=int(item.get("asset_id"))
            except (TypeError,ValueError,AttributeError):continue
            if asset_id in valid and asset_id not in asset_ids:asset_ids.append(asset_id)
        definition={key:short.get(key) for key in ("title","hook","cover_text","core_payoff","pacing","voice_mode","voice_reason") if short.get(key)};definition["asset_ids"]=asset_ids;definitions.append(definition)
        if len(definitions)>=max(1,min(3,int(max_outputs or 1))):break
    if not definitions:raise RuntimeError("没有可复用的短篇方案，不能安全重建短篇版本")
    shorts=[_build_short(definition,long_timeline,index,assets_by_id,style_seed,target_seconds,bgm_led,rhythm_marks) for index,definition in enumerate(definitions,1)]
    for index,short in enumerate(shorts,1):
        _,errors=timeline_stats(short["timeline"],assets);seconds=_effective_short_seconds(short["timeline"])
        if errors or not 10<=seconds<=600.05:raise RuntimeError(f"短篇{index}时间线校验失败："+"；".join(errors or [f"有效时长{seconds:.1f}秒"]))
    return shorts


def assemble_story_plan(intent,assets,context="",used_fallback=False):
    intent=intent if isinstance(intent,dict) else {};usable=[asset for asset in assets if _duration(asset)>=0.4];available=sum(_duration(asset) for asset in usable)
    if available<600:raise RuntimeError(f"素材总时长仅{available:.1f}秒，不足以制作10分钟长篇")
    chapters=_chapter_names(intent,context);anchor_ids=_anchor_ids(intent,usable,context);target=_recommended_long_target(intent,usable,anchor_ids);allocations=_allocate_budgets(usable,target,intent,anchor_ids);_,chapter_preferences=_asset_preferences(intent);timeline=[]
    for position,asset in enumerate(usable):
        default_chapter=chapters[min(len(chapters)-1,int(position*len(chapters)/max(1,len(usable))))];chapter=chapter_preferences.get(int(asset["id"]),default_chapter)
        if chapter not in chapters:chapter=default_chapter
        timeline.extend(_distributed_segments(asset,allocations[int(asset["id"])],chapter))
    for chapter in chapters:
        candidate=next((item for item in timeline if item["chapter"]==chapter and item["audio_mode"]=="montage"),None)
        if candidate:candidate["audio_mode"]="ambient"
    style_seed=intent.get("short_style_seed");assets_by_id={int(asset["id"]):asset for asset in usable};shorts=[_build_short(definition,timeline,index,assets_by_id,style_seed) for index,definition in enumerate(_short_definitions(intent,usable,anchor_ids,context),1)]
    anchor=intent.get("story_anchor") if isinstance(intent.get("story_anchor"),dict) else {}
    plan={"title":str(intent.get("title") or ("黄金周的东京散步" if "黄金周" in context else "一日生活记录")),"summary":str(intent.get("summary") or "以真实移动、街景、饮食和朋友交流串起当天经历。"),"long":{"target_seconds":round(target,1),"selection_ratio":round(target/available,4),"story_anchor":{"topic":str(anchor.get("topic") or ("中日数字手势的差异" if "手势" in context else "朋友交流中的意外发现")),"setup":str(anchor.get("setup") or "在旅程开头留下问题"),"payoff":str(anchor.get("payoff") or "在真实对话中展开并于结尾回扣"),"asset_ids":anchor_ids},"chapters":[{"name":name} for name in chapters],"timeline":timeline},"shorts":shorts,"short_style_seed":style_seed,"music_mood":intent.get("music_mood") if isinstance(intent.get("music_mood"),list) else [str(intent.get("music_mood") or "轻快日常、克制温暖")],"review_warnings":list(intent.get("review_warnings") or []) if isinstance(intent.get("review_warnings"),list) else [],"generation_method":"compact_ai_plus_deterministic_timeline_v4_dynamic_duration"}
    if used_fallback:plan["review_warnings"].append("AI创意结构未能解析，已使用确定性故事结构回退；时间线仍已通过真实素材校验。")
    errors=story_plan_errors(plan,assets)
    if errors:raise RuntimeError("确定性时间线构建失败："+"；".join(errors[:12]))
    return plan

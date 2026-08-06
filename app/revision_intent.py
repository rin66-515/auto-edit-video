import re

from .editorial_rules import parse_long_replan_directives


FILENAME_RE=re.compile(r"(?P<filename>[A-Za-z0-9_-]*_(?P<code>\d{4})_D\.MP4)",re.IGNORECASE)
OUTPUT_MINUTE_RANGE_RE=re.compile(r"(?<!\d)(?P<start>\d+(?:\.\d+)?)\s*(?:到|至|[-–—~～])\s*(?P<end>\d+(?:\.\d+)?)\s*分钟")
TIME_TOKEN_PATTERN=r"(?:\d{1,3}:\d{2}(?:\.\d+)?|\d+\s*分\s*\d+(?:\.\d+)?\s*(?:秒)?|\d+(?:\.\d+)?\s*秒)"
TIME_TOKEN_RE=re.compile(TIME_TOKEN_PATTERN)
TIME_RANGE_RE=re.compile(rf"(?P<start>{TIME_TOKEN_PATTERN})\s*(?:到|至|[-–—~～])\s*(?P<end>{TIME_TOKEN_PATTERN})")
PREFERRED_SIGNALS=("推荐","尽可能","可以加入","可加入","也可以","优先候选","优先推荐")
REQUIRED_SIGNALS=("替换","换为","改为","必须","固定","仅此","务必")
DELETE_SIGNALS=("删除","切掉","不要","去掉","删掉")
INSERT_SIGNALS=("插入","接入","放入")
REDUCE_SIGNALS=("减少镜头","删减镜头","缩短镜头","压缩镜头数量")
FULL_REPLAN_KIND="full_replan"


def revision_mode(revisions):
    """Version feedback is incremental unless the user selected full replan."""
    kinds={str(value.get("kind") or "") for value in revisions if isinstance(value,dict)}
    return "full_replan" if FULL_REPLAN_KIND in kinds else "incremental"


def _number_seconds(value):
    text=str(value or "").strip()
    try:return float(text)
    except ValueError:pass
    simple={"零":0,"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}
    return float(simple[text]) if text in simple else None


def parse_audio_revision(revisions):
    bodies=[str(value.get("body") or "") for value in revisions if isinstance(value,dict)]
    text="\n".join(bodies).strip()
    audio_signal=bool(re.search(r"BGM|背景音乐|背景音|人声|原声|说话|音量|降噪",text,re.IGNORECASE))
    if not audio_signal:
        return {"matched":False,"text":text,"summary":[]}
    target_clip=_target_clip(text)
    threshold=None
    time_match=re.search(r"(?:第)?([零一二两三四五六七八九十]|\d+(?:\.\d+)?)\s*秒",text)
    if not time_match:
        time_match=re.search(r"到\s*(\d+(?:\.\d+)?)\s*(?:秒)?\s*后",text)
    if time_match:threshold=_number_seconds(time_match.group(1))
    duck_on_dialogue=bool(re.search(r"(?:出现|开始|听到).{0,8}(?:人声|说话)|(?:人声|说话).{0,8}(?:出现|开始)|说话才",text))
    reduce_bgm=bool(re.search(r"(?:减小|降低|压低|削减).{0,8}(?:BGM|背景音乐|背景音)|(?:BGM|背景音乐|背景音).{0,8}(?:减小|降低|压低|削减)",text,re.IGNORECASE))
    cleanup=bool(re.search(r"降噪|清理背景|削减背景|降低背景",text))
    summary=["默认继承来源版本的全部镜头、顺序、转场和时长"]
    if duck_on_dialogue:
        label=f"成片 {threshold:g} 秒之后的首个有效人声" if threshold is not None else "首个有效人声"
        summary.append(f"BGM 保持到{label}，随后再压低并突出人声")
    elif reduce_bgm:
        summary.append("仅调整 BGM 与人声混合，不重新规划画面")
    return {
        "matched":True,"text":text,"target_clip_index":target_clip,
        "requested_after_seconds":threshold,
        "duck_on_dialogue_onset":duck_on_dialogue,
        "reduce_bgm":reduce_bgm,"background_cleanup":cleanup,
        "summary":summary,
    }


def _parse_time_token(value):
    token=str(value or "").strip()
    colon=re.fullmatch(r"(\d{1,3}):(\d{2})(?:\.(\d+))?",token)
    if colon:
        fraction=float(f"0.{colon.group(3)}") if colon.group(3) else 0.0
        return int(colon.group(1))*60+int(colon.group(2))+fraction
    minutes=re.fullmatch(r"(\d+)\s*分\s*(\d+(?:\.\d+)?)\s*(?:秒)?",token)
    if minutes:return int(minutes.group(1))*60+float(minutes.group(2))
    seconds=re.fullmatch(r"(\d+(?:\.\d+)?)\s*秒",token)
    if seconds:return float(seconds.group(1))
    return None


def _format_seconds(value):
    seconds=max(0.0,float(value or 0));minutes=int(seconds//60);rest=seconds-minutes*60
    return f"{minutes}:{rest:05.2f}".rstrip("0").rstrip(".")


def _time_from_line(line):
    match=TIME_TOKEN_RE.search(line)
    if match:return _parse_time_token(match.group(0))
    arrow=re.search(r"[→➡]\s*(\d+(?:\.\d+)?)",line)
    return float(arrow.group(1)) if arrow else None


def _target_clip(line):
    match=re.search(r"(?:母版|镜头)\s*0*(\d+)",line,re.IGNORECASE)
    return int(match.group(1)) if match else None


def _clip_count(text,source_clip_count):
    explicit=re.search(r"(?:保持|维持|保留)?\s*(\d+)\s*(?:段|个)?\s*镜头",text)
    if explicit:return max(1,int(explicit.group(1))),True
    negated_reduction=bool(re.search(r"(?:不要|不得|禁止).{0,8}(?:减少|删减|缩短).{0,5}镜头|(?:没有|未).{0,10}(?:要求|提到).{0,8}(?:减少|删减|缩短)",text))
    reduce_requested=not negated_reduction and any(signal in text for signal in REDUCE_SIGNALS)
    return (int(source_clip_count),True) if source_clip_count and not reduce_requested else (None,not reduce_requested)


def _line_priority(line):
    if any(signal in line for signal in REQUIRED_SIGNALS):return "required"
    if any(signal in line for signal in PREFERRED_SIGNALS):return "preferred"
    return "preferred"


def _output_ranges(text):
    result=[]
    for match in OUTPUT_MINUTE_RANGE_RE.finditer(text):
        start=float(match.group("start"))*60;end=float(match.group("end"))*60
        if end<start:start,end=end,start
        if end-start<0.5:continue
        result.append({
            "start_seconds":round(start,3),
            "end_seconds":round(end,3),
            "action":"rebalance",
        })
    return result


def _output_deletions(text):
    result=[];seen=set()
    for raw_line in text.splitlines():
        line=raw_line.strip().strip("*-• ")
        delete_line=any(signal in line for signal in DELETE_SIGNALS) and not any(signal in line for signal in ("不要擅自","没有提到","未提到","不得"))
        if not delete_line:continue
        first_filename=FILENAME_RE.search(line)
        for match in TIME_RANGE_RE.finditer(line):
            if first_filename and match.start()>first_filename.start() and not re.search(r"成片|输出|镜头",line[:match.start()]):
                continue
            start=_parse_time_token(match.group("start"));end=_parse_time_token(match.group("end"))
            if start is None or end is None:continue
            if end<start:start,end=end,start
            if end-start<0.04:continue
            key=(round(start,3),round(end,3))
            if key in seen:continue
            seen.add(key);result.append({"start_seconds":key[0],"end_seconds":key[1],"action":"cut","label":line})
    return result


def _exact_insertions_from_line(line,matches):
    if len(matches)!=1 or not any(signal in line for signal in INSERT_SIGNALS):return []
    filename_match=matches[0];prefix=line[:filename_match.start()]
    output_times=list(TIME_TOKEN_RE.finditer(prefix))
    if not output_times:return []
    output_match=output_times[-1]
    if not ("后" in prefix[output_match.end():] or re.search(r"成片|输出",prefix)):
        return []
    source_range=TIME_RANGE_RE.search(line,filename_match.end())
    if not source_range:return []
    output_at=_parse_time_token(output_match.group(0))
    source_start=_parse_time_token(source_range.group("start"));source_end=_parse_time_token(source_range.group("end"))
    if output_at is None or source_start is None or source_end is None:return []
    if source_end<source_start:source_start,source_end=source_end,source_start
    if source_end-source_start<0.04:return []
    return [{
        "filename":filename_match.group("filename"),"asset_code":filename_match.group("code"),
        "output_at_seconds":round(output_at,3),"source_start_seconds":round(source_start,3),
        "source_end_seconds":round(source_end,3),"priority":"required","label":line,
    }]


def _filename_specs(line,matches):
    specs=[{"filename":match.group("filename"),"asset_code":match.group("code")} for match in matches]
    expanded=list(specs)
    for index,(left,right) in enumerate(zip(matches,matches[1:])):
        connector=line[left.end():right.start()]
        if not re.fullmatch(r"\s*(?:到|至|[-–—~～])\s*",connector):continue
        start=int(left.group("code"));end=int(right.group("code"))
        if start==end or abs(end-start)>20:continue
        step=1 if end>start else -1
        additions=[
            {"filename":None,"asset_code":f"{code:04d}"}
            for code in range(start+step,end,step)
        ]
        insert_at=expanded.index(specs[index+1])
        expanded[insert_at:insert_at]=additions
    return expanded


def parse_revision_intent(revisions,source_clip_count=None):
    """Parse natural-language short-edit feedback into deterministic rules."""
    bodies=[str(value.get("body") or "") for value in revisions if isinstance(value,dict)]
    text="\n".join(bodies).strip()
    target_clip_count,preserve_clip_count=_clip_count(text,source_clip_count)
    recommendations=[];deletions=[];insertions=[];warnings=[]
    for raw_line in text.splitlines():
        line=raw_line.strip().strip("*-• ")
        if not line:continue
        matches=list(FILENAME_RE.finditer(line))
        if not matches:continue
        timestamp=_time_from_line(line);time_mode="after" if re.search(r"(?:后|以后|之后)",line) else "around"
        target_clip_index=_target_clip(line)
        wants_voice=bool(re.search(r"(?:保留|出现|使用).{0,6}(?:原声|人声)|(?:原声|人声).{0,6}(?:保留|出现|使用)",line))
        mute_voice=bool(re.search(r"(?:不要|无需|关闭|不保留).{0,5}(?:原声|人声)",line)) and not (wants_voice and "仅此" in line)
        background_cleanup=bool(re.search(r"降噪|清理背景|削减背景|降低背景",line))
        duck_bgm=bool(re.search(r"(?:降低|压低|削减).{0,5}(?:BGM|背景音|背景音乐)|(?:BGM|背景音|背景音乐).{0,8}(?:降低|压低|削减)|人声出现",line,re.IGNORECASE))
        delete_line=any(signal in line for signal in DELETE_SIGNALS) and not any(signal in line for signal in ("不要擅自","没有提到","未提到","不得"))
        exact_insertions=_exact_insertions_from_line(line,matches)
        if exact_insertions:
            for value in exact_insertions:
                value.update({
                    "audio_mode":"dialogue" if wants_voice and not mute_voice else None,
                    "background_cleanup":background_cleanup,"duck_bgm":duck_bgm,
                    "show_captions":False,
                })
            insertions.extend(exact_insertions);continue
        for spec in _filename_specs(line,matches):
            value={
                "filename":spec["filename"],"asset_code":spec["asset_code"],
                "time_seconds":round(timestamp,3) if timestamp is not None else None,
                "time_mode":time_mode,"target_clip_index":target_clip_index,
                "priority":_line_priority(line),"label":line,
                "audio_mode":"dialogue" if wants_voice and not mute_voice else "mute",
                "background_cleanup":background_cleanup,"duck_bgm":duck_bgm,
                "show_captions":False,
            }
            (deletions if delete_line else recommendations).append(value)
    seen=set();deduped=[]
    for item in recommendations:
        key=(item["asset_code"],item.get("time_seconds"),item.get("target_clip_index"),item.get("audio_mode"))
        if key in seen:continue
        seen.add(key);deduped.append(item)
    recommendations=deduped
    only_dialogue=any(re.search(r"(?:仅此|只有这|只在这).{0,10}(?:原声|人声)",line) for line in text.splitlines())
    output_ranges=_output_ranges(text)
    output_deletions=_output_deletions(text)
    if text and not recommendations and not insertions and FILENAME_RE.search(text):warnings.append("检测到素材文件名，但未能形成有效候选镜头")
    summary=[]
    if target_clip_count:summary.append(f"目标镜头数 {target_clip_count}，未要求删减时保持来源母版数量")
    if recommendations:
        required=sum(1 for value in recommendations if value["priority"]=="required")
        preferred=len(recommendations)-required
        summary.append(f"解析到 {required} 个必须镜头、{preferred} 个优先推荐镜头")
    for value in output_ranges:
        summary.append(f"重平衡成片 {value['start_seconds']/60:g}–{value['end_seconds']/60:g} 分钟区间，并保持区间外镜头不变")
    for value in output_deletions:
        summary.append(f"删除成片 {value['start_seconds']/60:.2f}–{value['end_seconds']/60:.2f} 分钟，共 {value['end_seconds']-value['start_seconds']:.1f} 秒")
    for value in insertions:
        summary.append(f"在成片 {_format_seconds(value['output_at_seconds'])} 后插入 {value['asset_code']} 的 {_format_seconds(value['source_start_seconds'])}–{_format_seconds(value['source_end_seconds'])}")
    voice=sum(1 for value in recommendations if value["audio_mode"]=="dialogue")
    if voice:summary.append(f"{voice} 个镜头保留清理后人声；其余镜头静音" if only_dialogue else f"{voice} 个镜头保留清理后人声")
    summary.append("同一原片可选取相距较远的不同精彩片段；禁止重叠或相邻伪重复画面")
    result={
        "schema":1,"text":text,"source_clip_count":source_clip_count,
        "target_clip_count":target_clip_count,"preserve_clip_count":preserve_clip_count,
        "allow_same_asset_distinct_ranges":True,"dedupe_visual_ranges":True,
        "only_specified_dialogue":only_dialogue,"recommendations":recommendations,
        "deletions":deletions,"output_ranges":output_ranges,
        "output_deletions":output_deletions,"insertions":insertions,
        "has_local_timeline_edits":bool(output_deletions or insertions),
        "deletion_preference":"dominant_asset_first" if output_ranges and re.search(r"太多|减少|删减|压缩",text) else None,
        "warnings":warnings,"summary":summary,
    }
    result["revision_mode"]=revision_mode(revisions)
    if result["revision_mode"]=="full_replan" and result["has_local_timeline_edits"]:
        result["warnings"].append("检测到明确成片时间码，必须使用局部剪辑调整；完整重规划将被拒绝")
    if result["revision_mode"]=="full_replan" and ("长篇" in text or "16:9" in text):
        long_replan=parse_long_replan_directives(revisions)
        if long_replan["summary"]:
            result["long_replan"]=long_replan
            result["summary"]=long_replan["summary"]+result["summary"]
    audio=parse_audio_revision(revisions)
    if audio.get("matched"):
        result["audio_revision"]=audio
        result["summary"]=audio["summary"]+[value for value in result["summary"] if value not in audio["summary"]]
    elif result["revision_mode"]=="incremental":
        result["summary"]=["默认继承来源版本，仅修改意见明确指出的内容"]+result["summary"]
    else:
        result["summary"]=["已明确选择完整重规划；将重新生成整条时间线"]+result["summary"]
    return result

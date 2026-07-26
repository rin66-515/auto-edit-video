import re


FILENAME_RE=re.compile(r"(?P<filename>[A-Za-z0-9_-]*_(?P<code>\d{4})_D\.MP4)",re.IGNORECASE)
PREFERRED_SIGNALS=("推荐","尽可能","可以加入","可加入","也可以","优先候选","优先推荐")
REQUIRED_SIGNALS=("替换","换为","改为","必须","固定","仅此","务必")
DELETE_SIGNALS=("删除","切掉","不要","去掉","删掉")
REDUCE_SIGNALS=("减少镜头","删减镜头","缩短镜头","压缩镜头数量")


def _time_from_line(line):
    colon=re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?:\.(\d+))?",line)
    if colon:
        fraction=float(f"0.{colon.group(3)}") if colon.group(3) else 0.0
        return int(colon.group(1))*60+int(colon.group(2))+fraction
    minutes=re.search(r"(\d+)\s*分\s*(\d+(?:\.\d+)?)\s*(?:秒)?",line)
    if minutes:return int(minutes.group(1))*60+float(minutes.group(2))
    seconds=re.search(r"(\d+(?:\.\d+)?)\s*秒",line)
    if seconds:return float(seconds.group(1))
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


def parse_revision_intent(revisions,source_clip_count=None):
    """Parse natural-language short-edit feedback into deterministic rules."""
    bodies=[str(value.get("body") or "") for value in revisions if isinstance(value,dict)]
    text="\n".join(bodies).strip()
    target_clip_count,preserve_clip_count=_clip_count(text,source_clip_count)
    recommendations=[];deletions=[];warnings=[]
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
        for match in matches:
            value={
                "filename":match.group("filename"),"asset_code":match.group("code"),
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
    if text and not recommendations and FILENAME_RE.search(text):warnings.append("检测到素材文件名，但未能形成有效候选镜头")
    summary=[]
    if target_clip_count:summary.append(f"目标镜头数 {target_clip_count}，未要求删减时保持来源母版数量")
    if recommendations:
        required=sum(1 for value in recommendations if value["priority"]=="required")
        preferred=len(recommendations)-required
        summary.append(f"解析到 {required} 个必须镜头、{preferred} 个优先推荐镜头")
    voice=sum(1 for value in recommendations if value["audio_mode"]=="dialogue")
    if voice:summary.append(f"{voice} 个镜头保留清理后人声；其余镜头静音" if only_dialogue else f"{voice} 个镜头保留清理后人声")
    summary.append("同一原片可选取相距较远的不同精彩片段；禁止重叠或相邻伪重复画面")
    return {
        "schema":1,"text":text,"source_clip_count":source_clip_count,
        "target_clip_count":target_clip_count,"preserve_clip_count":preserve_clip_count,
        "allow_same_asset_distinct_ranges":True,"dedupe_visual_ranges":True,
        "only_specified_dialogue":only_dialogue,"recommendations":recommendations,
        "deletions":deletions,"warnings":warnings,"summary":summary,
    }

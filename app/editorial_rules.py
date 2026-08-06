import copy
import re


FORMAT_LABELS={
    "long_16x9":"长篇 16:9",
    "short_9x16":"短篇 9:16",
}

COMMON_RULES="""通用规则：
1. 只能使用本项目真实素材、转写和画面分析，不虚构人物、冲突、地点或事件。
2. 禁止重复或相邻伪重复时间区间；同一原片只有在时间区间明显分离且内容不同的时候才能再次使用。
3. 人工版本意见优先；未明确要求改变的镜头、顺序、转场、时长、字幕、音频和隐私处理必须继承来源版本。
4. 未明确要求时不添加马赛克；人工马赛克只作用于所绑定版本。
5. 使用字幕时必须忠实、自然、保持中日语义一致；听不清或专名不确定时标记人工复核，不能猜测。"""

LONG_RULES="""长篇独有规则：
1. 仅供 YouTube 与 Bilibili 共用，画幅 16:9；不得套用短篇的竖屏、鼓点快闪、爆款转场或 BGM 定长规则。
2. 日系生活纪录风格，允许 10–60 分钟；人工明确时长或范围属于硬约束，优先于素材密度的自动建议。
3. 默认不使用 BGM。重视真实对话、移动过程、街景、饮食细节、章节推进和故事锚点，转场与特效保持克制。
4. 默认清理人声和现场声；环境声只在有叙事价值时保留。人工要求“仅降噪”时，保留所选镜头原声并降噪，不得把非对白区间大面积静音。
5. 字幕在画面母版确定后独立校对和生成。"""

SHORT_RULES="""短篇独有规则：
1. 仅供抖音与小红书共用，画幅 9:16；必须独立检查全部素材，不能只从长篇已选镜头中二次截取。
2. 指定 BGM 时由实际音乐时长和鼓点决定成片长度，不固定为一分钟；允许多组快闪、节奏切换和多种精致转场。
3. 前 1–3 秒直接给出真实问题、反差、反应或结论；镜头紧凑、场景多样，禁止单一精彩片段撑完整条短篇。
4. 母版默认无字幕。AI 只能选择 bgm_only 或 selective_dialogue；只有点题所必需的一至三句才保留降噪人声并生成对应字幕。
5. 不得套用长篇的舒缓留白、章节时长、连续环境声或默认无特效规则。"""


def editorial_overrides(settings):
    settings=settings if isinstance(settings,dict) else {}
    value=settings.get("editorial_rules")
    value=value if isinstance(value,dict) else {}
    return {
        "common":str(value.get("common") or "").strip(),
        "long_16x9":str(value.get("long_16x9") or "").strip(),
        "short_9x16":str(value.get("short_9x16") or "").strip(),
    }


def scoped_rules(fmt,settings=None):
    if fmt not in FORMAT_LABELS:raise ValueError(f"未知规则范围：{fmt}")
    overrides=editorial_overrides(settings)
    fixed=LONG_RULES if fmt=="long_16x9" else SHORT_RULES
    user_common=overrides["common"] or "无"
    user_format=overrides[fmt] or "无"
    return (
        COMMON_RULES+"\n"+fixed+
        f"\n用户通用补充规则：{user_common}"
        f"\n用户{FORMAT_LABELS[fmt]}补充规则：{user_format}"
    )


def scoped_prompt_context(fmt,settings=None,story_context="",revisions=None):
    revision_text="\n".join(str(value.get("body") or "") for value in (revisions or []) if isinstance(value,dict)).strip() or "无"
    return (
        scoped_rules(fmt,settings)+
        f"\n本轮唯一输出范围：{FORMAT_LABELS[fmt]}。忽略故事背景中只指向另一格式的制作要求。"
        f"\n项目事实与故事背景：{str(story_context or '无')}"
        f"\n本轮人工意见：{revision_text}"
    )


def parse_long_replan_directives(revisions):
    text="\n".join(str(value.get("body") or "") for value in (revisions or []) if isinstance(value,dict)).strip()
    duration_min=duration_max=None
    ranged=re.search(r"(\d+(?:\.\d+)?)\s*(?:[-–—~～至到])\s*(\d+(?:\.\d+)?)\s*分钟",text)
    if ranged:
        left=float(ranged.group(1));right=float(ranged.group(2))
        duration_min=max(10.0,min(left,right));duration_max=min(60.0,max(left,right))
    else:
        exact=re.search(r"(?:希望|目标|控制在|做成|生成).{0,12}?(\d+(?:\.\d+)?)\s*分钟",text)
        if exact:
            target=max(10.0,min(60.0,float(exact.group(1))))
            duration_min=duration_max=target
    reanalyze_assets=bool(re.search(r"重新.{0,8}(?:审核|分析|检查).{0,12}(?:全部|所有|\d+\s*(?:部|段))?.{0,8}(?:原素材|素材)|(?:重新审核|重新分析).{0,12}\d+\s*(?:部|段)",text))
    reproofread_captions=bool(re.search(r"重新.{0,12}(?:校对|精校|识别).{0,12}(?:人声|字幕)|(?:人声|字幕).{0,12}重新.{0,8}(?:校对|精校|识别)",text))
    denoise_only=bool(re.search(r"仅\s*(?:做|进行|需要)?\s*降噪|只\s*(?:做|进行|需要)?\s*降噪",text))
    summary=[]
    if duration_min is not None:
        summary.append(
            f"长篇时长硬约束 {duration_min:g}–{duration_max:g} 分钟"
            if duration_min!=duration_max else f"长篇时长硬约束 {duration_min:g} 分钟"
        )
    if reanalyze_assets:summary.append("强制重新分析全部原素材")
    if reproofread_captions:summary.append("强制重新校对人声与中日字幕")
    if denoise_only:summary.append("音频仅降噪并保留所选镜头原声，不大面积静音")
    return {
        "schema":1,
        "text":text,
        "duration_min_minutes":duration_min,
        "duration_max_minutes":duration_max,
        "duration_preferred_minutes":round((duration_min+duration_max)/2,3) if duration_min is not None else None,
        "reanalyze_assets":reanalyze_assets,
        "reproofread_captions":reproofread_captions,
        "audio_policy":"denoise_only" if denoise_only else None,
        "summary":summary,
    }


def inherit_long_replan_defaults(directives,source_snapshot=None,source_options=None):
    """Keep source duration and audio policy unless full-replan feedback changes them."""
    result=copy.deepcopy(directives) if isinstance(directives,dict) else {}
    snapshot=source_snapshot if isinstance(source_snapshot,dict) else {}
    options=source_options if isinstance(source_options,dict) else {}
    timeline=snapshot.get("long") if isinstance(snapshot.get("long"),list) else []
    summary=list(result.get("summary") or [])
    if result.get("duration_min_minutes") is None and timeline:
        seconds=sum(max(0.0,float(item.get("end") or 0)-float(item.get("start") or 0)) for item in timeline if isinstance(item,dict))
        if seconds>0:
            minutes=round(seconds/60,3)
            result.update({
                "duration_min_minutes":minutes,"duration_max_minutes":minutes,
                "duration_preferred_minutes":minutes,"duration_inherited":True,
            })
            summary.append(f"未指定新时长，继承来源版本约 {minutes:g} 分钟")
    if not result.get("audio_policy"):
        stored=str(options.get("long_audio_policy") or "")
        modes={str(item.get("audio_mode") or "") for item in timeline if isinstance(item,dict) and item.get("audio_mode")}
        if stored.startswith("denoise_only") or modes=={"denoise"}:
            result["audio_policy"]="denoise_only"
        elif stored.startswith("speech_aligned") or modes:
            result["audio_policy"]="speech_aligned"
        if result.get("audio_policy"):
            result["audio_policy_inherited"]=True
            label="仅降噪并保留原声" if result["audio_policy"]=="denoise_only" else "对白精确保留、非对白静音"
            summary.append(f"未指定新音频策略，继承来源版本：{label}")
    result["summary"]=summary
    return result


def merge_scoped_story_plan(existing,generated,fmt):
    """Replace exactly one format branch while preserving the other verbatim."""
    existing=existing if isinstance(existing,dict) else {}
    generated=generated if isinstance(generated,dict) else {}
    result=copy.deepcopy(existing)
    metadata=result.get("format_metadata")
    metadata=copy.deepcopy(metadata) if isinstance(metadata,dict) else {}
    if fmt=="long_16x9":
        result["long"]=copy.deepcopy(generated.get("long") or {})
        metadata["long_16x9"]={"title":generated.get("title"),"summary":generated.get("summary")}
    elif fmt=="short_9x16":
        result["shorts"]=copy.deepcopy(generated.get("shorts") or [])
        result["short_style_seed"]=generated.get("short_style_seed")
        metadata["short_9x16"]={"title":generated.get("title"),"summary":generated.get("summary")}
    else:raise ValueError(f"未知计划范围：{fmt}")
    result["format_metadata"]=metadata
    return result

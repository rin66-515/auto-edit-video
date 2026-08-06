import json
from pathlib import Path

from . import db
from .config import MUSIC
from .media import analyze_music_rhythm,probe
from .story_planner import rebuild_short_plans


class RenderPlanError(Exception):
    def __init__(self,status_code,message):
        super().__init__(message)
        self.status_code=status_code


def _asset_analysis(asset):
    value=asset.get("analysis") or {}
    if isinstance(value,dict):return value
    try:return json.loads(value)
    except (TypeError,json.JSONDecodeError):return {}


def _spoken_ranges(asset,start,end):
    analysis=_asset_analysis(asset);ranges=[]
    for caption in analysis.get("bilingual_captions") or []:
        if not isinstance(caption,dict):continue
        try:left=float(caption.get("start"));right=float(caption.get("end"))
        except (TypeError,ValueError):continue
        if right<=left or right<=start or left>=end:continue
        left=max(start,left-0.18);right=min(end,right+0.25)
        if ranges and left<=ranges[-1][1]+0.45:
            ranges[-1]=(ranges[-1][0],max(ranges[-1][1],right))
        else:ranges.append((left,right))
    return ranges


def _append_audio_segment(result,item,start,end,audio_mode,background_cleanup=False):
    if end-start<0.04:return
    segment=dict(item);segment["start"]=round(start,3);segment["end"]=round(end,3);segment["audio_mode"]=audio_mode
    segment.pop("dialogue_start_offset",None);segment.pop("dialogue_fade_seconds",None)
    if audio_mode=="dialogue":
        segment["background_cleanup"]=bool(background_cleanup)
        segment["reason"]="识别到真实对白，按说话时间精确保留并自动降噪"
    elif audio_mode=="ambient":
        segment.pop("background_cleanup",None);segment["reason"]="章节现场声锚点，短暂保留真实环境声"
    else:
        segment.pop("background_cleanup",None);segment["reason"]="非对白区间默认静音，避免环境声持续覆盖长篇"
    result.append(segment)


def refine_long_audio_timeline(timeline,assets,ambient_window_seconds=12.0):
    """Split long-form clips at spoken-caption boundaries and keep ambience sparingly."""
    by_id={int(value["id"]):value for value in assets};result=[]
    for item in timeline if isinstance(timeline,list) else []:
        if not isinstance(item,dict):continue
        try:
            asset=by_id.get(int(item.get("asset_id")));start=float(item.get("start"));end=float(item.get("end"))
        except (TypeError,ValueError):continue
        if not asset or end<=start:continue
        spoken=_spoken_ranges(asset,start,end)
        cleanup_status=str((_asset_analysis(asset).get("audio_cleanup") or {}).get("status") or "").lower()
        strong_cleanup=cleanup_status not in {"enhanced","clean"}
        if spoken:
            cursor=start
            for left,right in spoken:
                _append_audio_segment(result,item,cursor,left,"mute")
                _append_audio_segment(result,item,left,right,"dialogue",strong_cleanup)
                cursor=right
            _append_audio_segment(result,item,cursor,end,"mute")
            continue
        if str(item.get("audio_mode") or "")=="ambient":
            ambient_end=min(end,start+max(1.0,float(ambient_window_seconds)))
            _append_audio_segment(result,item,start,ambient_end,"ambient")
            _append_audio_segment(result,item,ambient_end,end,"mute")
        else:_append_audio_segment(result,item,start,end,"mute")
    return result


def denoise_long_audio_timeline(timeline):
    """Keep every selected clip's original sound while applying one denoise mix."""
    result=[]
    for item in timeline if isinstance(timeline,list) else []:
        if not isinstance(item,dict):continue
        segment=dict(item);segment["audio_mode"]="denoise";segment["background_cleanup"]=True
        segment["reason"]="人工要求仅降噪：保留所选镜头原声，不按对白边界静音"
        segment.pop("dialogue_start_offset",None);segment.pop("dialogue_fade_seconds",None)
        result.append(segment)
    return result


def _selected_bgm(settings):
    short_bgm=settings.get("short_bgm") if isinstance(settings.get("short_bgm"),dict) else {}
    requested=str(short_bgm.get("filename") or "").strip()
    if not requested:return None
    if Path(requested).name!=requested:
        raise RenderPlanError(400,"BGM 只填写音乐库中的文件名，不要填写路径")
    music=(MUSIC/requested).resolve()
    if not music.is_relative_to(MUSIC.resolve()) or not music.is_file():
        raise RenderPlanError(409,f"没有找到指定 BGM：{requested}")
    if music.suffix.lower() not in {".mp3",".wav",".m4a",".aac",".flac"}:
        raise RenderPlanError(400,"BGM 格式仅支持 mp3、wav、m4a、aac、flac")
    try:
        duration=float(probe(music).get("duration") or 0)
    except Exception as exc:
        raise RenderPlanError(409,f"无法读取 BGM：{exc}") from exc
    if not 10<=duration<=600:
        raise RenderPlanError(409,f"BGM 时长须为10秒到10分钟，当前 {duration:.1f} 秒")
    return requested,music,duration


def requested_snapshot(project_id,version,fmt,settings,fallback_snapshot=None):
    plan=settings.get("story_plan") or {}
    if fmt=="long_16x9":
        long_plan=plan.get("long",{}) if isinstance(plan.get("long"),dict) else {}
        timeline=long_plan.get("timeline",[])
        if not timeline and isinstance(fallback_snapshot,dict):
            timeline=fallback_snapshot.get("long",[])
        if not timeline:
            raise RenderPlanError(409,"剪辑方案没有可用的长篇时间线")
        assets=db.rows("SELECT * FROM assets WHERE project_id=? ORDER BY id",(project_id,))
        audio_policy=str(long_plan.get("audio_policy") or "")
        refined=denoise_long_audio_timeline(timeline) if audio_policy=="denoise_only" else refine_long_audio_timeline(timeline,assets)
        if not refined:raise RenderPlanError(409,"长篇音频时间线细化后没有可用镜头")
        policy="denoise_only_no_bgm_v1" if audio_policy=="denoise_only" else "speech_aligned_no_bgm_v1"
        return {"long":refined},{"long_audio_policy":policy}
    shorts=[value for value in (plan.get("shorts") or [])[:1] if isinstance(value,dict)]
    if not shorts:
        fallback_timeline=(fallback_snapshot or {}).get("short-1") if isinstance(fallback_snapshot,dict) else None
        if isinstance(fallback_timeline,list) and fallback_timeline:
            selected=_selected_bgm(settings)
            if selected is None:return {"short-1":fallback_timeline},{"snapshot_fallback":True}
            requested,_,duration=selected
            return {"short-1":fallback_timeline},{"bgm_filename":requested,"bgm_duration":round(duration,3),"snapshot_fallback":True}
        raise RenderPlanError(409,"剪辑方案没有可用的短篇主题")
    selected=_selected_bgm(settings)
    if selected is None:return {"short-1":shorts[0].get("timeline",[])},{}
    requested,music,duration=selected
    assets=db.rows("SELECT * FROM assets WHERE project_id=? ORDER BY id",(project_id,))
    rhythm_marks=analyze_music_rhythm(music,duration)
    rebuilt=rebuild_short_plans(
        shorts,
        plan.get("long",{}).get("timeline",[]),
        assets,
        f"{plan.get('short_style_seed')}:{project_id}:{version}",
        target_seconds=duration,
        max_outputs=1,
        bgm_led=True,
        rhythm_marks=rhythm_marks,
    )
    short=rebuilt[0]
    return {"short-1":short["timeline"]},{
        "bgm_filename":requested,
        "bgm_duration":round(duration,3),
        "short_style_profile":short.get("style_profile"),
        "short_voice_mode":short.get("voice_mode"),
        "short_voice_reason":short.get("voice_reason"),
        "short_voice_clips":short.get("voice_clips") or [],
        "short_flash_bursts":short.get("flash_bursts") or [],
        "caption_policy":short.get("caption_policy"),
    }

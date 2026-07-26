from pathlib import Path

from . import db
from .config import MUSIC
from .media import analyze_music_rhythm,probe
from .story_planner import rebuild_short_plans


class RenderPlanError(Exception):
    def __init__(self,status_code,message):
        super().__init__(message)
        self.status_code=status_code


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
        timeline=plan.get("long",{}).get("timeline",[])
        if not timeline and isinstance(fallback_snapshot,dict):
            timeline=fallback_snapshot.get("long",[])
        if not timeline:
            raise RenderPlanError(409,"剪辑方案没有可用的长篇时间线")
        return {"long":timeline},{}
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

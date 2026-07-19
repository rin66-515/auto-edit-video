import json
import subprocess
import shutil
from pathlib import Path

def run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8")

def probe(path: Path):
    data=json.loads(run(["ffprobe","-v","error","-show_streams","-show_format","-of","json",str(path)]).stdout)
    video=next((s for s in data.get("streams",[]) if s.get("codec_type")=="video"),{})
    rate=video.get("avg_frame_rate","0/1").split("/")
    fps=float(rate[0])/max(float(rate[1]),1) if len(rate)==2 else 0
    return {"duration":float(data.get("format",{}).get("duration") or 0),"width":int(video.get("width") or 0),"height":int(video.get("height") or 0),"fps":round(fps,3),"codec":video.get("codec_name","unknown")}

def create_derivatives(source:Path,proxy:Path,audio:Path,thumb:Path):
    for p in (proxy.parent,audio.parent,thumb.parent): p.mkdir(parents=True,exist_ok=True)
    run(["ffmpeg","-y","-i",str(source),"-vf","scale='min(1280,iw)':-2","-c:v","libx264","-preset","veryfast","-crf","28","-c:a","aac","-b:a","96k",str(proxy)])
    run(["ffmpeg","-y","-i",str(source),"-vn","-ac","1","-ar","48000","-c:a","pcm_s16le",str(audio)])
    run(["ffmpeg","-y","-ss","1","-i",str(source),"-frames:v","1","-vf","scale=640:-2",str(thumb)])

def _srt_time(seconds):
    ms=max(0,int(float(seconds)*1000));h,ms=divmod(ms,3600000);m,ms=divmod(ms,60000);s,ms=divmod(ms,1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def _write_bilingual_srt(used,target):
    lines=[];offset=0.0;idx=1
    for item,asset,length in used:
        clip_start=float(item.get('start',0));clip_end=clip_start+length
        analysis=json.loads(asset.get('analysis') or '{}')
        for caption in analysis.get('bilingual_captions',[]):
            start=float(caption.get('start',caption.get('start_time',0)));end=float(caption.get('end',caption.get('end_time',0)))
            if end<=clip_start or start>=clip_end:continue
            zh=str(caption.get('zh','')).strip();ja=str(caption.get('ja','')).strip();text='\n'.join(x for x in (zh,ja) if x)
            if not text:continue
            out_start=offset+max(start,clip_start)-clip_start;out_end=offset+min(end,clip_end)-clip_start
            lines.extend([str(idx),f"{_srt_time(out_start)} --> {_srt_time(out_end)}",text,'']);idx+=1
        offset+=length
    if lines:target.write_text('\n'.join(lines),encoding='utf-8-sig')
    return bool(lines)

def render_timeline(assets,timeline,fmt: str,target:Path,temp:Path,music_files=()):
    temp.mkdir(parents=True,exist_ok=True);target.parent.mkdir(parents=True,exist_ok=True)
    by_id={int(a['id']):a for a in assets};segments=[];used=[]
    vf="scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2" if fmt=="long_16x9" else "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    for idx,item in enumerate(timeline):
        asset=by_id.get(int(item.get('asset_id',-1)))
        if not asset:continue
        start=max(0,float(item.get('start',0)));end=min(float(asset.get('duration') or 0),float(item.get('end',asset.get('duration') or 0)))
        if end-start<0.25:continue
        segment=temp/f"{idx:04d}.mp4"
        common=["ffmpeg","-y","-ss",str(start),"-t",str(end-start),"-i",asset['path'],"-vf",vf,"-af","highpass=f=80,afftdn=nf=-25,loudnorm=I=-16:TP=-1.5:LRA=11","-r","30"]
        nvenc=common+["-c:v","h264_nvenc","-preset","p4","-cq","23","-b:v","0","-c:a","aac","-b:a","192k",str(segment)]
        cpu=common+["-c:v","libx264","-preset","fast","-crf","23","-c:a","aac","-b:a","192k",str(segment)]
        try:run(nvenc)
        except subprocess.CalledProcessError:
            run(cpu)
        segments.append(segment);used.append((item,asset,end-start))
    if not segments:raise RuntimeError("时间线没有可渲染片段")
    concat=temp/"concat.txt";concat.write_text("\n".join(f"file '{p.as_posix()}'" for p in segments),encoding="utf-8")
    merged=temp/"merged.mp4";run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat),"-c","copy",str(merged)])
    srt=target.with_suffix('.zh-ja.srt');has_subtitles=_write_bilingual_srt(used,srt);music=next(iter(music_files),None)
    if not has_subtitles and not music:shutil.move(str(merged),str(target))
    else:
        inputs=["ffmpeg","-y","-i",str(merged)];maps=[]
        if music:
            inputs += ["-stream_loop","-1","-i",str(music),"-filter_complex","[1:a]volume=0.10[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2[a]","-map","0:v","-map","[a]"]
        if has_subtitles:inputs += ["-vf",f"subtitles=filename='{srt.as_posix()}':force_style='FontName=Noto Sans CJK SC,FontSize=18,Outline=2,MarginV=40'"]
        nvenc=inputs+["-c:v","h264_nvenc","-preset","p4","-cq","23","-b:v","0","-c:a","aac","-b:a","192k","-shortest",str(target)]
        cpu=inputs+["-c:v","libx264","-preset","fast","-crf","23","-c:a","aac","-b:a","192k","-shortest",str(target)]
        try:run(nvenc)
        except subprocess.CalledProcessError:run(cpu)
    shutil.rmtree(temp,ignore_errors=True)
    return target

import json
import math
import re
import subprocess
import shutil
from array import array
from pathlib import Path
from .caption_clean import is_standalone_filler

def _normalized_intervals(intervals):
    cleaned=[]
    for value in intervals or []:
        try:start,end=float(value[0]),float(value[1])
        except (TypeError,ValueError,IndexError):continue
        if end>start:cleaned.append((max(0.0,start),max(0.0,end)))
    merged=[]
    for start,end in sorted(cleaned):
        if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(merged[-1][1],end))
        else:merged.append((start,end))
    return merged

def trim_timeline(timeline,cut_intervals,end_at=None):
    """Remove ranges expressed in the existing output timeline without losing source mapping."""
    cuts=list(_normalized_intervals(cut_intervals))
    if end_at is not None:cuts=_normalized_intervals([*cuts,(float(end_at),float("inf"))])
    result=[];cursor=0.0
    for item in timeline or []:
        try:source_start=float(item.get("start",0));source_end=float(item.get("end",source_start))
        except (TypeError,ValueError):continue
        duration=max(0.0,source_end-source_start);clip_start=cursor;clip_end=cursor+duration;cursor=clip_end
        if duration<0.001:continue
        keep=[(clip_start,clip_end)]
        for cut_start,cut_end in cuts:
            next_keep=[]
            for start,end in keep:
                if cut_end<=start or cut_start>=end:next_keep.append((start,end));continue
                if cut_start>start:next_keep.append((start,min(cut_start,end)))
                if cut_end<end:next_keep.append((max(cut_end,start),end))
            keep=next_keep
            if not keep:break
        for start,end in keep:
            if end-start<0.25:continue
            value=dict(item);value["start"]=round(source_start+start-clip_start,3);value["end"]=round(source_start+end-clip_start,3);result.append(value)
    return result

def shift_interval_after_cuts(start,end,cut_intervals,end_at=None):
    """Map an old-output time range to one or more ranges after timeline cuts."""
    cuts=list(_normalized_intervals(cut_intervals))
    if end_at is not None:cuts=_normalized_intervals([*cuts,(float(end_at),float("inf"))])
    pieces=[(float(start),float(end))]
    for cut_start,cut_end in cuts:
        next_pieces=[]
        for left,right in pieces:
            if cut_end<=left or cut_start>=right:next_pieces.append((left,right));continue
            if cut_start>left:next_pieces.append((left,min(cut_start,right)))
            if cut_end<right:next_pieces.append((max(cut_end,left),right))
        pieces=next_pieces
    result=[]
    for left,right in pieces:
        removed_before=sum(max(0.0,min(left,cut_end)-cut_start) for cut_start,cut_end in cuts if cut_start<left)
        new_left=left-removed_before;removed_inside=sum(max(0.0,min(right,cut_end)-max(left,cut_start)) for cut_start,cut_end in cuts)
        new_right=new_left+(right-left)-removed_inside
        if new_right>new_left:result.append((round(new_left,3),round(new_right,3)))
    return [(round(left,3),round(right,3)) for left,right in _normalized_intervals(result)]

def rendered_time_to_timeline(timeline,seconds,actual_duration,fps=30.0):
    """Map a legacy rendered timestamp back to the saved source timeline."""
    durations=[max(0.0,float(item.get("end",0))-float(item.get("start",0))) for item in timeline or []]
    planned_total=sum(durations);actual_duration=max(0.0,float(actual_duration or 0));seconds=max(0.0,float(seconds or 0))
    if not durations or planned_total<=0 or actual_duration<=0:return min(seconds,planned_total)
    rounded=[math.ceil(duration*fps-1e-9)/fps for duration in durations];rounded_total=sum(rounded)
    if rounded_total<=0:return min(seconds,planned_total)
    scale=actual_duration/rounded_total;actual_cursor=0.0;planned_cursor=0.0
    for planned,encoded in zip(durations,rounded):
        observed=encoded*scale
        if seconds<=actual_cursor+observed or planned_cursor+planned>=planned_total:
            ratio=max(0.0,min(1.0,(seconds-actual_cursor)/max(observed,1e-9)))
            return round(planned_cursor+planned*ratio,6)
        actual_cursor+=observed;planned_cursor+=planned
    return round(planned_total,6)

def run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8")

def _last_black_end(log_text,min_duration=0.25):
    matches=[]
    for start,end,duration in re.findall(r"black_start:([0-9.]+)\s+black_end:([0-9.]+)\s+black_duration:([0-9.]+)",log_text or ""):
        if float(duration)>=min_duration:matches.append((float(start),float(end),float(duration)))
    return matches[-1] if matches else None

def trim_after_final_black(path:Path,search_seconds=5.0):
    """Keep the final black beat and remove any accidental frames that appear after it."""
    metadata=probe(path);duration=float(metadata.get("duration") or 0);window=max(1.0,min(float(search_seconds),duration));start=max(0.0,duration-window)
    result=run(["ffmpeg","-hide_banner","-nostats","-ss",f"{start:.3f}","-i",str(path),"-t",f"{window:.3f}","-vf","setpts=PTS-STARTPTS,blackdetect=d=0.25:pix_th=0.10","-an","-f","null","-"])
    match=_last_black_end(result.stderr)
    if not match:return {"trimmed":False,"duration_before":duration}
    trim_at=min(duration,start+match[1])
    if duration-trim_at<0.08:return {"trimmed":False,"duration_before":duration,"black_end":trim_at}
    temporary=path.with_name(path.stem+".final-black.tmp"+path.suffix);temporary.unlink(missing_ok=True)
    run(["ffmpeg","-y","-i",str(path),"-t",f"{trim_at:.3f}","-c","copy","-movflags","+faststart",str(temporary)])
    temporary.replace(path)
    return {"trimmed":True,"duration_before":duration,"trim_at":round(trim_at,3),"removed_seconds":round(duration-trim_at,3),"black_duration":round(match[2],3)}

def probe(path: Path):
    data=json.loads(run(["ffprobe","-v","error","-show_streams","-show_format","-of","json",str(path)]).stdout)
    video=next((s for s in data.get("streams",[]) if s.get("codec_type")=="video"),{})
    rate=video.get("avg_frame_rate","0/1").split("/")
    fps=float(rate[0])/max(float(rate[1]),1) if len(rate)==2 else 0
    return {"duration":float(data.get("format",{}).get("duration") or 0),"width":int(video.get("width") or 0),"height":int(video.get("height") or 0),"fps":round(fps,3),"codec":video.get("codec_name","unknown")}

def analyze_music_rhythm(path:Path,duration=None):
    """Locate a few strong musical onsets for short-form flash-cut bursts."""
    result=subprocess.run(["ffmpeg","-v","error","-i",str(path),"-ac","1","-ar","8000","-f","s16le","pipe:1"],capture_output=True)
    if result.returncode or len(result.stdout)<3200:return []
    samples=array("h");samples.frombytes(result.stdout);frame_size=400
    energy=[sum(abs(value) for value in samples[index:index+frame_size])/frame_size for index in range(0,len(samples)-frame_size+1,frame_size)]
    if len(energy)<12:return []
    onset=[]
    for index,value in enumerate(energy):
        history=energy[max(0,index-8):index];baseline=sum(history)/len(history) if history else value
        onset.append(max(0.0,value-baseline))
    positive=sorted(value for value in onset if value>0)
    if not positive:return []
    threshold=positive[min(len(positive)-1,int(len(positive)*0.78))]
    seconds=float(duration or len(samples)/8000.0);desired=2 if seconds<35 else (3 if seconds<75 else 4);candidates=[]
    for index in range(1,len(onset)-1):
        second=index*0.05
        if seconds*0.12<=second<=seconds*0.90 and onset[index]>=threshold and onset[index]>=onset[index-1] and onset[index]>=onset[index+1]:candidates.append((onset[index],second))
    chosen=[];minimum_gap=max(3.5,min(12.0,seconds/(desired+1)*0.7))
    for _,second in sorted(candidates,reverse=True):
        if all(abs(second-existing)>=minimum_gap for existing in chosen):
            chosen.append(second)
            if len(chosen)>=desired:break
    return [round(value,3) for value in sorted(chosen)]

def create_derivatives(source:Path,proxy:Path,audio:Path,thumb:Path):
    for p in (proxy.parent,audio.parent,thumb.parent): p.mkdir(parents=True,exist_ok=True)
    run(["ffmpeg","-y","-i",str(source),"-vf","scale='min(1280,iw)':-2","-c:v","libx264","-preset","veryfast","-crf","28","-c:a","aac","-b:a","96k",str(proxy)])
    run(["ffmpeg","-y","-i",str(source),"-vn","-ac","1","-ar","48000","-c:a","pcm_s16le",str(audio)])
    run(["ffmpeg","-y","-ss","0","-i",str(source),"-frames:v","1","-vf","scale=640:-2",str(thumb)])

def _srt_time(seconds):
    ms=max(0,int(float(seconds)*1000));h,ms=divmod(ms,3600000);m,ms=divmod(ms,60000);s,ms=divmod(ms,1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def _wrap_caption(value,limit):
    text=" ".join(str(value or "").replace("\n"," ").split())
    return "\n".join(text[index:index+limit] for index in range(0,len(text),limit))

def _override_key(output_name,timeline_index,asset_id,caption_index):
    return f"{output_name}|{timeline_index}|{asset_id}|{caption_index}"

def _short_transition_overlap(item):
    if str(item.get("transition") or "cut")=="cut":return 0.0
    return max(0.0,min(float(item.get("transition_duration") or 0),0.6))

def _write_bilingual_srt(used,target,fmt,caption_overrides=None,output_name="output"):
    caption_overrides=caption_overrides or {}
    cues=[];offset=0.0
    line_limit=18 if fmt=="short_9x16" else 28
    for position,(item,asset,length,timeline_index) in enumerate(used):
        if position:offset-=_short_transition_overlap(item) if fmt=="short_9x16" else 0.0
        clip_start=float(item.get('start',0));clip_end=clip_start+length
        if fmt=="short_9x16" and item.get("show_captions") is False:
            offset+=length
            continue
        analysis=json.loads(asset.get('analysis') or '{}')
        for caption_index,caption in enumerate(analysis.get('bilingual_captions',[]),1):
            start=float(caption.get('start',caption.get('start_time',0)));end=float(caption.get('end',caption.get('end_time',0)))
            if end<=clip_start or start>=clip_end:continue
            override=caption_overrides.get(_override_key(output_name,timeline_index,int(asset['id']),caption_index),{})
            if override.get("omit"):continue
            zh=override.get('zh',caption.get('zh',''));ja=override.get('ja',caption.get('ja',''))
            if is_standalone_filler(zh,ja):continue
            out_start=offset+max(start,clip_start)-clip_start;out_end=offset+min(end,clip_end)-clip_start
            if override.get("output_start") is not None:out_start=float(override["output_start"])
            if override.get("output_end") is not None:out_end=float(override["output_end"])
            identity=(int(asset["id"]),caption_index,str(zh),str(ja))
            if cues and cues[-1]["identity"]==identity and out_start<=cues[-1]["end"]+0.08 and out_start>=cues[-1]["start"]-0.08:
                cues[-1]["end"]=max(cues[-1]["end"],out_end)
            else:cues.append({"start":out_start,"end":out_end,"zh":zh,"ja":ja,"identity":identity})
        offset+=length
    lines=[]
    for idx,cue in enumerate(cues,1):
        zh=_wrap_caption(cue["zh"],line_limit);ja=_wrap_caption(cue["ja"],line_limit);text='\n'.join(value for value in (zh,ja) if value)
        if text:lines.extend([str(idx),f"{_srt_time(cue['start'])} --> {_srt_time(cue['end'])}",text,''])
    if lines:target.write_text('\n'.join(lines),encoding='utf-8-sig')
    return bool(lines)

def write_bilingual_srt(assets,timeline,target,fmt,caption_overrides=None,output_name="output"):
    by_id={int(asset["id"]):asset for asset in assets};used=[]
    for timeline_index,item in enumerate(timeline or [],1):
        asset=by_id.get(int(item.get("asset_id",-1)))
        if not asset:continue
        try:start=max(0.0,float(item.get("start",0)));end=min(float(asset.get("duration") or 0),float(item.get("end",asset.get("duration") or 0)))
        except (TypeError,ValueError):continue
        if end-start>=0.25:used.append((item,asset,end-start,timeline_index))
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True);target.unlink(missing_ok=True)
    return _write_bilingual_srt(used,target,fmt,caption_overrides,output_name)

def _subtitle_style(fmt):
    return "FontName=Noto Sans CJK SC,FontSize=24,Bold=1,Outline=3,Shadow=1,BorderStyle=1,Alignment=2,MarginL=96,MarginR=96,MarginV=300" if fmt=="short_9x16" else "FontName=Noto Sans CJK SC,FontSize=20,Outline=2,BorderStyle=1,Alignment=2,MarginL=60,MarginR=60,MarginV=50"

def burn_subtitles(source,target,srt,fmt):
    source=Path(source);target=Path(target);srt=Path(srt);target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(target.stem+".subtitle.tmp"+target.suffix);temporary.unlink(missing_ok=True)
    common=["ffmpeg","-y","-i",str(source)]
    if srt.exists() and srt.stat().st_size:
        common+=["-vf",f"subtitles=filename='{srt.as_posix()}':force_style='{_subtitle_style(fmt)}'"]
    nvenc=common+["-c:v","h264_nvenc","-preset","p4","-cq","23","-b:v","0","-c:a","copy","-movflags","+faststart",str(temporary)]
    cpu=common+["-c:v","libx264","-preset","fast","-crf","23","-c:a","copy","-movflags","+faststart",str(temporary)]
    try:run(nvenc)
    except subprocess.CalledProcessError:run(cpu)
    temporary.replace(target)
    return target

def _video_filter(fmt,item):
    if fmt=="long_16x9":return "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
    base="scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    effect=str(item.get("effect") or "clean")
    if effect=="punch_in":return base+",scale=1166:2074,crop=1080:1920:(iw-ow)/2:(ih-oh)/2,eq=contrast=1.05:saturation=1.10"
    if effect=="impact_zoom":return base+",scale=1220:2169,crop=1080:1920:(iw-ow)/2:(ih-oh)/2,eq=contrast=1.08:saturation=1.12"
    if effect=="subtle_zoom":return base+",zoompan=z='min(zoom+0.0005,1.055)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps=30,eq=saturation=1.06"
    if effect=="warm":return base+",eq=contrast=1.04:saturation=1.12:gamma_r=1.025:gamma_b=0.985"
    if effect=="cool":return base+",eq=contrast=1.07:saturation=1.08:gamma_r=0.985:gamma_b=1.025"
    if effect=="soft_contrast":return base+",eq=contrast=1.09:brightness=-0.01:saturation=1.03"
    if effect=="flash_frame":return base+",fade=t=in:st=0:d=0.06:color=white,eq=contrast=1.08:saturation=1.10"
    return base+",eq=contrast=1.025:saturation=1.06"

def _music_mix_filter(fmt,target_seconds=None):
    if fmt=="short_9x16":
        if target_seconds is None:target_seconds=600.0
        target=max(0.25,float(target_seconds))
        return f"[0:a]apad=whole_dur={target:.3f},asplit=2[base_mix][base_side];[1:a]atrim=duration={target:.3f},volume=0.26[bg];[bg][base_side]sidechaincompress=threshold=0.015:ratio=10:attack=15:release=280[ducked];[base_mix][ducked]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,alimiter=limit=0.95[a]"
    return "[1:a]volume=0.08[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2[a]"

def _segment_audio_filter(item):
    audio_mode=str(item.get("audio_mode") or "montage")
    dialogue_filter="highpass=f=80,afftdn=nf=-25,loudnorm=I=-16:TP=-1.5:LRA=11,volume=0.90"
    if item.get("background_cleanup"):
        dialogue_filter="highpass=f=95,lowpass=f=10500,afftdn=nf=-32,loudnorm=I=-16:TP=-1.5:LRA=9,volume=0.96"
    return {
        "dialogue":dialogue_filter,
        "ambient":"highpass=f=45,loudnorm=I=-21:TP=-2:LRA=14,volume=0.55",
        "montage":"highpass=f=80,afftdn=nf=-25,loudnorm=I=-23:TP=-2:LRA=11,volume=0.18",
        "mute":"volume=0",
    }.get(audio_mode,"highpass=f=80,afftdn=nf=-25,loudnorm=I=-23:TP=-2:LRA=11,volume=0.18")

def _concat_segments(segments,merged,temp,expected_duration=None):
    concat=temp/"concat.txt";concat.write_text("\n".join(f"file '{p.as_posix()}'" for p in segments),encoding="utf-8")
    command=["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat),"-c","copy"]
    if expected_duration is not None:command.extend(["-t",f"{max(0.0,float(expected_duration)):.3f}"])
    run([*command,str(merged)])

def _merge_short_segments(segments,used,merged,temp):
    if len(segments)<2:return _concat_segments(segments,merged,temp)
    inputs=["ffmpeg","-y"]
    for segment in segments:inputs.extend(["-i",str(segment)])
    graph=[]
    for index in range(len(segments)):
        graph.append(f"[{index}:v]settb=AVTB,setpts=PTS-STARTPTS[sv{index}]")
        graph.append(f"[{index}:a]asetpts=PTS-STARTPTS[sa{index}]")
    video="[sv0]";audio="[sa0]";duration=float(used[0][2])
    allowed={"fade","fadeblack","fadewhite","smoothleft","smoothup","wipeleft","wiperight","slideup"}
    for index in range(1,len(segments)):
        item=used[index][0];transition=str(item.get("transition") or "cut");transition=transition if transition=="cut" or transition in allowed else "fade"
        next_video=f"[v{index}]";next_audio=f"[a{index}]"
        if transition=="cut":
            graph.append(f"{video}[sv{index}]concat=n=2:v=1:a=0{next_video}")
            graph.append(f"{audio}[sa{index}]concat=n=2:v=0:a=1{next_audio}")
            duration+=float(used[index][2])
        else:
            cross=max(0.12,min(float(item.get("transition_duration") or 0.24),0.6));offset=max(0.01,duration-cross)
            graph.append(f"{video}[sv{index}]xfade=transition={transition}:duration={cross:.3f}:offset={offset:.3f}{next_video}")
            graph.append(f"{audio}[sa{index}]acrossfade=d={cross:.3f}:c1=tri:c2=tri{next_audio}")
            duration=duration+float(used[index][2])-cross
        video=next_video;audio=next_audio
    common=inputs+["-filter_complex",";".join(graph),"-map",video,"-map",audio]
    nvenc=common+["-c:v","h264_nvenc","-preset","p4","-cq","23","-b:v","0","-c:a","aac","-b:a","192k",str(merged)]
    cpu=common+["-c:v","libx264","-preset","fast","-crf","23","-c:a","aac","-b:a","192k",str(merged)]
    try:run(nvenc)
    except subprocess.CalledProcessError:
        try:run(cpu)
        except subprocess.CalledProcessError as error:
            (temp/"short-merge.ffmpeg.log").write_text(error.stderr or str(error),encoding="utf-8")
            _concat_segments(segments,merged,temp)

def render_timeline(assets,timeline,fmt: str,target:Path,temp:Path,music_files=(),checkpoint=None,caption_overrides=None,output_name=None,burn_captions=True):
    temp.mkdir(parents=True,exist_ok=True);target.parent.mkdir(parents=True,exist_ok=True)
    by_id={int(a['id']):a for a in assets};segments=[];used=[]
    for idx,item in enumerate(timeline):
        asset=by_id.get(int(item.get('asset_id',-1)))
        if not asset:continue
        if checkpoint and not checkpoint(item,asset):raise InterruptedError("项目已暂停或停止")
        start=max(0,float(item.get('start',0)));end=min(float(asset.get('duration') or 0),float(item.get('end',asset.get('duration') or 0)))
        if end-start<0.25:continue
        segment=temp/f"{idx:04d}.mp4"
        common=["ffmpeg","-y","-ss",str(start),"-t",str(end-start),"-i",asset['path'],"-vf",_video_filter(fmt,item),"-af",_segment_audio_filter(item),"-r","30"]
        nvenc=common+["-c:v","h264_nvenc","-preset","p4","-cq","23","-b:v","0","-c:a","aac","-b:a","192k",str(segment)]
        cpu=common+["-c:v","libx264","-preset","fast","-crf","23","-c:a","aac","-b:a","192k",str(segment)]
        try:run(nvenc)
        except subprocess.CalledProcessError:
            run(cpu)
        segments.append(segment);used.append((item,asset,end-start,idx+1))
    if not segments:raise RuntimeError("时间线没有可渲染片段")
    merged=temp/"merged.mp4"
    if fmt=="short_9x16":_merge_short_segments(segments,used,merged,temp)
    else:_concat_segments(segments,merged,temp,sum(float(value[2]) for value in used))
    if checkpoint and not checkpoint():raise InterruptedError("项目已暂停或停止")
    srt=target.with_suffix('.zh-ja.srt');has_subtitles=_write_bilingual_srt(used,srt,fmt,caption_overrides,output_name or target.stem);music=next(iter(music_files),None)
    if (not burn_captions or not has_subtitles) and not music:shutil.move(str(merged),str(target))
    else:
        inputs=["ffmpeg","-y","-i",str(merged)]
        if music:
            mix_seconds=None
            if fmt=="short_9x16":
                mix_seconds=min(float(probe(merged).get("duration") or 0),float(probe(music).get("duration") or 0))
            inputs += ["-stream_loop","-1","-i",str(music),"-filter_complex",_music_mix_filter(fmt,mix_seconds),"-map","0:v","-map","[a]"]
        if burn_captions and has_subtitles:inputs += ["-vf",f"subtitles=filename='{srt.as_posix()}':force_style='{_subtitle_style(fmt)}'"]
        nvenc=inputs+["-c:v","h264_nvenc","-preset","p4","-cq","23","-b:v","0","-c:a","aac","-b:a","192k","-shortest",str(target)]
        cpu=inputs+["-c:v","libx264","-preset","fast","-crf","23","-c:a","aac","-b:a","192k","-shortest",str(target)]
        try:run(nvenc)
        except subprocess.CalledProcessError:run(cpu)
    shutil.rmtree(temp,ignore_errors=True)
    return target

def apply_text_overlays(source,overlays):
    source=Path(source);valid=[];text_files=[]
    for index,overlay in enumerate(overlays or []):
        if not isinstance(overlay,dict):continue
        try:start=float(overlay.get("start"));end=float(overlay.get("end"))
        except (TypeError,ValueError):continue
        overlay_text=str(overlay.get("text") or "").strip()
        if not overlay_text or end<=start:continue
        if overlay.get("vertical"):overlay_text="\n".join(overlay_text)
        text_file=source.with_name(f".{source.stem}.overlay-{index}.txt");text_file.write_text(overlay_text,encoding="utf-8");text_files.append(text_file)
        fade=max(0.05,min(float(overlay.get("fade_seconds") or 0.55),(end-start)/2));font_size=max(16,min(int(overlay.get("font_size") or 48),120));margin=max(20,min(int(overlay.get("right_margin") or 120),500));escaped=text_file.as_posix().replace("'","\\'")
        alpha=f"if(lt(t,{start+fade:.3f}),(t-{start:.3f})/{fade:.3f},if(lt(t,{end-fade:.3f}),1,({end:.3f}-t)/{fade:.3f}))"
        valid.append(f"drawtext=font='Noto Sans CJK JP':textfile='{escaped}':fontcolor=white:fontsize={font_size}:borderw=3:bordercolor=black@0.85:shadowx=2:shadowy=2:x=w-text_w-{margin}:y=(h-text_h)/2:alpha='{alpha}':enable='between(t,{start:.3f},{end:.3f})'")
    if not valid:return {"applied":0}
    temporary=source.with_name(source.stem+".overlay.tmp.mp4");temporary.unlink(missing_ok=True);common=["ffmpeg","-y","-i",str(source),"-vf",",".join(valid),"-map","0:v:0","-map","0:a?","-c:a","copy","-movflags","+faststart"]
    try:
        try:run([*common,"-c:v","h264_nvenc","-preset","p4","-cq","23","-b:v","0",str(temporary)])
        except subprocess.CalledProcessError:run([*common,"-c:v","libx264","-preset","fast","-crf","23",str(temporary)])
        if not temporary.is_file() or temporary.stat().st_size<=0:raise RuntimeError("动态文字叠加没有生成有效视频")
        temporary.replace(source)
        return {"applied":len(valid),"overlays":[{"text":str(value.get("text") or ""),"start":value.get("start"),"end":value.get("end"),"vertical":bool(value.get("vertical"))} for value in overlays if isinstance(value,dict)]}
    finally:
        temporary.unlink(missing_ok=True)
        for text_file in text_files:text_file.unlink(missing_ok=True)

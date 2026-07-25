import gc
import json
import os
import re
import subprocess
import threading
import time
import traceback
from contextlib import contextmanager,nullcontext
from pathlib import Path
from PIL import Image
import torch
from transformers import AutoModelForImageTextToText,AutoProcessor,BitsAndBytesConfig
from . import db
from .story_planner import assemble_story_plan,story_plan_errors,valid_story_plan

POLL=int(os.getenv("VL_POLL_SECONDS","20"))
MODEL_NAME=os.getenv("VL_MODEL","Qwen/Qwen3-VL-4B-Instruct")
PROMPT="""你是生活Vlog素材分析员。只根据画面返回JSON，不要markdown。字段：summary场景摘要，quality从0到100，problems数组（模糊、抖动、曝光、遮挡等），subjects数组，actions数组，mood，story_value从0到100，suggested_use（主线/B-roll/过场/弃用），duplicate_hint。不要猜测看不到的信息。"""
DUAL_STYLE="""固定双风格规范：长篇供YouTube与Bilibili共用，是10–60分钟、默认20–40分钟的日系生活纪录；节奏舒缓、有留白，重视移动过程、街景、饮食细节、真实对话和情绪余韵。从转写与画面中找出一个真实的“故事锚点”（优先文化差异、朋友互动、意外发现或有记忆点的观点），可用不同但相邻的真实片段在开头预告、中段展开、结尾回扣，不能重复同一时间区间。短篇供抖音与小红书共用，必须和长篇形成明显反差：前1–3秒直接给真实问题、反差、反应或结论作为钩子，关键词前置，剪辑更紧凑，交替使用对话、手部或物件特写、朋友反应和场景切换，并让结尾形成回答、反转或自然回环；禁止虚构冲突、标题党和与素材无关的梗。优先把文化差异和朋友交流剪成具有讨论度、可评论的网感短篇。"""
SHORT_AUDIO_STYLE="""短篇由指定BGM主导，镜头按音乐节奏密集编排，并在BGM能量点允许2–4组连续快闪。AI必须为每个短篇选择voice_mode，只能是bgm_only或selective_dialogue，并给出voice_reason：画面与音乐能说清楚时选bgm_only；只有真实对话是故事成立所必需时才选selective_dialogue，且只保留一至三句点题人声。字幕只跟随这些被选中的人声，不能全程铺字幕。"""

def load_model():
    quant=BitsAndBytesConfig(load_in_4bit=True,bnb_4bit_quant_type="nf4",bnb_4bit_compute_dtype=torch.bfloat16,bnb_4bit_use_double_quant=True)
    model=AutoModelForImageTextToText.from_pretrained(MODEL_NAME,device_map="auto",quantization_config=quant,dtype=torch.bfloat16)
    return model,AutoProcessor.from_pretrained(MODEL_NAME)

def valid_image(path):
    path=Path(path)
    if not path.exists() or path.stat().st_size<=0:return False
    try:
        with Image.open(path) as image:image.verify()
        return True
    except Exception:return False

def extract_frame(source,target,position):
    source=Path(source);target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    attempts=[]
    for seek in (max(0,float(position)),0.0):
        target.unlink(missing_ok=True)
        result=subprocess.run(["ffmpeg","-y","-ss",str(seek),"-i",str(source),"-frames:v","1","-vf","scale=640:-2",str(target)],capture_output=True,text=True)
        if result.returncode==0 and valid_image(target):return str(target)
        attempts.append((result.stderr or result.stdout)[-800:])
    raise RuntimeError(f"无法从视频重建有效缩略图：{source}\n"+"\n".join(attempts))

def ensure_thumbnail(asset):
    target=Path(asset['thumbnail_path'])
    if valid_image(target):return str(target)
    proxy=Path(asset.get('proxy_path') or '')
    source=proxy if proxy.is_file() else Path(asset['path'])
    duration=float(asset.get('duration') or 0)
    return extract_frame(source,target,min(1.0,duration/2) if duration>0 else 0)

def visual_samples(asset):
    paths=[ensure_thumbnail(asset)]
    duration=float(asset.get('duration') or 0)
    if duration<30:return paths
    sample_dir=Path(asset['thumbnail_path']).parent/"samples";sample_dir.mkdir(parents=True,exist_ok=True)
    for index,ratio in enumerate((0.25,0.5,0.75),1):
        target=sample_dir/f"asset-{asset['id']}-{index}.jpg"
        if not valid_image(target):extract_frame(asset['proxy_path'],target,duration*ratio)
        paths.append(str(target))
    return paths

def analyze(model,processor,image_paths):
    content=[{"type":"image","image":path} for path in image_paths]
    content.append({"type":"text","text":PROMPT+" 多张图片来自同一段素材的不同时间点，请综合判断场景变化。"})
    messages=[{"role":"user","content":content}]
    inputs=processor.apply_chat_template(messages,tokenize=True,add_generation_prompt=True,return_dict=True,return_tensors="pt").to(model.device)
    generated=model.generate(**inputs,max_new_tokens=512,do_sample=False)
    trimmed=[out[len(inp):] for inp,out in zip(inputs.input_ids,generated)]
    text=processor.batch_decode(trimmed,skip_special_tokens=True,clean_up_tokenization_spaces=False)[0].strip()
    try:return json.loads(text)
    except json.JSONDecodeError:return {"raw":text,"parse_warning":True}

def text_json(model,processor,prompt,max_tokens=4096):
    messages=[{"role":"user","content":[{"type":"text","text":prompt}]}]
    inputs=processor.apply_chat_template(messages,tokenize=True,add_generation_prompt=True,return_dict=True,return_tensors="pt").to(model.device)
    generated=model.generate(**inputs,max_new_tokens=max_tokens,do_sample=False)
    trimmed=[out[len(inp):] for inp,out in zip(inputs.input_ids,generated)]
    text=processor.batch_decode(trimmed,skip_special_tokens=True,clean_up_tokenization_spaces=False)[0].strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    if not text.startswith("{") and "{" in text and "}" in text:text=text[text.find("{"):text.rfind("}")+1]
    try:return json.loads(text)
    except json.JSONDecodeError:return {"raw":text,"parse_warning":True}

@contextmanager
def generation_heartbeat(project_id,resume_status):
    stop=threading.Event();started=time.monotonic()
    def report_loop():
        while not stop.wait(600):
            minutes=max(1,int((time.monotonic()-started)/60))
            item=f"AI创意构思 · 已运行 {minutes} 分钟 · 随后确定性构建时间线"
            db.set_progress(project_id,resume_status,"剪辑方案生成",item)
            db.log_event(project_id,"info","剪辑方案生成","generation_heartbeat",item)
    thread=threading.Thread(target=report_loop,name=f"story-intent-heartbeat-{project_id}",daemon=True);thread.start()
    try:yield
    finally:stop.set();thread.join(timeout=1)

def generate_valid_plan(model,processor,prompt,assets,project_id=None,resume_status="revision_requested"):
    if project_id:
        db.set_progress(project_id,resume_status,"剪辑方案生成","AI创意构思 · 时间线将按真实素材自动展开")
        db.log_event(project_id,"info","剪辑方案生成","output_target","本轮目标：1 部10–60分钟的动态长篇 + 1 部短篇草案；长篇时长由素材故事价值决定，短篇导出时可再按指定BGM时长重排")
        db.log_event(project_id,"info","剪辑方案生成","generation_started","开始生成紧凑编辑意图，不再要求AI输出数百个时间线片段")
    with generation_heartbeat(project_id,resume_status) if project_id else nullcontext():
        intent=text_json(model,processor,prompt[:48000],1280)
    fallback=not isinstance(intent,dict) or bool(intent.get("parse_warning"))
    if fallback:
        if project_id:db.log_event(project_id,"warning","剪辑方案生成","intent_fallback","AI创意结构未能解析，已自动切换确定性故事结构；不会再次盲目重试",{"raw_preview":str(intent.get("raw") or "")[:500] if isinstance(intent,dict) else ""})
        intent={}
    plan=assemble_story_plan(intent,assets,prompt,used_fallback=fallback);errors=story_plan_errors(plan,assets)
    if errors:raise RuntimeError("确定性时间线校验失败："+"；".join(errors[:12]))
    if project_id:
        long_seconds=sum(float(item["end"])-float(item["start"]) for item in plan["long"]["timeline"]);short_count=len(plan["shorts"]);total_files=1+short_count
        db.set_progress(project_id,resume_status,"剪辑方案生成",f"时间线已校验 · 长篇 {long_seconds/60:.1f} 分钟 · {short_count} 条短篇")
        db.log_event(project_id,"success","剪辑方案生成","generation_validated",f"确定性时间线通过校验：1 部 {long_seconds/60:.1f} 分钟长篇 + {short_count} 部短篇，共 {total_files} 个成片文件",{"long_seconds":round(long_seconds,1),"long_clips":len(plan["long"]["timeline"]),"short_files":short_count,"total_files":total_files,"fallback":fallback})
    return plan

def create_story_plan(model,processor,project):
    assets=db.rows("SELECT * FROM assets WHERE project_id=? ORDER BY id",(project['id'],));material=[]
    for asset in assets:
        analysis=json.loads(asset.get("analysis") or "{}")
        material.append({"asset_id":asset['id'],"filename":asset['filename'],"duration":asset['duration'],"transcript":analysis.get('transcript','')[:3000],"visual":analysis.get('visual',{})})
    prompt="""你是生活Vlog总剪辑师。只生成紧凑的编辑意图JSON，不要输出timeline、start或end；程序会根据真实素材时长展开时间线。制作备注是高优先级要求。"""+DUAL_STYLE+SHORT_AUDIO_STYLE+"""根据素材故事密度建议long_target_minutes（10到60，可用小数，不要机械选择30），并选择章节、故事锚点、素材优先级和1个短篇主题。短篇必须是抖音/小红书网感叙事，组合多个真实素材、前3秒强钩子、对话/反应/细节交替，不能只摘一段精彩。只能引用真实asset_id。结构：{title,summary,long_target_minutes:数字,story_anchor:{topic,setup,payoff,asset_ids:[整数]},chapters:[{name}],asset_priorities:[{asset_id,priority:0到100,chapter}],shorts:[{title,hook,cover_text,core_payoff,pacing,voice_mode,voice_reason,asset_ids:[整数]}],music_mood:[字符串],review_warnings:[字符串]}。整个JSON保持精简，asset_priorities最多12项。制作备注："""+str(project.get("notes") or "无")+"\n素材："+json.dumps(material,ensure_ascii=False)
    return generate_valid_plan(model,processor,prompt,assets,project['id'],"ready_for_visual")

def compact_existing_plan(plan):
    if not isinstance(plan,dict):return {}
    long=plan.get("long") if isinstance(plan.get("long"),dict) else {};shorts=[]
    for value in plan.get("shorts") or []:
        if isinstance(value,dict):shorts.append({key:value.get(key) for key in ("title","hook","cover_text","core_payoff","pacing","voice_mode","voice_reason")})
    return {"title":plan.get("title"),"summary":plan.get("summary"),"story_anchor":long.get("story_anchor"),"chapters":long.get("chapters"),"shorts":shorts,"music_mood":plan.get("music_mood")}

def group_time_stamps(times):
    normalized=[]
    for value in times:
        if not isinstance(value,dict):continue
        start=value.get('start_time',value.get('start'));end=value.get('end_time',value.get('end'));text=str(value.get('text','')).strip()
        if start is None or end is None or not text:continue
        normalized.append({"start":float(start),"end":float(end),"text":text})
    groups=[];current=None
    for value in normalized:
        if current:
            gap=max(0,value['start']-current['end']);duration=value['end']-current['start'];length=len(current['text'])
            boundary=gap>1.0 or duration>6.0 or length>=28 or bool(re.search(r"[。！？!?…]$",current['text']))
        else:boundary=True
        if boundary:
            current=dict(value);groups.append(current);continue
        separator=" " if current['text'][-1:].isascii() and value['text'][:1].isascii() else ""
        current['text']+=separator+value['text'];current['end']=max(current['end'],value['end'])
    return groups

def create_bilingual_captions(model,processor,analysis,duration):
    times=analysis.get('time_stamps') or []
    if not times and analysis.get('transcript'):times=[{"start":0,"end":duration,"text":analysis['transcript']}]
    grouped=group_time_stamps(times)
    if not grouped:return []
    captions=[]
    for offset in range(0,len(grouped),20):
        chunk=grouped[offset:offset+20]
        indexed=[{"index":index,"source":value["text"]} for index,value in enumerate(chunk)]
        translate_prompt="""你是专业中日双语字幕译者。只返回JSON：{captions:[{index:整数,zh:简体中文,ja:自然日语}]}。逐句忠实翻译，不增添原文没有的信息；修正常见口语识别断句，但不臆造听不清的内容；统一人名、地名、店名；语气自然、简短，尽量每种语言不超过24个中日韩字符。每个index必须且只能返回一次。原识别语言："""+str(analysis.get('language') or 'unknown')+"\n输入："+json.dumps(indexed,ensure_ascii=False)
        translated=text_json(model,processor,translate_prompt,3072)
        draft_rows=translated.get('captions',[]) if isinstance(translated,dict) else []
        draft_by_index={}
        for position,row in enumerate(draft_rows):
            if not isinstance(row,dict):continue
            try:index=int(row.get('index',position))
            except (TypeError,ValueError):index=position
            if 0<=index<len(chunk):draft_by_index[index]={"zh":str(row.get('zh','')).strip(),"ja":str(row.get('ja','')).strip()}
        draft=[{"index":index,"source":value["text"],"zh":draft_by_index.get(index,{}).get("zh",""),"ja":draft_by_index.get(index,{}).get("ja","")} for index,value in enumerate(chunk)]
        proof_prompt="""你是中日双语字幕终审。对照source精校draft，只返回JSON：{captions:[{index:整数,zh:简体中文,ja:自然日语,confidence:0到1,needs_review:布尔值}]}。要求：语义忠实、口语自然、两种语言信息一致；纠正常见中日语同音误识别和不自然直译；统一人名、地名、店名；不增加原文没有的信息；每种语言尽量不超过24个中日韩字符。原文含混、专名无法确认或译文可能不可靠时needs_review=true并降低confidence，禁止猜测。每个index必须且只能返回一次。画面参考："""+json.dumps(analysis.get('visual',{}),ensure_ascii=False)+"\n待校对："+json.dumps(draft,ensure_ascii=False)
        proofed=text_json(model,processor,proof_prompt,4096)
        proof_rows=proofed.get('captions',[]) if isinstance(proofed,dict) else []
        proof_by_index={}
        for position,row in enumerate(proof_rows):
            if not isinstance(row,dict):continue
            try:index=int(row.get('index',position))
            except (TypeError,ValueError):index=position
            if 0<=index<len(chunk):proof_by_index[index]=row
        for index,source in enumerate(chunk):
            row=proof_by_index.get(index) or draft_by_index.get(index) or {}
            zh=str(row.get('zh','')).strip();ja=str(row.get('ja','')).strip()
            if not zh and not ja:continue
            try:confidence=max(0.0,min(1.0,float(row.get('confidence',0.75))))
            except (TypeError,ValueError):confidence=0.5
            captions.append({"start":source["start"],"end":source["end"],"source":source["text"],"zh":zh,"ja":ja,"confidence":confidence,"needs_review":bool(row.get('needs_review',confidence<0.72))})
    return captions

def queue_default_exports(project_id,force_new=False,formats=None):
    numbers=[int(value["version"][1:]) for value in db.rows("SELECT version FROM exports WHERE project_id=?",(project_id,)) if value["version"].startswith("v") and value["version"][1:].isdigit()]
    existing=max(numbers,default=0)
    added=0
    project=db.row("SELECT settings FROM projects WHERE id=?",(project_id,)) or {};settings=json.loads(project.get("settings") or "{}");plan=settings.get("story_plan") or {}
    requested=tuple(value for value in (formats or ("long_16x9","short_9x16")) if value in {"long_16x9","short_9x16"})
    if not requested:raise RuntimeError("没有有效的成片输出格式")
    for fmt in requested:
        valid=None if force_new else db.row("SELECT id FROM exports WHERE project_id=? AND format=? AND (status IN ('render_requested','rendering','approved') OR (status='review_ready' AND path IS NOT NULL AND path!='[]')) LIMIT 1",(project_id,fmt))
        if valid:continue
        added+=1
        snapshot={"long":plan.get("long",{}).get("timeline",[])} if fmt=="long_16x9" else {f"short-{index+1}":value.get("timeline",[]) for index,value in enumerate(plan.get("shorts") or []) if isinstance(value,dict)}
        db.execute("INSERT INTO exports(project_id,version,format,status,timeline_snapshot,created_at) VALUES(?,?,?,?,?,?)",(project_id,f"v{existing+added}",fmt,"render_requested",json.dumps(snapshot,ensure_ascii=False),db.now()))
    pending=db.row("SELECT id FROM exports WHERE project_id=? AND status IN ('render_requested','rendering') LIMIT 1",(project_id,))
    next_status="render_requested" if pending else "review_ready"
    db.execute("UPDATE projects SET status=?,updated_at=? WHERE id=?",(next_status,db.now(),project_id))
    db.set_progress(project_id,"render_requested" if pending else None,"成片渲染" if pending else "人工审核")
    db.log_event(project_id,"info","成片渲染","exports_queued",f"已加入 {added} 个导出任务："+"、".join(requested),{"formats":requested,"force_new":force_new})

def process(project):
    stage="画面分析与双语字幕"
    if not db.begin_stage(project['id'],"visual_analyzing","ready_for_visual",stage):return
    model,processor=load_model()
    try:
        for asset in db.rows("SELECT * FROM assets WHERE project_id=? ORDER BY id",(project['id'],)):
            prior=json.loads(asset.get("analysis") or "{}")
            if not db.checkpoint(project['id'],"ready_for_visual",stage,asset['filename']):return
            if "visual_model" not in prior:
                prior["visual"]=analyze(model,processor,visual_samples(asset));prior["visual_model"]=MODEL_NAME
            if int(prior.get("caption_version") or 0)<3:
                prior["bilingual_captions"]=create_bilingual_captions(model,processor,prior,asset.get('duration') or 0);prior["caption_version"]=3
            db.execute("UPDATE assets SET analysis=? WHERE id=?",(json.dumps(prior,ensure_ascii=False),asset['id']))
            db.log_event(project['id'],"info",stage,"asset_completed",f"画面与字幕已处理：{asset['filename']}",{"asset_id":asset['id']})
            if not db.checkpoint(project['id'],"ready_for_visual",stage,asset['filename']):return
        fresh=db.row("SELECT settings FROM projects WHERE id=?",(project['id'],));settings=json.loads((fresh or {}).get('settings') or '{}')
        assets=db.rows("SELECT * FROM assets WHERE project_id=? ORDER BY id",(project['id'],))
        if not valid_story_plan(settings.get('story_plan'),assets):
            db.set_progress(project['id'],"ready_for_visual","剪辑方案生成","整套素材")
            settings['story_plan']=create_story_plan(model,processor,project);settings['story_model']=MODEL_NAME
            db.execute("UPDATE projects SET settings=?,updated_at=? WHERE id=?",(json.dumps(settings,ensure_ascii=False),db.now(),project['id']))
            db.log_event(project['id'],"success","剪辑方案生成","story_plan_created","长短篇剪辑方案已生成")
        if not db.checkpoint(project['id'],"ready_for_visual","剪辑方案生成","整套素材"):return
        db.finish_stage(project['id'],"draft_ready",stage,"画面、双语字幕和剪辑方案全部完成")
        db.create_control(project['id'],"stopped",None,"等待选择版本","请选择生成一个长篇或短篇版本")
        db.log_event(project['id'],"success","剪辑方案生成","awaiting_version_selection","剪辑方案已就绪；未自动加入成片队列，等待在审核页选择长篇或短篇")
    finally:
        del model,processor;gc.collect();torch.cuda.empty_cache()

def _plan_snapshot(plan,fmt):
    if fmt=="long_16x9":snapshot={"long":(plan.get("long") or {}).get("timeline",[])}
    else:
        shorts=plan.get("shorts") or []
        snapshot={"short-1":shorts[0].get("timeline",[])} if shorts and isinstance(shorts[0],dict) else {}
    if not snapshot or not all(isinstance(timeline,list) and timeline for timeline in snapshot.values()):raise RuntimeError("重规划后的时间线为空，未创建新版本")
    return snapshot

def _next_version(project_id):
    versions=[int(value["version"][1:]) for value in db.rows("SELECT version FROM exports WHERE project_id=?",(project_id,)) if str(value["version"]).startswith("v") and str(value["version"])[1:].isdigit()]
    return f"v{max(versions,default=0)+1}"

def _finalize_version_bound_replan(project,settings,source,revisions):
    snapshot=_plan_snapshot(settings.get("story_plan") or {},source["format"]);version=_next_version(project["id"]);revision_ids=[int(value["id"]) for value in revisions]
    options={"plan_rebuild":{"source_export_id":source["id"],"source_version":source["version"],"revision_ids":revision_ids}}
    export_id=db.execute(
        "INSERT INTO exports(project_id,version,format,status,timeline_snapshot,render_options,source_export_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (project["id"],version,source["format"],"render_requested",json.dumps(snapshot,ensure_ascii=False),json.dumps(options,ensure_ascii=False),source["id"],db.now()),
    )
    placeholders=",".join("?" for _ in revision_ids);stamp=db.now()
    db.execute(f"UPDATE revisions SET status='applied',resolved_at=?,applied_export_id=?,applied_version=? WHERE id IN ({placeholders})",(stamp,export_id,version,*revision_ids))
    settings.pop("replan_request",None)
    db.execute("UPDATE exports SET status='superseded' WHERE id=?",(source["id"],))
    db.execute("UPDATE projects SET status='render_requested',settings=?,updated_at=? WHERE id=?",(json.dumps(settings,ensure_ascii=False),stamp,project["id"]))
    label="长篇" if source["format"]=="long_16x9" else "短篇"
    db.create_control(project["id"],"stopped","render_requested","等待确认渲染",f"{source['version']} 已按版本意见重规划为 {version} · {label}",render_scope=source["format"])
    db.log_event(project["id"],"success","修改剪辑方案","version_replan_completed",f"已读取 {source['version']} 的 {len(revision_ids)} 条意见并创建 {version}；请确认方案后启动渲染",{"source_export_id":source["id"],"source_version":source["version"],"new_export_id":export_id,"new_version":version,"revision_ids":revision_ids,"format":source["format"]})
    return {"export_id":export_id,"version":version,"revision_ids":revision_ids}

def revise(project):
    stage="修改剪辑方案"
    if not db.begin_stage(project['id'],"revision_planning","revision_requested",stage):return
    model,processor=load_model()
    try:
        settings=json.loads(project.get('settings') or '{}');request=settings.get("replan_request") if isinstance(settings.get("replan_request"),dict) else None;source=None
        if request:
            try:source_id=int(request.get("source_export_id"))
            except (TypeError,ValueError):raise RuntimeError("版本重规划请求缺少来源版本")
            source=db.row("SELECT id,version,format,status FROM exports WHERE id=? AND project_id=?",(source_id,project['id']))
            if not source or source["status"]!="replan_requested":raise RuntimeError("关联版本不再处于等待重规划状态")
            revision_ids=[int(value) for value in request.get("revision_ids") or [] if str(value).isdigit()]
            if not revision_ids:raise RuntimeError("版本重规划没有绑定意见")
            placeholders=",".join("?" for _ in revision_ids)
            revisions=db.rows(f"SELECT id,kind,body,source_version FROM revisions WHERE project_id=? AND status='open' AND source_export_id=? AND id IN ({placeholders}) ORDER BY id",(project['id'],source_id,*revision_ids))
            if len(revisions)!=len(revision_ids):raise RuntimeError("有版本意见已被更改，请重新发起重规划")
        else:
            revisions=db.rows("SELECT id,kind,body,source_version FROM revisions WHERE project_id=? AND status='open' AND source_export_id IS NULL ORDER BY id",(project['id'],))
            if not revisions:raise RuntimeError("没有待处理的项目级重规划意见")
        assets=db.rows("SELECT * FROM assets WHERE project_id=? ORDER BY id",(project['id'],));catalog=[]
        for asset in assets:
            analysis=json.loads(asset.get("analysis") or "{}")
            if int(analysis.get("caption_version") or 0)<3:
                if not db.checkpoint(project['id'],"revision_requested","字幕二次精校",asset['filename']):return
                analysis["bilingual_captions"]=create_bilingual_captions(model,processor,analysis,asset.get('duration') or 0);analysis["caption_version"]=3
                db.execute("UPDATE assets SET analysis=? WHERE id=?",(json.dumps(analysis,ensure_ascii=False),asset['id']))
                db.log_event(project['id'],"info","字幕二次精校","caption_proofread",f"中日字幕已二次精校：{asset['filename']}",{"asset_id":asset['id']})
            catalog.append({"asset_id":asset["id"],"filename":asset["filename"],"duration":asset["duration"],"transcript":analysis.get("transcript","")[:1200],"visual":analysis.get("visual",{})})
        scope=f"本轮仅应用来源版本 {source['version']} 的绑定意见。" if source else "本轮仅应用未关联版本的项目级意见。"
        prompt="""你是生活Vlog总剪辑师。只根据人工意见生成紧凑的编辑意图JSON，不要输出timeline、start或end；程序会按真实素材时长构建并校验时间线。制作备注和人工意见优先。"""+DUAL_STYLE+SHORT_AUDIO_STYLE+"""根据素材故事密度建议long_target_minutes（10到60，可用小数，不要机械选择30），并提出1个由多个真实素材组合的抖音/小红书网感短篇主题，不能只摘一段精彩。只能引用真实asset_id。结构：{title,summary,long_target_minutes:数字,story_anchor:{topic,setup,payoff,asset_ids:[整数]},chapters:[{name}],asset_priorities:[{asset_id,priority:0到100,chapter}],shorts:[{title,hook,cover_text,core_payoff,pacing,voice_mode,voice_reason,asset_ids:[整数]}],music_mood:[字符串],review_warnings:[字符串]}。整个JSON保持精简，asset_priorities最多12项。"""+scope+"制作备注："+str(project.get("notes") or "无")+"\n人工意见："+json.dumps(revisions,ensure_ascii=False)+"\n素材："+json.dumps(catalog,ensure_ascii=False)+"\n现有计划概述："+json.dumps(compact_existing_plan(settings.get('story_plan',{})),ensure_ascii=False)
        settings['story_plan']=generate_valid_plan(model,processor,prompt,assets,project['id'],"revision_requested");settings['story_model']=MODEL_NAME
        if not db.checkpoint(project['id'],"revision_requested",stage,"整套方案"):return
        if source:
            result=_finalize_version_bound_replan(project,settings,source,revisions)
            db.log_event(project['id'],"success",stage,"revision_completed",f"{source['version']} 的版本意见已应用到 {result['version']}")
        else:
            stamp=db.now();db.execute("UPDATE projects SET status='draft_ready',settings=?,updated_at=? WHERE id=?",(json.dumps(settings,ensure_ascii=False),stamp,project['id']))
            db.execute("UPDATE revisions SET status='resolved',resolved_at=? WHERE project_id=? AND status='open' AND source_export_id IS NULL",(stamp,project['id']))
            db.log_event(project['id'],"success",stage,"revision_completed","项目级剪辑方案已按意见修改")
            db.create_control(project['id'],"stopped",None,"等待选择版本","修改方案已保存，请选择生成一个长篇或短篇版本")
            db.log_event(project['id'],"success",stage,"awaiting_version_selection","修改方案已保存；未自动加入成片队列，等待选择一个指定格式")
    finally:
        del model,processor;gc.collect();torch.cuda.empty_cache()

def main():
    db.init_db()
    db.recover_interrupted_projects("visual")
    db.start_worker_heartbeat("visual","画面分析与双语字幕")
    while True:
        project=db.row("SELECT p.* FROM projects p JOIN project_control c ON c.project_id=p.id WHERE c.desired_state='running' AND p.status IN ('ready_for_visual','revision_requested') ORDER BY CASE p.status WHEN 'revision_requested' THEN 0 ELSE 1 END,p.id LIMIT 1")
        if not project:time.sleep(POLL);continue
        try:revise(project) if project['status']=='revision_requested' else process(project)
        except Exception as exc:
            resume="revision_requested" if project['status']=='revision_requested' else "ready_for_visual"
            db.fail_stage(project['id'],"visual_failed",resume,db.stage_for(resume),str(exc),traceback.format_exc());gc.collect();torch.cuda.empty_cache();time.sleep(POLL)
if __name__=="__main__":main()

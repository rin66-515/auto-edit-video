import gc
import json
import os
import time
import traceback
from pathlib import Path
import torch
from qwen_asr import Qwen3ASRModel
from . import db

POLL=int(os.getenv("ASR_POLL_SECONDS","20"))
ASR_MODEL=os.getenv("ASR_MODEL","Qwen/Qwen3-ASR-1.7B")
ALIGNER_MODEL=os.getenv("ALIGNER_MODEL","Qwen/Qwen3-ForcedAligner-0.6B")

def serial_time(item):
    if isinstance(item,(str,int,float,bool,type(None))):return item
    if isinstance(item,dict):return item
    data={}
    for key in ("text","start_time","end_time","start","end"):
        if hasattr(item,key):data[key]=getattr(item,key)
    return data or str(item)

def srt_time(seconds):
    ms=max(0,int(float(seconds)*1000));h,ms=divmod(ms,3600000);m,ms=divmod(ms,60000);s,ms=divmod(ms,1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def write_srt(asset_id,times):
    target=Path("/vlog/_automation/projects")/"subtitles"/f"asset-{asset_id}.srt";target.parent.mkdir(parents=True,exist_ok=True)
    lines=[]
    for idx,t in enumerate(times,1):
        if not isinstance(t,dict):continue
        start=t.get("start_time",t.get("start"));end=t.get("end_time",t.get("end"));text=t.get("text","")
        if start is None or end is None or not text:continue
        lines.extend([str(idx),f"{srt_time(start)} --> {srt_time(end)}",str(text),""])
    target.write_text("\n".join(lines),encoding="utf-8")
    return str(target)

def process(project):
    stage="语音转写"
    if not db.begin_stage(project['id'],"transcribing","ready_for_ai",stage):return
    model=Qwen3ASRModel.from_pretrained(ASR_MODEL,dtype=torch.bfloat16,device_map="cuda:0",max_inference_batch_size=1,max_new_tokens=4096,forced_aligner=ALIGNER_MODEL,forced_aligner_kwargs={"dtype":torch.bfloat16,"device_map":"cuda:0"})
    try:
        for asset in db.rows("SELECT * FROM assets WHERE project_id=? ORDER BY id",(project['id'],)):
            prior=json.loads(asset.get("analysis") or "{}")
            if "asr_model" in prior:
                if not db.checkpoint(project['id'],"ready_for_ai",stage,asset['filename']):return
                continue
            if not db.checkpoint(project['id'],"ready_for_ai",stage,asset['filename']):return
            result=model.transcribe(audio=asset['audio_path'],language=None,return_time_stamps=True)[0]
            times=[serial_time(x) for x in (getattr(result,"time_stamps",None) or [])]
            payload={**prior,"language":getattr(result,"language",None),"transcript":getattr(result,"text",""),"time_stamps":times,"asr_model":ASR_MODEL,"subtitle_path":write_srt(asset['id'],times)}
            db.execute("UPDATE assets SET analysis=? WHERE id=?",(json.dumps(payload,ensure_ascii=False),asset['id']))
            db.log_event(project['id'],"info",stage,"asset_completed",f"转写已完成：{asset['filename']}",{"asset_id":asset['id'],"language":payload.get('language')})
            if not db.checkpoint(project['id'],"ready_for_ai",stage,asset['filename']):return
        db.finish_stage(project['id'],"ready_for_visual",stage,"全部语音转写完成")
    finally:
        del model;gc.collect();torch.cuda.empty_cache()

def main():
    db.init_db()
    db.recover_interrupted_projects("asr")
    db.start_worker_heartbeat("asr","语音识别")
    while True:
        project=db.row("SELECT p.* FROM projects p JOIN project_control c ON c.project_id=p.id WHERE c.desired_state='running' AND p.status='ready_for_ai' ORDER BY p.id LIMIT 1")
        if not project:time.sleep(POLL);continue
        try:process(project)
        except Exception as exc:
            db.fail_stage(project['id'],"asr_failed","ready_for_ai","语音转写",str(exc),traceback.format_exc());gc.collect();torch.cuda.empty_cache();time.sleep(POLL)
if __name__=="__main__":main()

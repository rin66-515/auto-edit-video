import gc
import json
import os
import time
import torch
from transformers import AutoModelForImageTextToText,AutoProcessor,BitsAndBytesConfig
from . import db

POLL=int(os.getenv("VL_POLL_SECONDS","20"))
MODEL_NAME=os.getenv("VL_MODEL","Qwen/Qwen3-VL-4B-Instruct")
PROMPT="""你是生活Vlog素材分析员。只根据画面返回JSON，不要markdown。字段：summary场景摘要，quality从0到100，problems数组（模糊、抖动、曝光、遮挡等），subjects数组，actions数组，mood，story_value从0到100，suggested_use（主线/B-roll/过场/弃用），duplicate_hint。不要猜测看不到的信息。"""

def load_model():
    quant=BitsAndBytesConfig(load_in_4bit=True,bnb_4bit_quant_type="nf4",bnb_4bit_compute_dtype=torch.bfloat16,bnb_4bit_use_double_quant=True)
    model=AutoModelForImageTextToText.from_pretrained(MODEL_NAME,device_map="auto",quantization_config=quant,dtype=torch.bfloat16)
    return model,AutoProcessor.from_pretrained(MODEL_NAME)

def analyze(model,processor,image_path):
    messages=[{"role":"user","content":[{"type":"image","image":image_path},{"type":"text","text":PROMPT}]}]
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
    text=processor.batch_decode(trimmed,skip_special_tokens=True,clean_up_tokenization_spaces=False)[0].strip().removeprefix("```json").removesuffix("```").strip()
    try:return json.loads(text)
    except json.JSONDecodeError:return {"raw":text,"parse_warning":True}

def create_story_plan(model,processor,project):
    material=[]
    for asset in db.rows("SELECT * FROM assets WHERE project_id=? ORDER BY id",(project['id'],)):
        analysis=json.loads(asset.get("analysis") or "{}")
        material.append({"asset_id":asset['id'],"filename":asset['filename'],"duration":asset['duration'],"transcript":analysis.get('transcript','')[:3000],"visual":analysis.get('visual',{})})
    prompt="""你是生活Vlog总剪辑师。根据素材清单生成可执行剪辑计划，只返回JSON。必须使用真实asset_id，start/end单位为秒且不得超出duration。不要重复相同内容。长篇用于YouTube和Bilibili，目标30分钟以上，但素材不足时不要硬凑；短篇用于抖音和小红书，生成1至3条。结构：{title,summary,long:{chapters:[{name}],timeline:[{asset_id,start,end,chapter,reason}]},shorts:[{title,hook,timeline:[{asset_id,start,end,reason}]}],music_mood:[字符串],review_warnings:[字符串]}。优先故事完整、真实自然，删除模糊、严重抖动和无意义重复。素材："""+json.dumps(material,ensure_ascii=False)
    return text_json(model,processor,prompt[:60000])

def create_bilingual_captions(model,processor,analysis,duration):
    times=analysis.get('time_stamps') or []
    if not times and analysis.get('transcript'):times=[{"start":0,"end":duration,"text":analysis['transcript']}]
    if not times:return []
    prompt="""你是专业中日双语字幕译者。根据原文和时间戳返回JSON，不要markdown。结构必须是{captions:[{start:数字,end:数字,zh:简体中文,ja:自然日语}]}。保留全部时间戳，不增加不存在的信息；人名、地名保持一致，口语自然简短。输入："""+json.dumps({"language":analysis.get('language'),"segments":times},ensure_ascii=False)
    result=text_json(model,processor,prompt[:30000],3072)
    return result.get('captions',[]) if isinstance(result,dict) else []

def process(project):
    db.execute("UPDATE projects SET status='visual_analyzing',updated_at=?,error=NULL WHERE id=?",(db.now(),project['id']))
    model,processor=load_model()
    try:
        for asset in db.rows("SELECT * FROM assets WHERE project_id=? ORDER BY id",(project['id'],)):
            prior=json.loads(asset.get("analysis") or "{}")
            if not prior.get("visual"):
                prior["visual"]=analyze(model,processor,asset['thumbnail_path']);prior["visual_model"]=MODEL_NAME
            if not prior.get("bilingual_captions"):
                prior["bilingual_captions"]=create_bilingual_captions(model,processor,prior,asset.get('duration') or 0)
            db.execute("UPDATE assets SET analysis=? WHERE id=?",(json.dumps(prior,ensure_ascii=False),asset['id']))
        settings=json.loads(project.get('settings') or '{}');settings['story_plan']=create_story_plan(model,processor,project);settings['story_model']=MODEL_NAME
        db.execute("UPDATE projects SET status='draft_ready',settings=?,updated_at=? WHERE id=?",(json.dumps(settings,ensure_ascii=False),db.now(),project['id']))
    finally:
        del model,processor;gc.collect();torch.cuda.empty_cache()

def revise(project):
    db.execute("UPDATE projects SET status='revision_planning',updated_at=?,error=NULL WHERE id=?",(db.now(),project['id']))
    model,processor=load_model()
    try:
        settings=json.loads(project.get('settings') or '{}');revisions=db.rows("SELECT id,kind,body FROM revisions WHERE project_id=? AND status='open' ORDER BY id",(project['id'],))
        prompt="""你是生活Vlog总剪辑师。根据人工审核意见修订现有剪辑计划，只返回完整story_plan JSON，保持原有结构。必须使用已有asset_id，start/end为秒，不得发明素材；没有要求修改的部分尽量保持不变。现有计划："""+json.dumps(settings.get('story_plan',{}),ensure_ascii=False)+"\n审核意见："+json.dumps(revisions,ensure_ascii=False)
        settings['story_plan']=text_json(model,processor,prompt[:60000]);settings['story_model']=MODEL_NAME
        db.execute("UPDATE projects SET status='draft_ready',settings=?,updated_at=? WHERE id=?",(json.dumps(settings,ensure_ascii=False),db.now(),project['id']))
        db.execute("UPDATE revisions SET status='resolved',resolved_at=? WHERE project_id=? AND status='open'",(db.now(),project['id']))
    finally:
        del model,processor;gc.collect();torch.cuda.empty_cache()

def main():
    db.init_db()
    while True:
        project=db.row("SELECT * FROM projects WHERE status IN ('ready_for_visual','revision_requested') ORDER BY CASE status WHEN 'revision_requested' THEN 0 ELSE 1 END,id LIMIT 1")
        if not project:time.sleep(POLL);continue
        try:revise(project) if project['status']=='revision_requested' else process(project)
        except Exception as exc:
            db.execute("UPDATE projects SET status='visual_failed',error=?,updated_at=? WHERE id=?",(str(exc),db.now(),project['id']));gc.collect();torch.cuda.empty_cache();time.sleep(POLL)
if __name__=="__main__":main()

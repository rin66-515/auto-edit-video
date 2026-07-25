import json
import os
import shutil
import subprocess
import time
import traceback
from pathlib import Path
from . import db

POLL=int(os.getenv('AUDIO_POLL_SECONDS','20'))

def enhance(asset,project_slug):
    source=Path(asset['audio_path']);outdir=Path('/vlog/_automation/audio')/project_slug/'enhanced'/f"asset-{asset['id']}";outdir.mkdir(parents=True,exist_ok=True)
    result=subprocess.run(['deepFilter','--pf','--output-dir',str(outdir),str(source)],capture_output=True,text=True)
    candidates=sorted(outdir.glob('*.wav'),key=lambda p:p.stat().st_mtime,reverse=True)
    analysis=json.loads(asset.get('analysis') or '{}')
    if result.returncode==0 and candidates:
        target=outdir/f"asset-{asset['id']}-enhanced.wav"
        if candidates[0]!=target:shutil.move(str(candidates[0]),str(target))
        analysis['audio_cleanup']={'engine':'DeepFilterNet3','status':'enhanced'};audio_path=str(target)
    else:
        analysis['audio_cleanup']={'engine':'FFmpeg fallback','status':'warning','detail':(result.stderr or result.stdout)[-1000:]};audio_path=str(source)
    db.execute('UPDATE assets SET audio_path=?,analysis=? WHERE id=?',(audio_path,json.dumps(analysis,ensure_ascii=False),asset['id']))
    return analysis['audio_cleanup']

def process(project):
    stage="音频清理"
    if not db.begin_stage(project['id'],"audio_cleaning","ready_for_audio",stage):return
    for asset in db.rows('SELECT * FROM assets WHERE project_id=? ORDER BY id',(project['id'],)):
        prior=json.loads(asset.get('analysis') or '{}')
        if prior.get('audio_cleanup'):
            if not db.checkpoint(project['id'],"ready_for_audio",stage,asset['filename']):return
            continue
        if not db.checkpoint(project['id'],"ready_for_audio",stage,asset['filename']):return
        cleanup=enhance(asset,project['slug'])
        level="warning" if cleanup.get('status')=="warning" else "info"
        db.log_event(project['id'],level,stage,"asset_completed",f"音频已处理：{asset['filename']}",{"asset_id":asset['id'],**cleanup})
        if not db.checkpoint(project['id'],"ready_for_audio",stage,asset['filename']):return
    db.finish_stage(project['id'],"ready_for_ai",stage,"全部音频清理完成")

def main():
    db.init_db()
    db.recover_interrupted_projects("audio")
    db.start_worker_heartbeat("audio","音频清理")
    while True:
        project=db.row("SELECT p.* FROM projects p JOIN project_control c ON c.project_id=p.id WHERE c.desired_state='running' AND p.status='ready_for_audio' ORDER BY p.id LIMIT 1")
        if not project:time.sleep(POLL);continue
        try:process(project)
        except Exception as exc:
            db.fail_stage(project['id'],"audio_failed","ready_for_audio","音频清理",str(exc),traceback.format_exc());time.sleep(POLL)
if __name__=='__main__':main()

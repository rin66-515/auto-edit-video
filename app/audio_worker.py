import json
import os
import shutil
import subprocess
import time
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

def process(project):
    db.execute("UPDATE projects SET status='audio_cleaning',updated_at=?,error=NULL WHERE id=?",(db.now(),project['id']))
    for asset in db.rows('SELECT * FROM assets WHERE project_id=? ORDER BY id',(project['id'],)):enhance(asset,project['slug'])
    db.execute("UPDATE projects SET status='ready_for_ai',updated_at=? WHERE id=?",(db.now(),project['id']))

def main():
    db.init_db()
    while True:
        project=db.row("SELECT * FROM projects WHERE status='ready_for_audio' ORDER BY id LIMIT 1")
        if not project:time.sleep(POLL);continue
        try:process(project)
        except Exception as exc:
            db.execute("UPDATE projects SET status='audio_failed',error=?,updated_at=? WHERE id=?",(str(exc),db.now(),project['id']));time.sleep(POLL)
if __name__=='__main__':main()


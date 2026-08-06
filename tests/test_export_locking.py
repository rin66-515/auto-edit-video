import json
import unittest

from fastapi import HTTPException

from app import db,main
from _support import IsolatedDbTestCase


class ExportLockingTest(IsolatedDbTestCase):
    def setUp(self):
        super().setUp()
        stamp=db.now();self.slug=f"export-lock-{id(self)}"
        settings={
            "story_plan":{
                "long":{"timeline":[{"asset_id":1,"start":0.0,"end":10.0,"audio_mode":"ambient"}]},
                "shorts":[{"timeline":[{"asset_id":1,"start":0.0,"end":3.0,"audio_mode":"mute"}]}],
            },
        }
        self.project_id=db.execute(
            "INSERT INTO projects(slug,title,source_dir,status,settings,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (self.slug,"Export Lock",f"/vlog/inbox/{self.slug}","review_ready",json.dumps(settings),stamp,stamp),
        )
        db.create_control(self.project_id,"stopped",None,"人工审核")
        db.execute(
            "INSERT INTO assets(id,project_id,path,filename,bytes,duration,analysis,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (1,self.project_id,f"/vlog/inbox/{self.slug}/1.mp4","1.mp4",1,20.0,json.dumps({"bilingual_captions":[]}),stamp),
        )
        self.short_id=db.execute(
            "INSERT INTO exports(project_id,version,format,path,status,locked,approved_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (self.project_id,"v15","short_9x16",'["/tmp/short.mp4"]',"approved",1,stamp,stamp),
        )

    def test_locked_short_blocks_short_and_combined_but_not_long(self):
        for fmt in ("short_9x16","both"):
            with self.assertRaises(HTTPException) as blocked:
                main.generate_export(self.project_id,fmt)
            self.assertEqual(409,blocked.exception.status_code)
            self.assertIn("v15",str(blocked.exception.detail))

        created=main._create_export(self.project_id,"long_16x9")
        self.assertEqual("render_requested",created["status"])
        export=db.row("SELECT format,render_options FROM exports WHERE id=?",(created["id"],))
        self.assertEqual("long_16x9",export["format"])
        self.assertEqual("speech_aligned_no_bgm_v1",json.loads(export["render_options"])["long_audio_policy"])

    def test_unlock_preserves_file_and_allows_new_short(self):
        result=main.unlock_export(self.short_id)
        self.assertEqual("review_ready",result["status"])
        unlocked=db.row("SELECT status,locked,approved_at,path FROM exports WHERE id=?",(self.short_id,))
        self.assertEqual("review_ready",unlocked["status"])
        self.assertEqual(0,unlocked["locked"])
        self.assertIsNone(unlocked["approved_at"])
        self.assertEqual('["/tmp/short.mp4"]',unlocked["path"])

        created=main._create_export(self.project_id,"short_9x16")
        self.assertEqual("render_requested",created["status"])


if __name__=="__main__":unittest.main()

import json
import unittest

from fastapi import HTTPException

from app import db,main
from _support import IsolatedDbTestCase


class PendingRevisionGateTest(IsolatedDbTestCase):
    def setUp(self):
        super().setUp()
        stamp=db.now();self.project_id=db.execute(
            "INSERT INTO projects(slug,title,source_dir,status,settings,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("revision-gate","Revision Gate","/vlog/inbox/revision-gate","caption_review_ready",json.dumps({
                "story_plan":{
                    "long":{"timeline":[{"asset_id":1,"start":0.0,"end":12.0,"audio_mode":"dialogue"}]},
                    "shorts":[{"timeline":[{"asset_id":1,"start":0.0,"end":3.0,"audio_mode":"mute"}]}],
                },
            }),stamp,stamp),
        )
        db.create_control(self.project_id,"stopped",None,"人工审核")
        db.execute(
            "INSERT INTO assets(id,project_id,path,filename,bytes,duration,analysis,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (1,self.project_id,"/tmp/source.mp4","source.mp4",1,20.0,json.dumps({"bilingual_captions":[]}),stamp),
        )
        self.long_id=db.execute(
            "INSERT INTO exports(project_id,version,format,status,timeline_snapshot,created_at) VALUES(?,?,?,?,?,?)",
            (self.project_id,"v1","long_16x9","caption_review_ready",json.dumps({"long":[{"asset_id":1,"start":0.0,"end":12.0,"audio_mode":"dialogue"}]}),stamp),
        )

    def test_non_privacy_revision_blocks_direct_long_render(self):
        stamp=db.now();db.execute(
            "INSERT INTO revisions(project_id,kind,body,source_export_id,source_version,created_at) VALUES(?,?,?,?,?,?)",
            (self.project_id,"full_replan","长篇改成40-50分钟",self.long_id,"v1",stamp),
        )
        with self.assertRaises(HTTPException) as blocked:
            main.generate_export(self.project_id,"long_16x9")
        self.assertEqual(409,blocked.exception.status_code)
        self.assertIn("v1",str(blocked.exception.detail))

    def test_privacy_revision_does_not_block_direct_render_gate(self):
        stamp=db.now();db.execute(
            "INSERT INTO revisions(project_id,kind,body,source_export_id,source_version,created_at) VALUES(?,?,?,?,?,?)",
            (self.project_id,"privacy","2秒右侧人物加马赛克",self.long_id,"v1",stamp),
        )
        self.assertEqual([],main._pending_version_revisions(self.project_id,("long_16x9",)))

    def test_exact_timecode_cannot_be_saved_as_full_replan(self):
        logs_before=db.row("SELECT COUNT(*) AS count FROM project_logs WHERE project_id=?",(self.project_id,))["count"]
        with self.assertRaises(HTTPException) as blocked:
            main.add_revision(self.project_id,main.RevisionIn(
                kind="full_replan",
                body="2分44秒到2分59秒切掉",
                source_export_id=self.long_id,
            ))
        self.assertEqual(409,blocked.exception.status_code)
        self.assertIn("局部剪辑调整",str(blocked.exception.detail))
        self.assertEqual(0,db.row("SELECT COUNT(*) AS count FROM revisions WHERE project_id=?",(self.project_id,))["count"])
        self.assertEqual(logs_before,db.row("SELECT COUNT(*) AS count FROM project_logs WHERE project_id=?",(self.project_id,))["count"])

    def test_unparseable_incremental_revision_is_rejected_without_database_or_log_write(self):
        revisions_before=db.row("SELECT COUNT(*) AS count FROM revisions WHERE project_id=?",(self.project_id,))["count"]
        logs_before=db.row("SELECT COUNT(*) AS count FROM project_logs WHERE project_id=?",(self.project_id,))["count"]
        with self.assertRaises(HTTPException) as blocked:
            main.add_revision(self.project_id,main.RevisionIn(
                kind="edit",
                body="整体再好看一点",
                source_export_id=self.long_id,
            ))
        self.assertEqual(409,blocked.exception.status_code)
        self.assertIn("尚未解析",str(blocked.exception.detail))
        self.assertEqual(revisions_before,db.row("SELECT COUNT(*) AS count FROM revisions WHERE project_id=?",(self.project_id,))["count"])
        self.assertEqual(logs_before,db.row("SELECT COUNT(*) AS count FROM project_logs WHERE project_id=?",(self.project_id,))["count"])

    def test_valid_exact_incremental_revision_is_saved_without_creating_export(self):
        result=main.add_revision(self.project_id,main.RevisionIn(
            kind="edit",
            body="2秒到4秒切掉",
            source_export_id=self.long_id,
        ))
        self.assertTrue(result["ok"])
        self.assertEqual(1,db.row("SELECT COUNT(*) AS count FROM revisions WHERE project_id=?",(self.project_id,))["count"])
        self.assertEqual(1,db.row("SELECT COUNT(*) AS count FROM exports WHERE project_id=?",(self.project_id,))["count"])

    def test_legacy_full_replan_with_exact_timecode_is_blocked_when_applied(self):
        stamp=db.now();db.execute(
            "INSERT INTO revisions(project_id,kind,body,source_export_id,source_version,created_at) VALUES(?,?,?,?,?,?)",
            (self.project_id,"full_replan","2分44秒到2分59秒切掉",self.long_id,"v1",stamp),
        )
        with self.assertRaises(HTTPException) as blocked:
            main.apply_version_revisions(self.long_id,main.RevisionApplyIn(confirm_full_replan=True))
        self.assertEqual(409,blocked.exception.status_code)
        self.assertIn("不能执行完整重规划",str(blocked.exception.detail))


if __name__=="__main__":unittest.main()

import json
import unittest

from fastapi import HTTPException
from app import db,main
from _support import IsolatedDbTestCase


class ExportDeleteTest(IsolatedDbTestCase):
    def setUp(self):
        super().setUp()
        self._original_outputs=main.OUTPUTS
        self.addCleanup(self._restore_outputs)
        main.OUTPUTS=self.test_root/"outputs"
        stamp=db.now();self.slug=f"delete-export-{id(self)}-{stamp.replace(':','-')}"
        self.project_id=db.execute("INSERT INTO projects(slug,title,source_dir,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",(self.slug,"Delete Export Test",f"/tmp/{self.slug}","review_ready",stamp,stamp))
        db.create_control(self.project_id,"running",None,"人工审核")
        self.output_dir=main.OUTPUTS/self.slug/"v9";self.output_dir.mkdir(parents=True,exist_ok=True)
        self.video=self.output_dir/"long.mp4";self.subtitle=self.output_dir/"long.zh-ja.srt"
        self.video.write_bytes(b"video");self.subtitle.write_text("subtitle",encoding="utf-8")
        self.export_id=db.execute("INSERT INTO exports(project_id,version,format,path,status,created_at) VALUES(?,?,?,?,?,?)",(self.project_id,"v9","long_16x9",json.dumps([str(self.video)]),"review_ready",stamp))

    def _restore_outputs(self):
        main.OUTPUTS=self._original_outputs

    def test_requires_stopped_confirmation_state_then_deletes_only_version_files(self):
        with self.assertRaises(HTTPException) as blocked:
            main.delete_export(self.export_id)
        self.assertEqual(409,blocked.exception.status_code);self.assertTrue(self.video.exists());self.assertIsNotNone(db.row("SELECT id FROM exports WHERE id=?",(self.export_id,)))
        db.create_control(self.project_id,"stopped",None,"人工审核")
        result=main.delete_export(self.export_id)
        self.assertEqual(2,result["deleted_files"]);self.assertFalse(self.video.exists());self.assertFalse(self.subtitle.exists());self.assertIsNone(db.row("SELECT id FROM exports WHERE id=?",(self.export_id,)))
        log=db.row("SELECT event,message FROM project_logs WHERE project_id=? ORDER BY id DESC LIMIT 1",(self.project_id,))
        self.assertEqual("export_deleted",log["event"]);self.assertIn("v9",log["message"])

    def test_deleting_last_pending_export_clears_stale_resume_state(self):
        db.execute("DELETE FROM exports WHERE id=?",(self.export_id,))
        approved_id=db.execute(
            "INSERT INTO exports(project_id,version,format,path,status,locked,created_at) VALUES(?,?,?,?,?,?,?)",
            (self.project_id,"v8","short_9x16",json.dumps(["/vlog/outputs/v8.mp4"]),"approved",1,db.now()),
        )
        pending_id=db.execute(
            "INSERT INTO exports(project_id,version,format,status,created_at) VALUES(?,?,?,?,?)",
            (self.project_id,"v10","short_9x16","render_requested",db.now()),
        )
        db.execute("UPDATE projects SET status='render_requested' WHERE id=?",(self.project_id,))
        db.create_control(self.project_id,"stopped","render_requested","成片渲染","v10 等待渲染",render_scope="short_9x16")

        main.delete_export(pending_id)

        self.assertIsNotNone(db.row("SELECT id FROM exports WHERE id=?",(approved_id,)))
        self.assertEqual("review_ready",db.row("SELECT status FROM projects WHERE id=?",(self.project_id,))["status"])
        control=db.control(self.project_id)
        self.assertEqual("stopped",control["desired_state"])
        self.assertIsNone(control["resume_status"])
        self.assertIsNone(control["render_scope"])
        self.assertEqual("人工审核",control["stage"])
        self.assertEqual("没有待渲染版本",control["item"])


if __name__=="__main__":unittest.main()

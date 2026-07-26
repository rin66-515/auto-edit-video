import json
import shutil
import unittest

from fastapi import HTTPException
from app import db,main
from app.config import OUTPUTS


class ExportDeleteTest(unittest.TestCase):
    def setUp(self):
        db.init_db();stamp=db.now();self.slug=f"delete-export-{id(self)}-{stamp.replace(':','-')}"
        self.project_id=db.execute("INSERT INTO projects(slug,title,source_dir,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",(self.slug,"Delete Export Test",f"/tmp/{self.slug}","review_ready",stamp,stamp))
        db.create_control(self.project_id,"running",None,"人工审核")
        self.output_dir=OUTPUTS/self.slug/"v9";self.output_dir.mkdir(parents=True,exist_ok=True)
        self.video=self.output_dir/"long.mp4";self.subtitle=self.output_dir/"long.zh-ja.srt"
        self.video.write_bytes(b"video");self.subtitle.write_text("subtitle",encoding="utf-8")
        self.export_id=db.execute("INSERT INTO exports(project_id,version,format,path,status,created_at) VALUES(?,?,?,?,?,?)",(self.project_id,"v9","long_16x9",json.dumps([str(self.video)]),"review_ready",stamp))

    def tearDown(self):
        shutil.rmtree(OUTPUTS/self.slug,ignore_errors=True)

    def test_requires_stopped_confirmation_state_then_deletes_only_version_files(self):
        with self.assertRaises(HTTPException) as blocked:
            main.delete_export(self.export_id)
        self.assertEqual(409,blocked.exception.status_code);self.assertTrue(self.video.exists());self.assertIsNotNone(db.row("SELECT id FROM exports WHERE id=?",(self.export_id,)))
        db.create_control(self.project_id,"stopped",None,"人工审核")
        result=main.delete_export(self.export_id)
        self.assertEqual(2,result["deleted_files"]);self.assertFalse(self.video.exists());self.assertFalse(self.subtitle.exists());self.assertIsNone(db.row("SELECT id FROM exports WHERE id=?",(self.export_id,)))
        log=db.row("SELECT event,message FROM project_logs WHERE project_id=? ORDER BY id DESC LIMIT 1",(self.project_id,))
        self.assertEqual("export_deleted",log["event"]);self.assertIn("v9",log["message"])


if __name__=="__main__":unittest.main()

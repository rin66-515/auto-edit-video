import json
import tempfile
import unittest
from pathlib import Path

from app import db
from app.manual_revision import create_privacy_revision
from app.short_revision import release_scheduled_short


class ScheduledShortReleaseTest(unittest.TestCase):
    def test_release_waits_for_completed_predecessor_and_keeps_project_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            original=db.DB_PATH;db.DB_PATH=Path(folder)/"test.db"
            try:
                db.init_db();stamp=db.now()
                project_id=db.execute("INSERT INTO projects(slug,title,source_dir,status,settings,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",("queue-test","Queue Test","/vlog/inbox/queue-test","review_ready","{}",stamp,stamp))
                db.create_control(project_id,"stopped",None,"人工审核","等待人工审核")
                long_id=db.execute("INSERT INTO exports(project_id,version,format,status,timeline_snapshot,created_at) VALUES(?,?,?,?,?,?)",(project_id,"v6","long_16x9","review_ready",json.dumps({"long":[{"asset_id":1,"start":0,"end":1}]}),stamp))
                short_id=db.execute("INSERT INTO exports(project_id,version,format,status,timeline_snapshot,render_options,source_export_id,created_at) VALUES(?,?,?,?,?,?,?,?)",(project_id,"v7","short_9x16","scheduled",json.dumps({"short-1":[{"asset_id":1,"start":0,"end":1}]}),json.dumps({"queue_after_export_id":long_id}),long_id,stamp))
                result=release_scheduled_short(short_id)
                self.assertEqual("render_requested",result["status"])
                self.assertEqual("render_requested",db.row("SELECT status FROM exports WHERE id=?",(short_id,))["status"])
                self.assertEqual("render_requested",db.row("SELECT status FROM projects WHERE id=?",(project_id,))["status"])
                control=db.control(project_id);self.assertEqual("stopped",control["desired_state"]);self.assertEqual("render_requested",control["resume_status"])
            finally:db.DB_PATH=original

    def test_short_privacy_revision_can_keep_only_anchor_output(self):
        with tempfile.TemporaryDirectory() as folder:
            original=db.DB_PATH;db.DB_PATH=Path(folder)/"test.db"
            try:
                db.init_db();stamp=db.now();project_id=db.execute("INSERT INTO projects(slug,title,source_dir,status,settings,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",("short-test","Short Test","/vlog/inbox/short-test","review_ready","{}",stamp,stamp));db.create_control(project_id,"stopped",None,"人工审核","等待人工审核")
                snapshot={"short-1":[{"asset_id":1,"start":0,"end":2}],"short-2":[{"asset_id":2,"start":0,"end":2}]};source_id=db.execute("INSERT INTO exports(project_id,version,format,status,timeline_snapshot,render_options,created_at) VALUES(?,?,?,?,?,?,?)",(project_id,"v7","short_9x16","review_ready",json.dumps(snapshot),json.dumps({"short_style_profiles":["city_pulse","warm_rhythm"]}),stamp))
                result=create_privacy_revision(project_id,source_id,{"force_cover":[{"start":0,"end":2.4}]},"cover anchor",["short-1"]);created=db.row("SELECT timeline_snapshot,render_options,render_mode FROM exports WHERE id=?",(result["export_id"],))
                self.assertEqual(["short-1"],list(json.loads(created["timeline_snapshot"])));self.assertEqual(["short-1"],json.loads(created["render_options"])["kept_outputs"]);self.assertEqual("privacy_only",created["render_mode"])
            finally:db.DB_PATH=original


if __name__=="__main__":unittest.main()

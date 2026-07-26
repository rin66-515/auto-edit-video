import unittest

from app import db
from _support import IsolatedDbTestCase


class WorkerRecoveryTest(IsolatedDbTestCase):
    def create_project(self,status,resume,stage):
        stamp=db.now();slug=f"recovery-{status}-{id(self)}-{stamp}"
        project_id=db.execute("INSERT INTO projects(slug,title,source_dir,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",(slug,"Recovery Test",f"/tmp/{slug}",status,stamp,stamp))
        db.create_control(project_id,"running",resume,stage)
        return project_id

    def test_app_restart_does_not_recover_live_visual_stage(self):
        project_id=self.create_project("revision_planning","revision_requested","修改剪辑方案")
        db.recover_interrupted_projects("app")
        self.assertEqual("revision_planning",db.row("SELECT status FROM projects WHERE id=?",(project_id,))["status"])
        self.assertEqual("running",db.control(project_id)["desired_state"])
        db.recover_interrupted_projects("visual")
        self.assertEqual("revision_requested",db.row("SELECT status FROM projects WHERE id=?",(project_id,))["status"])
        self.assertEqual("stopped",db.control(project_id)["desired_state"])

    def test_app_restart_recovers_its_own_render_stage(self):
        project_id=self.create_project("rendering","render_requested","成片渲染")
        db.recover_interrupted_projects("app")
        self.assertEqual("render_requested",db.row("SELECT status FROM projects WHERE id=?",(project_id,))["status"])
        self.assertEqual("stopped",db.control(project_id)["desired_state"])


if __name__=="__main__":unittest.main()

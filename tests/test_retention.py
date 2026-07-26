import json
import unittest


from app import db, main, pipeline
from _support import IsolatedDbTestCase


class RetentionWorkflowTest(IsolatedDbTestCase):
    def setUp(self):
        super().setUp()
        names=("INBOX","PROXIES","AUDIO","PROJECTS","OUTPUTS")
        self._original_paths={name:getattr(pipeline,name) for name in names}
        self.addCleanup(self._restore_paths)
        folders={"INBOX":"inbox","PROXIES":"proxies","AUDIO":"audio","PROJECTS":"projects","OUTPUTS":"outputs"}
        for name,folder in folders.items():setattr(pipeline,name,self.test_root/folder)

    def _restore_paths(self):
        for name,path in self._original_paths.items():setattr(pipeline,name,path)

    def test_upload_toggle_and_retention_cleanup(self):
        db.init_db()
        source = pipeline.INBOX / "trip"
        source.mkdir(parents=True)
        raw = source / "raw.mp4"
        raw.write_bytes(b"raw-video")
        stamp = db.now()
        project_id = db.execute(
            "INSERT INTO projects(slug,title,source_dir,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("trip", "Trip", str(source), "review_ready", stamp, stamp),
        )
        proxy = pipeline.PROXIES / "trip" / "asset.mp4"
        audio = pipeline.AUDIO / "trip" / "enhanced" / "asset.wav"
        cache = pipeline.PROJECTS / "trip" / "thumbnails" / "asset.jpg"
        temporary = pipeline.PROJECTS / "trip" / "render-temp" / "part.mp4"
        for path in (proxy, audio, cache, temporary):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"intermediate")
        db.execute(
            "INSERT INTO assets(project_id,path,filename,bytes,proxy_path,audio_path,thumbnail_path,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (project_id, str(raw), raw.name, raw.stat().st_size, str(proxy), str(audio), str(cache), stamp),
        )
        db.create_control(project_id, "stopped", None, "人工审核", None)
        long_final = pipeline.OUTPUTS / "trip" / "v1" / "long.mp4"
        short_final = pipeline.OUTPUTS / "trip" / "v2" / "short.mp4"
        for path in (long_final, short_final):
            path.parent.mkdir(parents=True)
            path.write_bytes(b"final-video")
        long_export_id = db.execute(
            "INSERT INTO exports(project_id,version,format,path,status,locked,created_at,approved_at) VALUES(?,?,?,?,?,?,?,?)",
            (project_id, "v1", "long_16x9", json.dumps([str(long_final)]), "approved", 1, stamp, stamp),
        )
        db.execute(
            "INSERT INTO exports(project_id,version,format,path,status,locked,created_at,approved_at) VALUES(?,?,?,?,?,?,?,?)",
            (project_id, "v2", "short_9x16", json.dumps([str(short_final)]), "approved", 1, stamp, stamp),
        )

        for platform in main.PLATFORMS[:-1]:
            main.upload_done(project_id, platform)
        db.create_control(project_id, "running", "review_ready", "人工审核", None)
        with self.assertRaises(main.HTTPException):
            main.upload_done(project_id, main.PLATFORMS[-1])
        db.create_control(project_id, "stopped", None, "人工审核", None)
        main.upload_done(project_id, main.PLATFORMS[-1])
        project = db.row("SELECT * FROM projects WHERE id=?", (project_id,))
        self.assertEqual(project["status"], "published")
        self.assertIsNotNone(project["upload_confirmed_at"])
        self.assertFalse(temporary.exists())

        main.cancel_upload_done(project_id, "youtube")
        project = db.row("SELECT * FROM projects WHERE id=?", (project_id,))
        self.assertEqual(project["status"], "review_ready")
        self.assertIsNone(project["upload_confirmed_at"])
        self.assertIsNone(db.row("SELECT platform FROM platform_uploads WHERE project_id=? AND platform='youtube'", (project_id,)))

        final = long_final
        subtitle = final.with_suffix(".zh-ja.srt")
        subtitle.write_bytes(b"subtitle")
        db.execute(
            "UPDATE exports SET approved_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (long_export_id,),
        )
        pipeline.retention_once()
        self.assertTrue(final.exists(), "未完成四平台确认时不得删除成片")
        db.execute(
            "UPDATE projects SET upload_confirmed_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (project_id,),
        )
        pipeline.retention_once()

        project = db.row("SELECT * FROM projects WHERE id=?", (project_id,))
        export = db.row("SELECT * FROM exports WHERE id=?", (long_export_id,))
        self.assertIsNotNone(project["intermediates_deleted_at"])
        self.assertFalse(raw.exists())
        self.assertFalse(proxy.exists())
        self.assertFalse(audio.exists())
        self.assertFalse(cache.exists())
        self.assertFalse(final.exists())
        self.assertFalse(subtitle.exists())
        self.assertEqual(export["status"], "expired")
        self.assertEqual(export["path"], "[]")

        guarded_source = pipeline.INBOX / "guarded"
        guarded_source.mkdir(parents=True)
        guarded_id = db.execute(
            "INSERT INTO projects(slug,title,source_dir,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("guarded", "Guarded", str(guarded_source), "ready_for_visual", stamp, stamp),
        )
        db.create_control(guarded_id, "stopped", "ready_for_visual", "画面分析与双语字幕", None)
        with self.assertRaises(main.HTTPException) as blocked:
            main.control_project(guarded_id, "start")
        self.assertEqual(blocked.exception.status_code, 503)
        db.worker_heartbeat("visual", "画面分析与双语字幕")
        result = main.control_project(guarded_id, "start")
        self.assertEqual(result["control"]["desired_state"], "running")
        self.assertTrue(next(x for x in result["workers"] if x["worker"] == "visual")["online"])


if __name__ == "__main__":
    unittest.main()

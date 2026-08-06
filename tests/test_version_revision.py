import json
import tempfile
import unittest
from pathlib import Path

from app import db
from app.media import _segment_audio_filter
from app.version_revision import create_inherited_revision,validate_inherited_revision


class InheritedVersionRevisionTest(unittest.TestCase):
    def test_validation_prepares_operations_without_database_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            original=db.DB_PATH;db.DB_PATH=Path(folder)/"test.db"
            try:
                db.init_db();stamp=db.now()
                project_id=db.execute("INSERT INTO projects(slug,title,source_dir,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",("validate-only","Validate Only","/vlog/inbox/validate-only","caption_review_ready",stamp,stamp))
                asset_id=db.execute("INSERT INTO assets(project_id,path,filename,bytes,duration,analysis,created_at) VALUES(?,?,?,?,?,?,?)",(project_id,"/tmp/source.mp4","source.mp4",1,12,json.dumps({}),stamp))
                source_id=db.execute("INSERT INTO exports(project_id,version,format,status,timeline_snapshot,created_at) VALUES(?,?,?,?,?,?)",(project_id,"v1","long_16x9","caption_review_ready",json.dumps({"long":[{"asset_id":asset_id,"start":0.0,"end":12.0,"audio_mode":"denoise"}]}),stamp))
                before={
                    "exports":db.row("SELECT COUNT(*) AS count FROM exports WHERE project_id=?",(project_id,))["count"],
                    "revisions":db.row("SELECT COUNT(*) AS count FROM revisions WHERE project_id=?",(project_id,))["count"],
                    "logs":db.row("SELECT COUNT(*) AS count FROM project_logs WHERE project_id=?",(project_id,))["count"],
                }

                result=validate_inherited_revision(project_id,source_id,[{"kind":"edit","body":"2秒到4秒切掉"}])

                self.assertEqual(["delete_output_range"],[value["kind"] for value in result["operations"]])
                self.assertEqual(before["exports"],db.row("SELECT COUNT(*) AS count FROM exports WHERE project_id=?",(project_id,))["count"])
                self.assertEqual(before["revisions"],db.row("SELECT COUNT(*) AS count FROM revisions WHERE project_id=?",(project_id,))["count"])
                self.assertEqual(before["logs"],db.row("SELECT COUNT(*) AS count FROM project_logs WHERE project_id=?",(project_id,))["count"])
            finally:db.DB_PATH=original

    def test_audio_revision_preserves_timeline_and_delays_ducking_until_dialogue(self):
        with tempfile.TemporaryDirectory() as folder:
            original=db.DB_PATH;db.DB_PATH=Path(folder)/"test.db"
            try:
                db.init_db();stamp=db.now()
                project_id=db.execute("INSERT INTO projects(slug,title,source_dir,status,settings,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",("inherit-test","Inherit Test","/vlog/inbox/inherit-test","caption_review_ready","{}",stamp,stamp))
                db.create_control(project_id,"stopped",None,"人工审核","等待人工审核")
                asset1=db.execute("INSERT INTO assets(project_id,path,filename,bytes,duration,analysis,created_at) VALUES(?,?,?,?,?,?,?)",(project_id,"/tmp/a.mp4","DJI_TEST_0001_D.MP4",1,10,json.dumps({}),stamp))
                captions={"bilingual_captions":[{"start":20.16,"end":20.32},{"start":21.6,"end":23.0}]}
                asset2=db.execute("INSERT INTO assets(project_id,path,filename,bytes,duration,analysis,created_at) VALUES(?,?,?,?,?,?,?)",(project_id,"/tmp/b.mp4","DJI_TEST_0002_D.MP4",1,30,json.dumps(captions),stamp))
                source_timeline=[
                    {"asset_id":asset1,"start":0.0,"end":0.95,"audio_mode":"mute","transition":"cut"},
                    {"asset_id":asset2,"start":19.833166,"end":22.833166,"audio_mode":"dialogue","background_cleanup":True,"duck_bgm":True,"transition":"cut"},
                ]
                source_id=db.execute("INSERT INTO exports(project_id,version,format,status,timeline_snapshot,render_options,created_at) VALUES(?,?,?,?,?,?,?)",(project_id,"v13","short_9x16","caption_review_ready",json.dumps({"short-1":source_timeline}),json.dumps({"bgm_filename":"song.mp3","bgm_duration":3.95}),stamp))
                revision_id=db.execute("INSERT INTO revisions(project_id,kind,body,source_export_id,source_version,created_at) VALUES(?,?,?,?,?,?)",(project_id,"audio","第二秒多播放一些BGM，到2后最后几帧，出现人说话才主要捕捉人声，减小BGM。",source_id,"v13",stamp))
                result=create_inherited_revision(project_id,source_id,[{"id":revision_id,"kind":"audio","body":"第二秒多播放一些BGM，到2后最后几帧，出现人说话才主要捕捉人声，减小BGM。"}],revision_ids=[revision_id])
                created=db.row("SELECT * FROM exports WHERE id=?",(result["export_id"],));timeline=json.loads(created["timeline_snapshot"])["short-1"]
                self.assertEqual(2,len(timeline));self.assertEqual(source_timeline[0],timeline[0])
                self.assertAlmostEqual(1.766834,timeline[1]["dialogue_start_offset"],places=5)
                self.assertAlmostEqual(2.716834,result["operations"][0]["dialogue_output_start"],places=3)
                self.assertEqual("caption_review_ready",db.row("SELECT status FROM exports WHERE id=?",(source_id,))["status"])
                self.assertEqual("stopped",db.control(project_id)["desired_state"])
                self.assertEqual("render_requested",db.control(project_id)["resume_status"])
                self.assertIn("enable='lt(t,1.766834)'",_segment_audio_filter(timeline[1]))
            finally:db.DB_PATH=original

    def test_long_range_rebalance_uses_asset_codes_and_carries_privacy_feedback(self):
        with tempfile.TemporaryDirectory() as folder:
            original=db.DB_PATH;db.DB_PATH=Path(folder)/"test.db"
            try:
                db.init_db();stamp=db.now()
                project_id=db.execute("INSERT INTO projects(slug,title,source_dir,status,settings,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",("long-rebalance","Long Rebalance","/vlog/inbox/long-rebalance","caption_review_ready","{}",stamp,stamp))
                db.create_control(project_id,"stopped",None,"人工审核","等待人工审核")
                def add_asset(filename,duration):
                    return db.execute("INSERT INTO assets(project_id,path,filename,bytes,duration,analysis,created_at) VALUES(?,?,?,?,?,?,?)",(project_id,f"/tmp/{filename}",filename,1,duration,json.dumps({}),stamp))
                intro=add_asset("DJI_TEST_0040_D.MP4",960)
                billiards=add_asset("DJI_TEST_0050_D.MP4",300)
                recommended=[
                    add_asset("DJI_20260613181757_0057_D.MP4",13),
                    add_asset("DJI_20260613205208_0059_D.MP4",15),
                    add_asset("DJI_20260614174828_0081_D.MP4",30),
                    add_asset("DJI_20260614175323_0082_D.MP4",26),
                    add_asset("DJI_20260614191226_0083_D.MP4",9),
                    add_asset("DJI_20260614204650_0084_D.MP4",6),
                ]
                source_timeline=[{"asset_id":intro,"start":0.0,"end":960.0,"audio_mode":"denoise","chapter":"出发"}]
                source_timeline.extend(
                    {"asset_id":billiards,"start":index*60.0,"end":(index+1)*60.0,"audio_mode":"denoise","chapter":"台球"}
                    for index in range(5)
                )
                source_id=db.execute("INSERT INTO exports(project_id,version,format,status,timeline_snapshot,render_options,created_at) VALUES(?,?,?,?,?,?,?)",(project_id,"v18","long_16x9","caption_review_ready",json.dumps({"long":source_timeline}),"{}",stamp))
                body="""16到21分钟的台球镜头太多
可以考虑加入DJI_20260613181757_0057_D.MP4，DJI_20260613181757_0059_D.MP4
还有考虑DJI_20260613181757_0081_D.MP4到DJI_20260613181757_0084_D.MP4的镜头接入。"""
                revision_id=db.execute("INSERT INTO revisions(project_id,kind,body,source_export_id,source_version,created_at) VALUES(?,?,?,?,?,?)",(project_id,"shot",body,source_id,"v18",stamp))
                privacy_id=db.execute("INSERT INTO revisions(project_id,kind,body,source_export_id,source_version,created_at) VALUES(?,?,?,?,?,?)",(project_id,"privacy","22到23分钟紫衣女生需要马赛克",source_id,"v18",stamp))
                result=create_inherited_revision(project_id,source_id,[{"id":revision_id,"kind":"shot","body":body}],revision_ids=[revision_id])
                created=db.row("SELECT * FROM exports WHERE id=?",(result["export_id"],));timeline=json.loads(created["timeline_snapshot"])["long"]
                self.assertAlmostEqual(1260.0,sum(float(item["end"])-float(item["start"]) for item in timeline),places=3)
                asset_ids=[int(item["asset_id"]) for item in timeline]
                self.assertEqual(sorted(asset_ids),asset_ids)
                self.assertTrue(all(asset_id in asset_ids for asset_id in recommended))
                self.assertLess(sum(float(item["end"])-float(item["start"]) for item in timeline if int(item["asset_id"])==billiards),300)
                self.assertEqual([privacy_id],result["carried_privacy_revision_ids"])
                carried=db.row("SELECT source_export_id,source_version,status FROM revisions WHERE id=?",(privacy_id,))
                self.assertEqual(result["export_id"],carried["source_export_id"])
                self.assertEqual(result["version"],carried["source_version"])
                self.assertEqual("open",carried["status"])
            finally:db.DB_PATH=original

    def test_exact_long_cut_then_insert_uses_source_version_timecodes(self):
        with tempfile.TemporaryDirectory() as folder:
            original=db.DB_PATH;db.DB_PATH=Path(folder)/"test.db"
            try:
                db.init_db();stamp=db.now()
                project_id=db.execute("INSERT INTO projects(slug,title,source_dir,status,settings,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",("exact-long-edit","Exact Long Edit","/vlog/inbox/exact-long-edit","caption_review_ready","{}",stamp,stamp))
                db.create_control(project_id,"stopped",None,"人工审核","等待人工审核")
                base_asset=db.execute("INSERT INTO assets(project_id,path,filename,bytes,duration,analysis,created_at) VALUES(?,?,?,?,?,?,?)",(project_id,"/tmp/base.mp4","DJI_TEST_0037_D.MP4",1,300,json.dumps({}),stamp))
                inserted_asset=db.execute("INSERT INTO assets(project_id,path,filename,bytes,duration,analysis,created_at) VALUES(?,?,?,?,?,?,?)",(project_id,"/tmp/food.mp4","DJI_20260613131204_0038_D.MP4",1,590.256,json.dumps({}),stamp))
                source_timeline=[{"asset_id":base_asset,"start":0.0,"end":200.0,"audio_mode":"denoise","background_cleanup":True,"transition":"cut","chapter":"出发"}]
                source_options={"long_audio_policy":"denoise_only_no_bgm_v1","custom_setting":"keep"}
                source_id=db.execute("INSERT INTO exports(project_id,version,format,status,timeline_snapshot,render_options,created_at) VALUES(?,?,?,?,?,?,?)",(project_id,"v19","long_16x9","caption_review_ready",json.dumps({"long":source_timeline}),json.dumps(source_options),stamp))
                revisions=[
                    {"id":1,"kind":"edit","body":"2分44秒到2分59秒，出现了隐私信息，需要剪切掉这段镜头使用的素材"},
                    {"id":2,"kind":"edit","body":"0分43秒后，应该插入DJI_20260613131204_0038_D.MP4的7分06到7分57吃炸鸡的镜头"},
                ]
                result=create_inherited_revision(project_id,source_id,revisions)
                created=db.row("SELECT * FROM exports WHERE id=?",(result["export_id"],));timeline=json.loads(created["timeline_snapshot"])["long"]
                self.assertAlmostEqual(236.0,sum(float(item["end"])-float(item["start"]) for item in timeline),places=3)
                self.assertEqual([base_asset,inserted_asset,base_asset,base_asset],[int(item["asset_id"]) for item in timeline])
                inserted=timeline[1]
                self.assertEqual((426.0,477.0),(inserted["start"],inserted["end"]))
                self.assertTrue(all(item["audio_mode"]=="denoise" for item in timeline))
                options=json.loads(created["render_options"])
                self.assertEqual("denoise_only_no_bgm_v1",options["long_audio_policy"])
                self.assertEqual("keep",options["custom_setting"])
                self.assertEqual("explicit_timecode_delta",options["version_inheritance"]["duration_policy"])
                self.assertEqual(["delete_output_range","insert_source_range"],[value["kind"] for value in result["operations"]])
            finally:db.DB_PATH=original


if __name__=="__main__":unittest.main()

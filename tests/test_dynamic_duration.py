import json
import unittest
from unittest.mock import patch

from app import db,main,render_plan
from _support import IsolatedDbTestCase


class DynamicDurationTest(IsolatedDbTestCase):
    def setUp(self):
        super().setUp()
        self._original_music=render_plan.MUSIC
        self.addCleanup(self._restore_music)
        render_plan.MUSIC=self.test_root/"music"
        render_plan.MUSIC.mkdir(parents=True,exist_ok=True)

    def _restore_music(self):
        render_plan.MUSIC=self._original_music

    def test_short_snapshot_follows_selected_bgm_duration_and_keeps_one_output(self):
        stamp=db.now();slug=f"dynamic-short-{id(self)}"
        project_id=db.execute(
            "INSERT INTO projects(slug,title,source_dir,status,settings,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (slug,"Dynamic Short",f"/vlog/inbox/{slug}","draft_ready","{}",stamp,stamp),
        )
        assets=[]
        for index in range(1,4):
            analysis={"bilingual_captions":[{"start":0,"end":8,"zh":"字幕","ja":"字幕"}]}
            asset_id=db.execute(
                "INSERT INTO assets(project_id,path,filename,bytes,duration,analysis,created_at) VALUES(?,?,?,?,?,?,?)",
                (project_id,f"/vlog/inbox/{slug}/{index}.mp4",f"{index}.mp4",1,120.0,json.dumps(analysis),stamp),
            )
            assets.append(asset_id)
        long_timeline=[
            {"asset_id":asset_id,"start":0.0,"end":100.0,"audio_mode":"dialogue" if position==0 else "montage"}
            for position,asset_id in enumerate(assets)
        ]
        settings={
            "short_bgm":{"filename":"chosen.mp3"},
            "story_plan":{
                "long":{"timeline":long_timeline},
                "shorts":[{"title":"一条短篇","hook":"钩子","voice_mode":"selective_dialogue","voice_reason":"真实对话用于点题","timeline":[{"asset_id":asset_id,"start":0.0,"end":3.0,"audio_mode":"montage"} for asset_id in assets]}],
            },
        }
        music=render_plan.MUSIC/"chosen.mp3";music.write_bytes(b"test")
        try:
            with patch.object(render_plan,"probe",return_value={"duration":75.5}),patch.object(render_plan,"analyze_music_rhythm",return_value=[15.0,45.0]):
                snapshot,options=main._requested_snapshot(project_id,"v2","short_9x16",settings)
        finally:
            music.unlink(missing_ok=True)
        self.assertEqual(["short-1"],list(snapshot))
        self.assertEqual("chosen.mp3",options["bgm_filename"])
        self.assertEqual(75.5,options["bgm_duration"])
        self.assertEqual("selective_dialogue",options["short_voice_mode"])
        self.assertEqual("voice_only",options["caption_policy"])
        self.assertEqual([15.0,45.0],options["short_flash_bursts"])
        timeline=snapshot["short-1"]
        effective=sum(item["end"]-item["start"] for item in timeline)-sum(float(item.get("transition_duration") or 0) for item in timeline[1:] if item.get("transition")!="cut")
        self.assertAlmostEqual(75.5,effective,delta=0.08)
        self.assertGreaterEqual(len({item["asset_id"] for item in timeline}),3)
        self.assertGreaterEqual(len(timeline),35)
        voiced=[item for item in timeline if item["audio_mode"]=="dialogue"]
        self.assertGreaterEqual(len(voiced),1)
        self.assertLessEqual(len(voiced),3)
        self.assertTrue(all(item["show_captions"]==(item["audio_mode"]=="dialogue") for item in timeline))
        self.assertGreaterEqual(sum(1 for item in timeline if item.get("effect")=="flash_frame"),2)


if __name__=="__main__":
    unittest.main()

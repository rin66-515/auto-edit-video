import json
import unittest

from app.story_planner import _effective_short_seconds,manual_short_reselection,timeline_stats


class StructuredShortRevisionTest(unittest.TestCase):
    def test_preserves_clip_count_allows_distinct_same_asset_ranges_and_one_voice(self):
        assets=[]
        codes=[39,69]+list(range(100,128))
        for asset_id,code in enumerate(codes,1):
            assets.append({
                "id":asset_id,"filename":f"DJI_TEST_{code:04d}_D.MP4",
                "duration":600.0,
                "analysis":json.dumps({"visual":{"quality":80-asset_id%7,"story_value":70+asset_id%9}}),
            })
        base=[]
        for index in range(23):
            asset_id=index%13+1;start=float((index//13)*4)
            base.append({"asset_id":asset_id,"start":start,"end":start+2.0,"audio_mode":"mute"})
        revisions=[{"body":"""保持23段镜头，未明确要求不得减少。
推荐加入 DJI_TEST_0039_D.MP4 5分19秒草莓大福。
镜头02替换为 DJI_TEST_0039_D.MP4 20秒后的美味しかった，仅此部分保留原声并削减背景音。
DJI_TEST_0069_D.MP4 鸟居也可以加入。"""}]
        short=manual_short_reselection(revisions,assets,"test",26.149,[13.95,22.1],base)
        timeline=short["timeline"]
        self.assertEqual(23,len(timeline))
        self.assertEqual("dialogue",timeline[1]["audio_mode"])
        self.assertTrue(timeline[1]["background_cleanup"])
        self.assertEqual([2],short["voice_clips"])
        same=[item for item in timeline if item["asset_id"]==1]
        self.assertEqual(2,len(same))
        self.assertGreater(abs(same[0]["start"]-same[1]["start"]),250)
        self.assertAlmostEqual(26.149,_effective_short_seconds(timeline),places=3)
        self.assertEqual([],timeline_stats(timeline,assets)[1])
        manual=short["manual_replan"]
        self.assertEqual(23,manual["actual_clip_count"])
        self.assertEqual("distinct_nonoverlapping_time_ranges",manual["repeat_policy"])


if __name__=="__main__":unittest.main()

import json
import unittest

from app.story_planner import assemble_story_plan,rebuild_short_plans,story_plan_errors,timeline_stats


def asset(asset_id,duration,dialogue=False,story_value=50,quality=70,suggested_use="B-roll",duplicate_hint=False):
    captions=[]
    if dialogue:
        for index in range(12):
            start=10.0+index*20.0
            captions.append({"start":start,"end":start+6.0,"source":f"对话 {asset_id}-{index}","zh":"测试字幕","ja":"テスト字幕"})
    analysis={"visual":{"story_value":story_value,"quality":quality,"suggested_use":suggested_use,"duplicate_hint":duplicate_hint},"bilingual_captions":captions}
    return {"id":asset_id,"duration":duration,"analysis":json.dumps(analysis,ensure_ascii=False)}


class StoryPlannerTest(unittest.TestCase):
    def setUp(self):
        self.assets=[asset(index,300.0,dialogue=index in {4,8,12},story_value=40+index) for index in range(1,13)]
        self.context="人工意见：长篇偏日系，最后一段与朋友讨论中日数字手势，要作为故事锚点和短篇网感镜头。"

    def assert_valid_outputs(self,plan):
        self.assertEqual([],story_plan_errors(plan,self.assets))
        long_seconds,long_errors=timeline_stats(plan["long"]["timeline"],self.assets)
        self.assertEqual([],long_errors)
        self.assertGreaterEqual(long_seconds,600)
        self.assertLessEqual(long_seconds,3600)
        self.assertAlmostEqual(plan["long"]["target_seconds"],long_seconds,delta=0.25)
        self.assertNotAlmostEqual(1800.0,long_seconds,delta=0.25)
        self.assertEqual(1,len(plan["shorts"]))
        for short in plan["shorts"]:
            seconds,errors=timeline_stats(short["timeline"],self.assets)
            self.assertEqual([],errors)
            self.assertGreaterEqual(seconds,10)
            self.assertLessEqual(seconds,600)
            self.assertGreaterEqual(len(short["timeline"]),8)
            first=short["timeline"][0]
            self.assertLessEqual(first["end"]-first["start"],3)
            self.assertEqual("douyin_polished_v2",short["editorial_style"])
            hard_cuts=sum(1 for item in short["timeline"][1:] if item.get("transition")=="cut")
            self.assertGreaterEqual(hard_cuts,len(short["timeline"][1:])//2)
        self.assertEqual(1,len({short["style_profile"] for short in plan["shorts"]}))

    def test_compact_intent_builds_valid_non_overlapping_timelines(self):
        intent={
            "title":"手势不同，友情相同",
            "story_anchor":{"topic":"中日数字手势","asset_ids":[12]},
            "chapters":[{"name":"出发"},{"name":"相聚"},{"name":"手势讨论"}],
            "asset_priorities":[{"asset_id":12,"priority":100,"chapter":"手势讨论"}],
            "shorts":[{"title":"中日数字手势","hook":"数字手势真的一样吗？","asset_ids":[]}],
        }
        plan=assemble_story_plan(intent,self.assets,self.context)
        self.assert_valid_outputs(plan)
        self.assertEqual(12,plan["long"]["story_anchor"]["asset_ids"][0])
        self.assertTrue(any(item["asset_id"]==12 for item in plan["shorts"][0]["timeline"]))

    def test_unparseable_ai_intent_falls_back_without_retry(self):
        plan=assemble_story_plan({},self.assets,self.context,used_fallback=True)
        self.assert_valid_outputs(plan)
        self.assertEqual("compact_ai_plus_deterministic_timeline_v5_quality_first",plan["generation_method"])
        self.assertTrue(any("确定性故事结构回退" in value for value in plan["review_warnings"]))
        self.assertIn("手势",plan["shorts"][0]["title"])

    def test_human_long_duration_range_is_a_hard_constraint(self):
        intent={
            "long_target_minutes":12,
            "long_duration_constraint":{"min_minutes":40,"max_minutes":50,"preferred_minutes":45},
            "shorts":[{"title":"独立短篇","hook":"发生了什么？","asset_ids":[1,2,3]}],
        }
        plan=assemble_story_plan(intent,self.assets,self.context)
        seconds,_=timeline_stats(plan["long"]["timeline"],self.assets)
        self.assertAlmostEqual(40*60,seconds,delta=0.25)
        asset_ids=[item["asset_id"] for item in plan["long"]["timeline"]]
        self.assertEqual(sorted(asset_ids),asset_ids)
        self.assertLess(plan["long"]["selected_asset_count"],len(self.assets))

    def test_quality_first_skips_rejected_asset_when_good_material_is_sufficient(self):
        assets=[
            asset(1,360,story_value=90,quality=90),
            asset(2,360,story_value=85,quality=85),
            asset(3,360,story_value=80,quality=85),
            asset(4,360,story_value=75,quality=80),
            asset(5,360,story_value=10,quality=15,suggested_use="弃用"),
        ]
        intent={
            "long_duration_constraint":{"min_minutes":10,"max_minutes":12,"preferred_minutes":12},
            "shorts":[{"title":"独立短篇","hook":"发生了什么？","asset_ids":[1,2,3]}],
        }
        plan=assemble_story_plan(intent,assets,"")
        selected=set(plan["long"]["selected_asset_ids"])
        self.assertNotIn(5,selected)
        self.assertEqual(0,plan["long"]["fallback_asset_count"])

    def test_quality_first_adds_fallback_only_until_minimum_is_reached(self):
        assets=[
            asset(1,250,story_value=90,quality=90),
            asset(2,250,story_value=85,quality=85),
            asset(3,300,story_value=30,quality=35),
        ]
        intent={
            "long_duration_constraint":{"min_minutes":10,"max_minutes":12,"preferred_minutes":12},
            "shorts":[{"title":"独立短篇","hook":"发生了什么？","asset_ids":[1,2,3]}],
        }
        plan=assemble_story_plan(intent,assets,"")
        self.assertAlmostEqual(600,plan["long"]["target_seconds"],delta=0.25)
        self.assertIn(3,plan["long"]["selected_asset_ids"])
        self.assertEqual(1,plan["long"]["fallback_asset_count"])

    def test_short_bgm_rebuild_uses_assets_missing_from_long_timeline(self):
        assets=[asset(1,30.0),asset(2,30.0)]
        existing=[{"title":"独立短篇","hook":"素材二是什么？","timeline":[{"asset_id":2,"start":0,"end":10,"audio_mode":"mute"}]}]
        long_timeline=[{"asset_id":1,"start":0,"end":20,"audio_mode":"montage"}]
        rebuilt=rebuild_short_plans(existing,long_timeline,assets,"independent",target_seconds=12,bgm_led=True)
        self.assertTrue(any(item["asset_id"]==2 for item in rebuilt[0]["timeline"]))


if __name__=="__main__":unittest.main()

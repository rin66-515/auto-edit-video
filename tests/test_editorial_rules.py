import unittest

from app.editorial_rules import (
    editorial_overrides,
    inherit_long_replan_defaults,
    merge_scoped_story_plan,
    parse_long_replan_directives,
    scoped_rules,
)


class EditorialRulesTest(unittest.TestCase):
    def test_parses_explicit_long_replan_as_hard_directives(self):
        directives=parse_long_replan_directives([{
            "kind":"full_replan",
            "body":"重新审核48部原素材，生成新的长篇，希望能有40-50分钟长度。仅降噪，重新校对人声与字幕以及时间线。",
        }])
        self.assertEqual(40.0,directives["duration_min_minutes"])
        self.assertEqual(50.0,directives["duration_max_minutes"])
        self.assertEqual(45.0,directives["duration_preferred_minutes"])
        self.assertTrue(directives["reanalyze_assets"])
        self.assertTrue(directives["reproofread_captions"])
        self.assertEqual("denoise_only",directives["audio_policy"])

    def test_scoped_user_rules_do_not_cross_formats(self):
        settings={"editorial_rules":{"common":"共同保留事实","long_16x9":"长篇人工规则","short_9x16":"短篇人工规则"}}
        long=scoped_rules("long_16x9",settings)
        short=scoped_rules("short_9x16",settings)
        self.assertIn("共同保留事实",long)
        self.assertIn("共同保留事实",short)
        self.assertIn("长篇人工规则",long)
        self.assertNotIn("短篇人工规则",long)
        self.assertIn("短篇人工规则",short)
        self.assertNotIn("长篇人工规则",short)
        self.assertEqual("共同保留事实",editorial_overrides(settings)["common"])

    def test_scoped_merge_preserves_other_format_verbatim(self):
        old_long={"timeline":[{"asset_id":1,"start":0,"end":10,"audio_mode":"dialogue"}]}
        old_short={"timeline":[{"asset_id":2,"start":0,"end":2,"audio_mode":"mute"}]}
        existing={"long":old_long,"shorts":[old_short],"short_style_seed":"old"}
        new_long={"title":"新长篇","summary":"只改长篇","long":{"timeline":[{"asset_id":3,"start":0,"end":12,"audio_mode":"denoise"}]}}
        merged_long=merge_scoped_story_plan(existing,new_long,"long_16x9")
        self.assertEqual([old_short],merged_long["shorts"])
        self.assertEqual("old",merged_long["short_style_seed"])
        self.assertEqual(3,merged_long["long"]["timeline"][0]["asset_id"])

        new_short={"title":"新短篇","shorts":[{"timeline":[{"asset_id":4,"start":0,"end":1,"audio_mode":"mute"}]}],"short_style_seed":"new"}
        merged_short=merge_scoped_story_plan(existing,new_short,"short_9x16")
        self.assertEqual(old_long,merged_short["long"])
        self.assertEqual(4,merged_short["shorts"][0]["timeline"][0]["asset_id"])

    def test_full_replan_inherits_source_duration_and_audio_when_unspecified(self):
        directives=parse_long_replan_directives([{"kind":"full_replan","body":"重新构思整条长篇故事"}])
        inherited=inherit_long_replan_defaults(
            directives,
            {"long":[{"asset_id":1,"start":0.0,"end":2429.217,"audio_mode":"denoise"}]},
            {"long_audio_policy":"denoise_only_no_bgm_v1"},
        )
        self.assertEqual(40.487,inherited["duration_preferred_minutes"])
        self.assertEqual("denoise_only",inherited["audio_policy"])
        self.assertTrue(inherited["duration_inherited"])
        self.assertTrue(inherited["audio_policy_inherited"])


if __name__=="__main__":unittest.main()

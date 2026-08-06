import unittest

from app.revision_intent import parse_audio_revision,parse_revision_intent,revision_mode


class RevisionIntentTest(unittest.TestCase):
    def test_parses_recommendations_times_clip_count_and_single_voice(self):
        body = """保持23段镜头，没有要求时不得删减。
推荐加入 DJI_TEST_0039_D.MP4 5分19秒的草莓大福。
镜头02替换为 DJI_TEST_0039_D.MP4 20秒后的美味しかった，仅此部分保留原声，削减背景音；其余短篇镜头关闭原声。
DJI_TEST_0069_D.MP4 的鸟居也可以加入。"""
        intent = parse_revision_intent([{"body": body}], 23)
        self.assertEqual(23, intent["target_clip_count"])
        self.assertTrue(intent["preserve_clip_count"])
        self.assertTrue(intent["only_specified_dialogue"])
        self.assertEqual(3, len(intent["recommendations"]))
        strawberry, voice, torii = intent["recommendations"]
        self.assertEqual(319.0, strawberry["time_seconds"])
        self.assertEqual(2, voice["target_clip_index"])
        self.assertEqual("dialogue", voice["audio_mode"])
        self.assertTrue(voice["background_cleanup"])
        self.assertEqual("preferred", torii["priority"])

    def test_bgm_first_ducking_word_order(self):
        intent=parse_revision_intent([{"body":"镜头02替换为 DJI_TEST_0039_D.MP4 20秒后，保留人声并让BGM自动压低。"}],23)
        self.assertTrue(intent["recommendations"][0]["duck_bgm"])

    def test_negated_reduction_keeps_source_clip_count(self):
        intent=parse_revision_intent([{"body":"没有提到要删减镜头时，不要擅自删减镜头。"}],23)
        self.assertTrue(intent["preserve_clip_count"])
        self.assertEqual(23,intent["target_clip_count"])

    def test_supports_colon_and_minute_without_seconds_suffix(self):
        intent = parse_revision_intent([{"body": """推荐 DJI_TEST_0038_D.MP4 7分04拿到炸鸡
推荐 DJI_TEST_0078_D.MP4 08:50 山全景"""}], 23)
        self.assertEqual([424.0, 530.0], [item["time_seconds"] for item in intent["recommendations"]])

    def test_version_feedback_is_incremental_unless_full_replan_kind_is_selected(self):
        self.assertEqual("incremental",revision_mode([{"kind":"edit","body":"重新调整第二秒的BGM"}]))
        self.assertEqual("full_replan",revision_mode([{"kind":"full_replan","body":"重新选材并整体重剪"}]))

    def test_audio_feedback_parses_dialogue_onset_after_second_two(self):
        audio=parse_audio_revision([{"kind":"audio","body":"第二秒多播放一些BGM，到2后最后几帧，出现人说话才主要捕捉人声，减小BGM。"}])
        self.assertTrue(audio["matched"])
        self.assertEqual(2.0,audio["requested_after_seconds"])
        self.assertTrue(audio["duck_on_dialogue_onset"])
        self.assertTrue(audio["reduce_bgm"])

    def test_parses_long_output_range_and_expands_asset_code_range(self):
        body="""16到21分钟的台球镜头太多
可以考虑加入DJI_20260613181757_0057_D.MP4，DJI_20260613181757_0059_D.MP4
还有考虑DJI_20260613181757_0081_D.MP4到DJI_20260613181757_0084_D.MP4的镜头接入。"""
        intent=parse_revision_intent([{"kind":"shot","body":body}])
        self.assertEqual([{"start_seconds":960.0,"end_seconds":1260.0,"action":"rebalance"}],intent["output_ranges"])
        self.assertEqual(["0057","0059","0081","0082","0083","0084"],[value["asset_code"] for value in intent["recommendations"]])
        self.assertEqual("dominant_asset_first",intent["deletion_preference"])

    def test_parses_exact_output_cut_and_source_range_insertion_separately(self):
        body="""2分44秒到2分59秒，出现了隐私信息，需要剪切掉这段镜头使用的素材
0分43秒后，应该插入DJI_20260613131204_0038_D.MP4的7分06到7分57吃炸鸡的镜头"""
        intent=parse_revision_intent([{"kind":"edit","body":body}])
        self.assertEqual([{"start_seconds":164.0,"end_seconds":179.0,"action":"cut","label":"2分44秒到2分59秒，出现了隐私信息，需要剪切掉这段镜头使用的素材"}],intent["output_deletions"])
        self.assertEqual(1,len(intent["insertions"]))
        insertion=intent["insertions"][0]
        self.assertEqual(43.0,insertion["output_at_seconds"])
        self.assertEqual((426.0,477.0),(insertion["source_start_seconds"],insertion["source_end_seconds"]))
        self.assertEqual("0038",insertion["asset_code"])
        self.assertEqual([],intent["recommendations"])
        self.assertTrue(intent["has_local_timeline_edits"])

    def test_full_replan_with_exact_timecodes_is_marked_unsafe(self):
        intent=parse_revision_intent([{"kind":"full_replan","body":"2:44到2:59切掉"}])
        self.assertTrue(intent["has_local_timeline_edits"])
        self.assertTrue(any("完整重规划将被拒绝" in value for value in intent["warnings"]))


if __name__=="__main__":unittest.main()

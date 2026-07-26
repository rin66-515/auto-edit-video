import unittest

from app.revision_intent import parse_revision_intent


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


if __name__=="__main__":unittest.main()

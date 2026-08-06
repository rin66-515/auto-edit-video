import unittest

from app.privacy_intent import parse_privacy_intent


class PrivacyIntentTest(unittest.TestCase):
    def test_parses_output_range_region_and_frame_continuity(self):
        intent=parse_privacy_intent([{"kind":"privacy","body":"22.1秒到22.8秒，右侧人物加马赛克，提前3帧，延后4帧"}])
        rule=intent["privacy_rules"]["force_cover"][0]
        self.assertEqual((22.1,22.8),(rule["start"],rule["end"]))
        self.assertEqual((0.44,1.0),(rule["x_min"],rule["x_max"]))
        self.assertEqual((3,4),(rule["lead_frames"],rule["tail_frames"]))

    def test_missing_instruction_keeps_original_picture(self):
        intent=parse_privacy_intent([{"kind":"privacy","body":"画面很好，不需要调整"}])
        self.assertEqual([],intent["privacy_rules"]["force_cover"])
        self.assertIn("不运行自动人脸遮挡",intent["summary"][-1])

    def test_remove_mosaic_becomes_suppress_rule(self):
        intent=parse_privacy_intent([{"kind":"privacy","body":"1:02到1:04 左侧人物去掉马赛克"}])
        self.assertEqual([],intent["privacy_rules"]["force_cover"])
        self.assertEqual((62.0,64.0),(intent["privacy_rules"]["suppress"][0]["start"],intent["privacy_rules"]["suppress"][0]["end"]))


    def test_same_minute_shorthand_and_before_range(self):
        tracked=parse_privacy_intent([{"kind":"privacy","body":"4分44秒的右侧人脸加马赛克，一直追踪到47秒"}])
        rule=tracked["force_cover"][0];self.assertEqual((284.0,287.0),(rule["start"],rule["end"]))
        before=parse_privacy_intent([{"kind":"privacy","body":"4分40秒前的马赛克全部去掉"}])
        self.assertEqual((0.0,280.0),(before["suppress"][0]["start"],before["suppress"][0]["end"]))

if __name__=="__main__":unittest.main()

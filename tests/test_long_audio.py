import json
import unittest

from app.render_plan import denoise_long_audio_timeline,refine_long_audio_timeline


class LongAudioTimelineTest(unittest.TestCase):
    def test_splits_speech_exactly_and_keeps_only_short_ambient_windows(self):
        assets=[
            {
                "id":1,
                "analysis":json.dumps({
                    "audio_cleanup":{"engine":"FFmpeg fallback","status":"warning"},
                    "bilingual_captions":[
                        {"start":2.0,"end":3.0,"zh":"第一句","ja":"一"},
                        {"start":6.0,"end":7.0,"zh":"第二句","ja":"二"},
                    ],
                }),
            },
            {"id":2,"analysis":json.dumps({"audio_cleanup":{"engine":"DeepFilterNet3","status":"enhanced"},"bilingual_captions":[]})},
            {"id":3,"analysis":json.dumps({"audio_cleanup":{"engine":"DeepFilterNet3","status":"enhanced"},"bilingual_captions":[]})},
        ]
        timeline=[
            {"asset_id":1,"start":0.0,"end":10.0,"audio_mode":"dialogue","chapter":"对白"},
            {"asset_id":2,"start":0.0,"end":20.0,"audio_mode":"ambient","chapter":"现场"},
            {"asset_id":3,"start":0.0,"end":5.0,"audio_mode":"montage","chapter":"过场"},
        ]

        refined=refine_long_audio_timeline(timeline,assets)

        self.assertAlmostEqual(35.0,sum(item["end"]-item["start"] for item in refined),places=3)
        dialogue=[item for item in refined if item["audio_mode"]=="dialogue"]
        self.assertEqual(2,len(dialogue))
        self.assertTrue(all(item["background_cleanup"] for item in dialogue))
        self.assertAlmostEqual(1.82,dialogue[0]["start"],places=2)
        self.assertAlmostEqual(3.25,dialogue[0]["end"],places=2)
        ambient=[item for item in refined if item["audio_mode"]=="ambient"]
        self.assertEqual(1,len(ambient))
        self.assertAlmostEqual(12.0,ambient[0]["end"]-ambient[0]["start"],places=3)
        self.assertFalse(any(item["audio_mode"]=="montage" for item in refined))

    def test_enhanced_audio_uses_standard_dialogue_cleanup(self):
        assets=[{"id":1,"analysis":{"audio_cleanup":{"status":"enhanced"},"bilingual_captions":[{"start":1.0,"end":2.0}]}}]
        refined=refine_long_audio_timeline([{"asset_id":1,"start":0.0,"end":3.0,"audio_mode":"dialogue"}],assets)
        dialogue=next(item for item in refined if item["audio_mode"]=="dialogue")
        self.assertFalse(dialogue["background_cleanup"])

    def test_denoise_only_preserves_whole_selected_timeline_without_muting(self):
        source=[
            {"asset_id":1,"start":0.0,"end":20.0,"audio_mode":"dialogue"},
            {"asset_id":2,"start":5.0,"end":17.0,"audio_mode":"ambient"},
        ]
        refined=denoise_long_audio_timeline(source)
        self.assertAlmostEqual(32.0,sum(item["end"]-item["start"] for item in refined))
        self.assertEqual(["denoise","denoise"],[item["audio_mode"] for item in refined])
        self.assertTrue(all(item["background_cleanup"] for item in refined))


if __name__=="__main__":unittest.main()

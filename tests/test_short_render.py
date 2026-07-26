import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.media import _merge_short_segments,_music_mix_filter,_segment_audio_filter,_short_transition_overlap,_write_bilingual_srt


class ShortRenderTest(unittest.TestCase):
    def test_short_music_is_bgm_led_and_ducks_under_selected_voice(self):
        short_filter=_music_mix_filter("short_9x16",26.149)
        self.assertIn("volume=0.26",short_filter)
        self.assertIn("sidechaincompress",short_filter)
        self.assertIn("apad=whole_dur=26.149",short_filter)
        self.assertIn("atrim=duration=26.149",short_filter)
        long_filter=_music_mix_filter("long_16x9")
        self.assertIn("volume=0.08",long_filter)
        self.assertNotIn("sidechaincompress",long_filter)

    def test_selected_voice_can_request_stronger_background_cleanup(self):
        cleaned=_segment_audio_filter({"audio_mode":"dialogue","background_cleanup":True})
        normal=_segment_audio_filter({"audio_mode":"dialogue"})
        self.assertIn("afftdn=nf=-32",cleaned)
        self.assertIn("lowpass=f=10500",cleaned)
        self.assertNotEqual(normal,cleaned)
        self.assertEqual("volume=0",_segment_audio_filter({"audio_mode":"mute"}))

    def test_short_bgm_continues_after_a_shorter_original_audio_track(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);primary=root/"primary.mp4";music=root/"music.m4a";target=root/"mixed.mp4"
            subprocess.run(["ffmpeg","-loglevel","error","-y","-f","lavfi","-i","color=c=black:s=96x160:r=30:d=3","-f","lavfi","-i","sine=frequency=440:sample_rate=48000:duration=1","-map","0:v","-map","1:a","-t","3","-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac",str(primary)],check=True)
            subprocess.run(["ffmpeg","-loglevel","error","-y","-f","lavfi","-i","sine=frequency=880:sample_rate=48000:duration=3","-c:a","aac",str(music)],check=True)
            subprocess.run(["ffmpeg","-loglevel","error","-y","-i",str(primary),"-stream_loop","-1","-i",str(music),"-filter_complex",_music_mix_filter("short_9x16",3),"-map","0:v","-map","[a]","-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac","-shortest",str(target)],check=True)
            probe=subprocess.run(["ffprobe","-v","error","-show_entries","stream=codec_type,duration","-of","json",str(target)],check=True,capture_output=True,text=True)
            durations={stream["codec_type"]:float(stream["duration"]) for stream in json.loads(probe.stdout)["streams"]}
            self.assertAlmostEqual(3.0,durations["audio"],delta=0.08)
            self.assertAlmostEqual(3.0,durations["video"],delta=0.08)

    def test_short_caption_file_skips_non_voice_clips(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/"captions.srt"
            item={"asset_id":1,"start":0.0,"end":2.0,"show_captions":False}
            asset={"id":1,"analysis":json.dumps({"bilingual_captions":[{"start":0.0,"end":2.0,"zh":"不应显示","ja":"表示しない"}]})}
            self.assertFalse(_write_bilingual_srt([(item,asset,2.0,1)],target,"short_9x16"))
            self.assertFalse(target.exists())

    def test_mixed_hard_cut_and_xfade_render_and_timing(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);segments=[]
            for index,color in enumerate(("red","green","blue")):
                target=root/f"segment-{index}.mp4"
                subprocess.run(["ffmpeg","-loglevel","error","-y","-f","lavfi","-i",f"color=c={color}:s=96x160:r=30:d=0.8","-f","lavfi","-i","sine=frequency=440:sample_rate=48000:duration=0.8","-shortest","-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac",str(target)],check=True)
                segments.append(target)
            used=[({"transition":"cut","transition_duration":0},None,0.8,1),({"transition":"cut","transition_duration":0.4},None,0.8,2),({"transition":"fade","transition_duration":0.2},None,0.8,3)]
            merged=root/"merged.mp4";_merge_short_segments(segments,used,merged,root)
            probe=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","json",str(merged)],check=True,capture_output=True,text=True)
            duration=float(json.loads(probe.stdout)["format"]["duration"])
            failure_log=(root/"short-merge.ffmpeg.log").read_text(encoding="utf-8") if (root/"short-merge.ffmpeg.log").exists() else ""
            self.assertAlmostEqual(2.2,duration,delta=0.12,msg=failure_log)
            self.assertEqual(0.0,_short_transition_overlap(used[1][0]))
            self.assertAlmostEqual(0.2,_short_transition_overlap(used[2][0]))


if __name__=="__main__":unittest.main()

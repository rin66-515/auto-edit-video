import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.media import _concat_segments,apply_text_overlays


class TextOverlayTest(unittest.TestCase):
    def make_clip(self,target,duration,color="navy"):
        subprocess.run(["ffmpeg","-loglevel","error","-y","-f","lavfi","-i",f"color=c={color}:s=320x180:r=30:d={duration}","-f","lavfi","-i",f"sine=frequency=440:sample_rate=48000:duration={duration}","-shortest","-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac",str(target)],check=True)

    def duration(self,path):
        result=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","json",str(path)],check=True,capture_output=True,text=True)
        return float(json.loads(result.stdout)["format"]["duration"])

    def test_expected_duration_trims_legacy_concat_padding(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);first=root/"first.mp4";second=root/"second.mp4";merged=root/"merged.mp4"
            self.make_clip(first,0.61,"red");self.make_clip(second,0.73,"blue");_concat_segments([first,second],merged,root,1.34)
            self.assertLessEqual(self.duration(merged),1.40)

    def test_vertical_japanese_fade_overlay_renders(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/"overlay.mp4";self.make_clip(target,2.0)
            result=apply_text_overlays(target,[{"text":"バス遅い...","start":0.4,"end":1.6,"vertical":True,"fade_seconds":0.25,"font_size":32,"right_margin":30}])
            self.assertEqual(1,result["applied"]);self.assertTrue(target.is_file());self.assertAlmostEqual(2.0,self.duration(target),delta=0.12)
            self.assertEqual([],list(target.parent.glob(".*.overlay-*.txt")))


if __name__=="__main__":unittest.main()

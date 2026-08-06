import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app import privacy
from app.privacy import TRACK_LEAD_FRAMES,TRACK_TAIL_FRAMES,_identity_reference_files,_pixelate_face,_plausible_face,_primary_face,_rule_matches,_track_covers_frame,_track_is_live,privacy_enabled


class PrivacyOverlayTest(unittest.TestCase):
    def test_normal_mosaic_pixelates_face_area_and_keeps_outside_pixels(self):
        y,x=np.indices((120,120));frame=np.stack((x,y,(x+y)%255),axis=-1).astype(np.uint8);before=frame.copy()
        _pixelate_face(frame,np.array([40,35,32,38],dtype=np.float32))
        self.assertTrue(np.array_equal(frame[0,0],before[0,0]))
        self.assertFalse(np.array_equal(frame[60,60],before[60,60]))
        self.assertLess(len(np.unique(frame[30:95,30:95].reshape(-1,3),axis=0)),len(np.unique(before[30:95,30:95].reshape(-1,3),axis=0)))
        _pixelate_face(frame,np.array([-8,-12,40,44],dtype=np.float32))
        self.assertEqual((120,120,3),frame.shape)

    def test_privacy_is_manual_only_by_default(self):
        self.assertFalse(privacy_enabled({}));self.assertTrue(privacy_enabled({}, {"force_cover":[{"start":1,"end":2}]}))
    def test_static_track_is_suppressed_and_live_track_has_lead_tail(self):
        track={"owner":False,"owner_hits":0,"seen":8,"max_motion":0.2,"confirmed":False,"start":10,"last":20}
        self.assertFalse(_track_is_live(track))
        track["max_motion"]=9.0;self.assertTrue(_track_is_live(track));track["confirmed"]=True
        self.assertTrue(_track_covers_frame(track,10-TRACK_LEAD_FRAMES));self.assertTrue(_track_covers_frame(track,20+TRACK_TAIL_FRAMES));self.assertFalse(_track_covers_frame(track,9-TRACK_LEAD_FRAMES));self.assertFalse(_track_covers_frame(track,21+TRACK_TAIL_FRAMES))

    def test_owner_track_is_never_covered(self):
        track={"owner":True,"owner_hits":2,"seen":20,"max_motion":20.0,"confirmed":True,"start":0,"last":30}
        self.assertFalse(_track_is_live(track));self.assertFalse(_track_covers_frame(track,10))

    def test_face_geometry_rejects_object_false_positive(self):
        valid=np.array([10,10,40,48,20,24,39,24,30,34,22,45,38,45,0.91],dtype=np.float32)
        hand_like=np.array([10,10,40,48,12,13,48,49,14,12,47,14,11,48,0.91],dtype=np.float32)
        low_score=valid.copy();low_score[14]=0.58
        self.assertTrue(_plausible_face(valid));self.assertFalse(_plausible_face(hand_like));self.assertFalse(_plausible_face(low_score))

    def test_manual_region_and_custom_continuity(self):
        face=np.array([70,20,20,24],dtype=np.float32);rule={"start":10,"end":12,"x_min":0.6,"x_max":1.0}
        self.assertTrue(_rule_matches(rule,11,face,100,100));self.assertFalse(_rule_matches(rule,9.9,face,100,100))
        track={"owner":False,"seen":1,"max_motion":0.0,"manual_cover":True,"confirmed":True,"start":100,"last":110,"lead_frames":30,"tail_frames":45}
        self.assertTrue(_track_is_live(track));self.assertTrue(_track_covers_frame(track,70));self.assertTrue(_track_covers_frame(track,155));self.assertFalse(_track_covers_frame(track,156))

    def test_distant_background_face_is_not_a_privacy_target(self):
        self.assertFalse(_primary_face(np.array([100,50,20,30],dtype=np.float32),1920,1080))
        self.assertTrue(_primary_face(np.array([100,50,90,120],dtype=np.float32),1920,1080))

    def test_side_face_references_are_loaded_as_owner_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ("reference-syain-profile-a.jpg","profile-extra.png","scene-left-gesture.webp","ignore.txt"):
                (root/name).write_bytes(b"x")
            with patch.object(privacy,"OWNER_IDENTITY",root):
                names=[path.name for path in _identity_reference_files()]
        self.assertEqual(["profile-extra.png","reference-syain-profile-a.jpg","scene-left-gesture.webp"],names)


if __name__=="__main__":unittest.main()

import unittest

from app.media import _last_black_end,rendered_time_to_timeline,shift_interval_after_cuts,trim_timeline
from app.manual_revision import shift_privacy_rules


class TimelineTrimTest(unittest.TestCase):
    def test_cut_splits_items_and_preserves_source_time(self):
        timeline=[{"asset_id":1,"start":100.0,"end":110.0},{"asset_id":2,"start":20.0,"end":30.0}]
        trimmed=trim_timeline(timeline,[(4,7),(12,15)],end_at=18)
        self.assertEqual([(100.0,104.0),(107.0,110.0),(20.0,22.0),(25.0,28.0)],[(x["start"],x["end"]) for x in trimmed])
        self.assertAlmostEqual(12.0,sum(x["end"]-x["start"] for x in trimmed))

    def test_manual_rule_time_is_shifted_after_cuts(self):
        self.assertEqual([(8.0,14.0)],shift_interval_after_cuts(10,17,[(2,4),(12,13)]))
        self.assertEqual([],shift_interval_after_cuts(12.1,12.9,[(12,13)]))

    def test_privacy_rule_regions_survive_time_shift(self):
        shifted=shift_privacy_rules({"suppress":[{"start":10,"end":17,"x_max":0.5,"label":"owner"}],"force_owner":[{"start":10,"end":17,"x_min":0.6,"label":"profile"}]},[(2,4),(12,13)])
        self.assertEqual([{"start":8.0,"end":14.0,"x_max":0.5,"label":"owner"}],shifted["suppress"])
        self.assertEqual([{"start":8.0,"end":14.0,"x_min":0.6,"label":"profile"}],shifted["force_owner"])

    def test_last_black_interval_is_selected(self):
        log="black_start:1.0 black_end:1.1 black_duration:0.1\nblack_start:3.2 black_end:4.1 black_duration:0.9"
        self.assertEqual((3.2,4.1,0.9),_last_black_end(log))

    def test_legacy_rendered_time_maps_per_segment_not_by_global_ratio(self):
        timeline=[{"start":0,"end":1.01},{"start":10,"end":12.02}]
        rounded_first=31/30;rounded_total=(31+61)/30;actual_duration=rounded_total*1.01
        self.assertAlmostEqual(1.01,rendered_time_to_timeline(timeline,rounded_first*1.01,actual_duration),places=5)
        self.assertAlmostEqual(3.03,rendered_time_to_timeline(timeline,actual_duration,actual_duration),places=5)


if __name__=="__main__":unittest.main()

"""표시 시간대(PWIKI_TZ). 비우면 KST 그대로, 값을 주면 날짜 경계·표시만 바뀐다."""
import os
import unittest
from unittest import mock

from pwikilib import paths

TS = "2026-10-02T00:05:00.000Z"   # KST 09:05, 미국 서부 전날 17:05


def env(v):
    return mock.patch.dict(os.environ, {"PWIKI_TZ": v} if v is not None else {}, clear=False)


class TimezoneTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("PWIKI_TZ", None)

    def tearDown(self):
        os.environ.pop("PWIKI_TZ", None)
        if self._saved is not None:
            os.environ["PWIKI_TZ"] = self._saved

    def test_default_is_kst(self):
        self.assertEqual(paths.tz_label(), "KST")
        self.assertEqual(paths.kst_str(TS), "2026-10-02 09:05")
        self.assertEqual(paths.kst_str_z(TS), "2026-10-02 09:05 KST")
        self.assertEqual(paths.kst_day_bounds_utc("2026-10-02"),
                         ("2026-10-01T15:00:00.000Z", "2026-10-02T15:00:00.000Z"))

    def test_explicit_kst_same_as_default(self):
        with env("KST"):
            self.assertEqual(paths.kst_str_z(TS), "2026-10-02 09:05 KST")

    def test_utc_and_offsets(self):
        with env("UTC"):
            self.assertEqual(paths.kst_str_z(TS), "2026-10-02 00:05 UTC")
            self.assertEqual(paths.kst_day_bounds_utc("2026-10-02")[0], "2026-10-02T00:00:00.000Z")
        with env("+05:30"):
            self.assertEqual(paths.kst_str_z(TS), "2026-10-02 05:35 UTC+05:30")
        with env("UTC-8"):
            self.assertEqual(paths.kst_str_z(TS), "2026-10-01 16:05 UTC-08:00")
            self.assertEqual(paths.kst_date(TS), "2026-10-01")

    def test_iana_zone_and_dst_day(self):
        with env("America/Los_Angeles"):
            self.assertEqual(paths.kst_str_z(TS), "2026-10-01 17:05 PDT")
            lo, hi = paths.kst_day_bounds_utc("2026-11-01")   # 서머타임 끝: 25시간
            self.assertEqual((lo, hi), ("2026-11-01T07:00:00.000Z", "2026-11-02T08:00:00.000Z"))

    def test_label_follows_each_timestamp(self):
        with env("America/New_York"):
            self.assertEqual(paths.kst_str_z("2026-01-15T00:00:00.000Z"), "2026-01-14 19:00 EST")
            self.assertEqual(paths.kst_str_z("2026-07-15T00:00:00.000Z"), "2026-07-14 20:00 EDT")
            self.assertEqual(paths.tz_label_for_date("2026-01-14"), "EST")
            self.assertEqual(paths.tz_label_for_date("2026-07-14"), "EDT")
        with env("+05:30"):
            self.assertEqual(paths.tz_label_for_date("2026-01-14"), "UTC+05:30")
        self.assertEqual(paths.tz_label_for_date("2026-01-14"), "KST")

    def test_bad_value_falls_back_to_kst(self):
        for v in ("nonsense/zone", "+99:00", "abc"):
            with env(v):
                self.assertEqual(paths.tz_label(), "KST", v)
                self.assertEqual(paths.kst_str(TS), "2026-10-02 09:05", v)

    def test_local_uses_system_zone(self):
        with env("local"):
            label = paths.tz_label()
            self.assertTrue(label)
            self.assertIsNone(paths.display_tz()[0])
            self.assertRegex(paths.kst_str(TS), r"^2026-10-0[12] \d\d:\d\d$")


if __name__ == "__main__":
    unittest.main()

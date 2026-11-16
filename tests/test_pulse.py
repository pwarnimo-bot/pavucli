import unittest

from pavucli import pulse
from pavucli.pulse import VOLUME_NORM as N
from pavucli.ui import channel_label, swidth, trunc

STEREO = ["front-left", "front-right"]
SURROUND = ["front-left", "front-right", "front-center", "lfe", "rear-left", "rear-right"]


class BalanceTest(unittest.TestCase):
    def test_centered(self):
        self.assertEqual(pulse.get_balance(STEREO, [N, N]), 0.0)

    def test_left_and_right(self):
        self.assertAlmostEqual(pulse.get_balance(STEREO, [N, N // 2]), -0.5, places=3)
        self.assertAlmostEqual(pulse.get_balance(STEREO, [0, N]), 1.0)

    def test_mono_has_no_balance(self):
        self.assertIsNone(pulse.get_balance(["mono"], [N]))
        self.assertIsNone(pulse.set_balance(["mono"], [N], 0.5) and None)

    def test_aux_stereo(self):
        self.assertAlmostEqual(pulse.get_balance(["aux0", "aux1"], [N, 0]), -1.0)
        self.assertIsNone(pulse.get_balance(["aux0", "aux1", "aux2"], [N, N, N]))

    def test_set_roundtrip(self):
        for b in (-1.0, -0.4, 0.0, 0.25, 1.0):
            vols = pulse.set_balance(STEREO, [N, N], b)
            self.assertEqual(max(vols), N)
            self.assertAlmostEqual(pulse.get_balance(STEREO, vols), b, places=3)

    def test_recover_from_full_side(self):
        vols = pulse.set_balance(STEREO, [N, 0], 0.0)
        self.assertEqual(vols, [N, N])

    def test_surround_keeps_center(self):
        vols = [N, N, N // 2, N // 3, N, N]
        new = pulse.set_balance(SURROUND, vols, 0.5)
        self.assertEqual(new[2:4], vols[2:4])
        self.assertEqual(new[1], N)
        self.assertEqual(new[0], N // 2)


class VolumeTest(unittest.TestCase):
    def test_scale_keeps_ratio(self):
        self.assertEqual(pulse.scale_volumes([N, N // 2], N // 2), [N // 2, N // 4])

    def test_scale_from_zero(self):
        self.assertEqual(pulse.scale_volumes([0, 0], 1000), [1000, 1000])

    def test_pct(self):
        self.assertEqual(pulse.pct(N), 100)
        self.assertEqual(pulse.from_pct(150), N * 3 // 2)

    def test_db(self):
        self.assertAlmostEqual(pulse.volume_to_db(N), 0.0)
        self.assertEqual(pulse.volume_to_db(0), float("-inf"))


class ParseTest(unittest.TestCase):
    def test_parse_stream(self):
        raw = {
            "index": 7,
            "sink": 3,
            "mute": True,
            "corked": False,
            "channel_map": "front-left,front-right",
            "volume": {"front-left": {"value": 100}, "front-right": {"value": 50}},
            "properties": {"application.name": "Firefox", "media.name": "Video"},
        }
        item = pulse._parse_stream(pulse.SINK_INPUT, raw)
        self.assertEqual(item.title, "Firefox")
        self.assertEqual(item.subtitle, "Video")
        self.assertEqual(item.parent, 3)
        self.assertEqual(item.volumes, [100, 50])
        self.assertTrue(item.mute)
        self.assertEqual(item.ident, "7")

    def test_monitor_detection(self):
        raw = {"index": 1, "name": "x.monitor", "description": "Monitor of X", "monitor_source": "x"}
        self.assertTrue(pulse._parse_device(pulse.SOURCE, raw, "").is_monitor)
        raw = {"index": 2, "name": "mic", "description": "Mic", "monitor_source": "n/a"}
        self.assertFalse(pulse._parse_device(pulse.SOURCE, raw, "").is_monitor)

    def test_card_profiles_sorted(self):
        raw = {
            "index": 0,
            "name": "card0",
            "active_profile": "b",
            "profiles": {
                "a": {"description": "A", "priority": 1, "available": True},
                "b": {"description": "B", "priority": 9, "available": False},
            },
        }
        card = pulse._parse_card(raw)
        self.assertEqual([p.name for p in card.profiles], ["b", "a"])
        self.assertEqual(card.profile_description, "B")


class TextTest(unittest.TestCase):
    def test_trunc_wide(self):
        self.assertEqual(swidth("日本"), 4)
        self.assertLessEqual(swidth(trunc("日本語のアプリ", 5)), 5)
        self.assertEqual(trunc("short", 10), "short")

    def test_channel_label(self):
        self.assertEqual(channel_label("front-left"), "FL")
        self.assertEqual(channel_label("lfe"), "LFE")
        self.assertEqual(channel_label("aux1"), "A1")


if __name__ == "__main__":
    unittest.main()

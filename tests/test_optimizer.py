"""python -m unittest discover -s tests"""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import u1_travel_optimizer as opt

CONFIG = ["; nozzle_temperature = 220,220,220,220", "; printer_model = Snapmaker U1", ""]


def patch(x, y, z=0.2):
    """A small square patch whose retract and travel come first, as the slicer writes them."""
    return ["G1 E-1 F6000", f"G1 Z{z + 0.2:g} F30000", f"G1 X{x} Y{y} F30000", f"G1 Z{z:g}", "G1 E1 F6000", "G1 F3600",
            f"G1 X{x + 2} Y{y} E.1", f"G1 X{x + 2} Y{y + 2} E.1", f"G1 X{x} Y{y + 2} E.1"]


def sweeping_gcode():
    """Patches on alternating sides of the bed: left, right, left, right, ..."""
    lines = ["M83", "T0", ";LAYER_CHANGE", ";Z:0.2", "G1 X10 Y10 F30000"]
    for i in range(6):
        lines += patch(10 if i % 2 == 0 else 250, 10 + i * 10)
    return lines + ["G1 E-1 F6000", ";LAYER_CHANGE", ";Z:0.4"] + CONFIG


def wiped_gcode():
    """The same sweep as the slicer writes it with wipe on (the U1 default): travel acceleration, retract, wipe back along the patch,
    a lift that rises during the travel, deretract, then the feature's own acceleration."""
    lines = ["M83", "T0", ";LAYER_CHANGE", ";Z:0.2", "G1 X10 Y10 F30000", "G1 E1.5 F1800"]
    end = (10, 10)
    for i in range(6):
        x, y = (10 if i % 2 == 0 else 250), 10 + i * 10
        if i:
            lines += ["SET_VELOCITY_LIMIT ACCEL=6000 ACCEL_TO_DECEL=3000", "G1 E-1.1 F1800", ";WIPE_START", "G1 F9000",
                      f"G1 X{end[0] + 1} Y{end[1]} E-.4", ";WIPE_END", f"G1 X{x} Y{y} Z0.6 F30000", "G1 Z0.2", "G1 E1.5 F1800"]
        lines += ["SET_VELOCITY_LIMIT ACCEL=2000 ACCEL_TO_DECEL=1000", ";TYPE:Outer wall", "G1 F3600",
                  f"G1 X{x + 2} Y{y} E.1", f"G1 X{x + 2} Y{y + 2} E.1", f"G1 X{x} Y{y + 2} E.1"]
        end = (x, y + 2)
    return lines + ["G1 E-1.5 F1800", ";LAYER_CHANGE", ";Z:0.4", "; travel_slope = 3"] + CONFIG


class WipeTests(unittest.TestCase):
    """Fixes in 1.1.0, found on real prints with the U1's default settings (wipe on)."""

    def test_wipes_and_retracts_stay_balanced(self):
        lines = wiped_gcode()
        result, messages = opt.process(lines, do_preheat=False)
        self.assertIsNot(result, lines, messages)
        a, b = opt.scan(lines), opt.scan(result)
        self.assertLess(b["travel_mm"], a["travel_mm"] * 0.6)
        self.assertEqual(a["wipes"], b["wipes"])
        self.assertEqual(a["restarts"], b["restarts"])
        self.assertEqual(a["idle_pushes"], b["idle_pushes"])

    def test_travel_keeps_the_travel_acceleration(self):
        result, _ = opt.process(wiped_gcode(), do_preheat=False)
        accel = None
        for line in result:
            if line.startswith("SET_VELOCITY_LIMIT"):
                accel = line
            elif accel and line.startswith("G1 X") and "F30000" in line and " E" not in line:  # every travel after the first patch
                self.assertIn("ACCEL=6000", accel, line)

    def test_lifts_rise_along_the_travel(self):
        result, _ = opt.process(sweeping_gcode(), do_preheat=False)
        self.assertFalse([line for line in result if re.match(r"^G1 Z[\d.]+ F30000$", line)])  # no separate straight-up hop
        self.assertTrue([line for line in result if re.match(r"^G1 X[\d.]+ Y[\d.]+ Z[\d.]+ F30000$", line)])

    def test_no_extra_priming_at_the_end_of_a_run(self):
        lines = sweeping_gcode()
        result, _ = opt.process(lines, do_preheat=False)
        self.assertEqual(sum(opt.kind(line) == "der" for line in lines), sum(opt.kind(line) == "der" for line in result))

    def test_verify_catches_a_lost_wipe(self):
        lines = wiped_gcode()
        wipe = next(line for line in lines if line.endswith("E-.4"))
        with self.assertRaises(opt.CheckFailed):
            opt.verify(lines, [line for line in lines if line != wipe])

    def test_verify_catches_filament_pushed_out_with_nothing_printed(self):
        lines = wiped_gcode()
        i = lines.index("G1 X12 Y10 E.1") + 1
        with self.assertRaises(opt.CheckFailed):
            opt.verify(lines, lines[:i] + ["G1 E1", "G1 E-1"] + lines[i:])  # no speed change, so only this check can catch it


class ReorderTests(unittest.TestCase):
    def test_cuts_travel_and_keeps_every_extrusion(self):
        lines = sweeping_gcode()
        result, messages = opt.process(lines, do_preheat=False)
        a, b = opt.scan(lines), opt.scan(result)
        self.assertLess(b["travel_mm"], a["travel_mm"] * 0.5, messages)
        self.assertEqual(a["extrusions"], b["extrusions"])
        self.assertEqual(a["fixed"], b["fixed"])

    def test_fine_feedrates_survive_the_reorder(self):
        # The 0.2 mm profiles write speeds like F16543.922 once the flow limit applies. Six digits of precision fail the check.
        lines = [line.replace("G1 F3600", "G1 F16543.922") for line in sweeping_gcode()]
        result, messages = opt.process(lines, do_preheat=False)
        self.assertIsNot(result, lines, messages)
        self.assertEqual(opt.scan(lines)["extrusions"], opt.scan(result)["extrusions"])

    def test_already_efficient_file_is_kept(self):
        lines = ["M83", "T0", ";LAYER_CHANGE", ";Z:0.2", "G1 X10 Y10 F30000"]
        for i in range(4):
            lines += patch(10 + i * 3, 10)
        lines += CONFIG
        result, _ = opt.process(lines, do_preheat=False)
        self.assertIs(result, lines)

    def test_absolute_extrusion_is_refused(self):
        lines = ["M82" if line == "M83" else line for line in sweeping_gcode()]
        result, messages = opt.process(lines, do_preheat=False)
        self.assertIs(result, lines)
        self.assertIn("relative extrusion", " ".join(messages))

    def test_xy_arcs_are_refused(self):
        lines = sweeping_gcode()
        lines.insert(8, "G2 X20 Y20 I5 J0 E.1")
        result, messages = opt.process(lines, do_preheat=False)
        self.assertIs(result, lines)
        self.assertIn("Arc fitting", " ".join(messages))

    def test_lift_only_spiral_arcs_are_allowed(self):
        lines = sweeping_gcode()
        lines = [("G3 Z.4 I.6 J0 P1 F30000" if line == "G1 Z0.4 F30000" else line) for line in lines]
        result, _ = opt.process(lines, do_preheat=False)
        self.assertIsNot(result, lines)

    def test_other_printers_are_skipped(self):
        lines = [line.replace("Snapmaker U1", "Another Printer") for line in sweeping_gcode()]
        result, messages = opt.process(lines)
        self.assertIs(result, lines)
        self.assertIn("only the Snapmaker U1", " ".join(messages))

    def test_verify_catches_a_lost_extrusion(self):
        lines = sweeping_gcode()
        damaged = [line for line in lines if line != "G1 X12 Y10 E.1"]
        self.assertEqual(len(damaged), len(lines) - 1)
        with self.assertRaises(opt.CheckFailed):
            opt.verify(lines, damaged)

    def test_verify_catches_a_lost_tool_change(self):
        lines = sweeping_gcode()
        with self.assertRaises(opt.CheckFailed):
            opt.verify(lines, [line for line in lines if line != "T0"])


class PreheatTests(unittest.TestCase):
    def test_guard_goes_before_a_later_tool_change(self):
        lines = ["M83", "T0", ";LAYER_CHANGE", "T1", "G1 X1 Y1 E.1"] + CONFIG
        result, _ = opt.guard_cold_tool_changes(lines)
        index = result.index("T1")
        self.assertTrue(result[index - 2].startswith("M104 T1 S220"))
        self.assertTrue(result[index - 1].startswith("TEMPERATURE_WAIT SENSOR=extruder1 MINIMUM=215"))

    def test_first_tool_before_any_layer_is_not_guarded(self):
        lines = ["T0", ";LAYER_CHANGE"] + CONFIG
        result, message = opt.guard_cold_tool_changes(lines)
        self.assertEqual(result, lines)
        self.assertIn("0 tool changes", message)

    def test_guard_runs_once(self):
        lines = ["M83", "T0", ";LAYER_CHANGE", "T1"] + CONFIG
        once, _ = opt.guard_cold_tool_changes(lines)
        twice, message = opt.guard_cold_tool_changes(once)
        self.assertEqual(once, twice)
        self.assertIn("already", message)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "a file with spaces.gcode")
        self.log = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = self.dir.name

    def tearDown(self):
        os.environ["LOCALAPPDATA"] = self.log or ""
        self.dir.cleanup()

    def write(self, lines):
        with open(self.path, "wb") as f:
            f.write("\n".join(lines).encode("utf-8", "surrogateescape"))

    def read(self):
        with open(self.path, "rb") as f:
            return f.read()

    def test_in_place_then_second_run_changes_nothing(self):
        self.write(sweeping_gcode())
        self.assertEqual(opt.main([self.path]), 0)
        first = self.read()
        self.assertIn(b"; u1_travel_optimizer", first)
        self.assertEqual(opt.main([self.path]), 0)
        self.assertEqual(first, self.read())

    def test_unsupported_file_stays_byte_identical(self):
        self.write(["M82" if line == "M83" else line for line in sweeping_gcode()])
        before = self.read()
        self.assertEqual(opt.main(["--no-preheat", self.path]), 0)
        self.assertEqual(before, self.read())

    def test_non_utf8_bytes_survive(self):
        self.write(sweeping_gcode())
        raw = self.read().replace(b"; printer_model", "; caf\udce9\n; printer_model".encode("utf-8", "surrogateescape"))
        with open(self.path, "wb") as f:
            f.write(raw)
        self.assertEqual(opt.main([self.path]), 0)
        self.assertIn(b"caf\xe9", self.read())

    def test_a_crash_keeps_the_original(self):
        self.write(sweeping_gcode())
        before = self.read()
        original = opt.process
        opt.process = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertEqual(opt.main([self.path]), 0)
        finally:
            opt.process = original
        self.assertEqual(before, self.read())
        self.assertEqual([n for n in os.listdir(self.dir.name) if n.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""U1 Travel Optimizer: a post-processing script for Snapmaker Orca and the Snapmaker U1.

With Full Spectrum (mixed) filaments, the slicer prints each layer's patches grouped by mixed filament, so one physical head can
cross the bed many times per layer. This script reorders whole patches inside each tool block into a nearest-next tour, then makes
sure no tool change can pick a cold head.

Usage:
    u1_travel_optimizer <file.gcode> [--output OUT] [--no-reorder] [--no-preheat] [--force]

The file is changed in place unless --output is given. Before anything is written, the result is compared with the original: every
extrusion move and every tool change, temperature, wait and layer line must match. If a check fails or the file is not supported,
the original is left byte for byte unchanged and the script still exits with 0, so a print is never blocked.
"""
import argparse
import collections
import math
import os
import re
import sys
import tempfile
import time
import traceback

__version__ = "1.1.0"

MARKER = "; u1_travel_optimizer"
PREHEAT_COMMENT = "; fix_preheat"
GLUE_MM = 1.0  # patches whose original hop between them is shorter than this stay together (e.g. inner + outer wall of one spot)
DEFAULT_TRAVEL_SLOPE_DEG = 3.0  # used when the file doesn't state travel_slope
MOVE = re.compile(r"^G[0-3](?=\s)")
FIXED = re.compile(r"^(T\d|M104|M109|M140|M190|TEMPERATURE_WAIT|G4|M400|M220|SET_PRINT_STATS_INFO|TIMELAPSE|DEFECT|;Z:|;LAYER_CHANGE|; WIPE_TOWER)")
BARRIER_COMMENTS = (";TYPE:Prime tower", "; WIPE_TOWER_START", "; WIPE_TOWER_END", ";LAYER_CHANGE", ";Z:", ";BEFORE_LAYER_CHANGE",
                    ";AFTER_LAYER_CHANGE", "; CP TOOLCHANGE", ";HEIGHT:")
HEATERS = ["extruder", "extruder1", "extruder2", "extruder3"]


class Unsupported(Exception):
    """The file can't be optimized safely. The original is kept."""


class CheckFailed(Exception):
    """The optimized result didn't match the original. The original is kept."""


def params(line):
    values = {}
    for token in line.split(";")[0].split()[1:]:
        if token[0] in "XYZEFIJP" and len(token) > 1:
            try:
                values[token[0]] = float(token[1:])
            except ValueError:
                pass
    return values


def is_arc(line):
    return line.startswith(("G2 ", "G3 "))


def safe(line):
    """True when the line may travel with a patch; anything else ends the reorderable run."""
    s = line.strip()
    if not s:
        return True
    if s.startswith(";"):
        return not s.startswith(BARRIER_COMMENTS)
    if MOVE.match(s) or s == "G17":
        return True
    return s.startswith(("SET_VELOCITY_LIMIT", "EXCLUDE_OBJECT_START", "EXCLUDE_OBJECT_END", "M106", "M73"))


def kind(line):
    """'ext' extrusion with XY, 'trav' XY/Z move without E, 'ret' E<0 only, 'der' E>0 only, 'arc' G2/G3, 'other'."""
    s = line.strip()
    if is_arc(s):
        return "arc"
    if not MOVE.match(s):
        return "other"
    p = params(s)
    e = p.get("E", 0.0)
    xy = "X" in p or "Y" in p
    if e > 0 and xy:
        return "ext"
    if e < 0 and not xy and "Z" not in p:
        return "ret"
    if e > 0 and not xy and "Z" not in p:
        return "der"
    if e == 0 and (xy or "Z" in p):
        return "trav"
    return "other_move"


class Reorderer:
    def __init__(self, wipe, slope):
        self.wipe = wipe  # the slicer wipes after each retract
        self.slope = slope  # tan(travel_slope): how fast a lift rises along the travel
        self.state = dict(x=0.0, y=0.0, z=0.0, F=None, accel=None, fan=None, typ=None, obj=None)
        self.relative = False
        self.patches = 0
        self.runs = 0
        self.before_mm = 0.0
        self.after_mm = 0.0

    def track(self, line):
        st = self.state
        s = line.strip()
        if s.startswith("SET_VELOCITY_LIMIT"):
            st["accel"] = s
        elif s.startswith("M106") and " P" not in s:
            st["fan"] = s
        elif s.startswith(";TYPE:"):
            st["typ"] = s
        elif s.startswith("EXCLUDE_OBJECT_START"):
            st["obj"] = s.split("NAME=")[1]
        elif s.startswith("EXCLUDE_OBJECT_END"):
            st["obj"] = None
        elif s.startswith("G90"):
            self.relative = False
        elif s.startswith("G91"):
            self.relative = True
        elif MOVE.match(s):
            p = params(s)
            if "F" in p:
                st["F"] = p["F"]
            if self.relative:  # custom gcode only: its moves are outside every reorderable run
                st["x"] += p.get("X", 0.0)
                st["y"] += p.get("Y", 0.0)
                st["z"] += p.get("Z", 0.0)
                return
            if not is_arc(s):
                st["x"] = p.get("X", st["x"])
                st["y"] = p.get("Y", st["y"])
            if "Z" in p:
                st["z"] = p["Z"]

    def flush(self, seg):
        """seg: list of (line, state before the line). Patches start at a retract followed by an XY travel before the next extrusion."""
        if len(seg) < 3:
            return [line for line, _ in seg]
        kinds = [kind(line) for line, _ in seg]
        starts = []
        for i, k in enumerate(kinds):
            if k != "ret":
                continue
            j = i + 1
            travel_xy = False
            while j < len(seg) and kinds[j] != "ext":
                if kinds[j] == "trav" and ("X" in params(seg[j][0]) or "Y" in params(seg[j][0])):
                    travel_xy = True
                if kinds[j] == "ret":
                    break
                j += 1
            if travel_xy and j < len(seg) and kinds[j] == "ext":
                starts.append(i)
        if len(starts) < 2:
            return [line for line, _ in seg]
        if self.wipe:
            # With wipe on, each exit is: retract, wipe, travel. The retract and the wipe run along the patch just printed, so they stay
            # with that patch; the next patch starts after the wipe. A progress or speed line can sit between the retract and the wipe.
            cuts = []
            for a in starts:
                j = a + 1
                while j < len(seg) and kinds[j] == "other" and not seg[j][0].strip().startswith(";WIPE_START"):
                    j += 1
                if j < len(seg) and seg[j][0].strip().startswith(";WIPE_START"):
                    while j < len(seg) and not seg[j][0].strip().startswith(";WIPE_END"):
                        j += 1
                    cuts.append(min(j + 1, len(seg) - 1))
                else:
                    cuts.append(a + 1)
            starts = cuts
            if any(not any(k == "ext" for k in kinds[a:b]) for a, b in zip(starts, starts[1:] + [len(seg)])):
                return [line for line, _ in seg]
        head = seg[:starts[0]]
        chunks = []
        for a, b in zip(starts, starts[1:] + [len(seg)]):
            part = seg[a:b]
            part_kinds = kinds[a:b]
            first_ext = next(k for k, kk in enumerate(part_kinds) if kk == "ext")
            trav, body = part[:first_ext], part[first_ext:]
            s_body = body[0][1]  # original state right before the first extrusion
            ex, ey = s_body["x"], s_body["y"]
            for line, _ in body:
                if MOVE.match(line) and not is_arc(line):
                    p = params(line)
                    ex = p.get("X", ex)
                    ey = p.get("Y", ey)
            lifts = [params(line)["Z"] for line, _ in trav if MOVE.match(line) and "Z" in params(line)]
            chunks.append(dict(trav=trav, body=body, s0=part[0][1], sb=s_body, start=(s_body["x"], s_body["y"]), end=(ex, ey),
                               prev=(part[0][1]["x"], part[0][1]["y"]), hop=max(lifts) if lifts else None))
        # the run's trailing lines after its final extrusion (e.g. the retract before a tool change) stay at the very end
        last_body = chunks[-1]["body"]
        last_ext = max(i for i, (line, _) in enumerate(last_body) if kind(line) == "ext")
        tail = last_body[last_ext + 1:]
        chunks[-1]["body"] = last_body[:last_ext + 1]
        # glue near-contiguous patches, then nearest-next over the resulting units
        units = []
        for chunk in chunks:
            if units and math.dist(chunk["prev"], chunk["start"]) < GLUE_MM:
                units[-1].append(chunk)
            else:
                units.append([chunk])
        pos = chunks[0]["prev"]
        order = []
        remaining = set(range(len(units)))
        remaining.discard(len(units) - 1)  # the run's last patch stays last, so the run's tail follows it exactly as the slicer wrote it
        while remaining:
            k = min(remaining, key=lambda i: math.dist(pos, units[i][0]["start"]))
            order.append(k)
            remaining.discard(k)
            pos = units[k][-1]["end"]
        order.append(len(units) - 1)
        self.runs += 1
        self.patches += len(chunks)
        self.before_mm += sum(math.dist(c["prev"], c["start"]) for c in chunks)
        p = chunks[0]["prev"]
        for k in order:
            for c in units[k]:
                self.after_mm += math.dist(p, c["start"])
                p = c["end"]
        out = [line for line, _ in head]
        cur_obj = chunks[0]["s0"]["obj"]
        cur = chunks[0]["prev"]
        for k in order:
            for c in units[k]:
                s0, sb = c["s0"], c["sb"]
                if s0["accel"]:
                    out.append(s0["accel"])  # the state the patch's retract and travel originally ran with
                for line, _ in c["trav"]:  # retracts and non-move lines (no travel, no object markers)
                    kk = kind(line)
                    # the next feature's SET_VELOCITY_LIMIT goes back in after the travel, so the travel keeps the travel acceleration
                    if kk == "ret" or (kk == "other" and not line.startswith(("EXCLUDE_OBJECT", "SET_VELOCITY_LIMIT")) and line.strip() != "G17"):
                        out.append(line)
                if sb["obj"] != cur_obj:
                    if cur_obj:
                        out.append(f"EXCLUDE_OBJECT_END NAME={cur_obj}")
                    if sb["obj"]:
                        out.append(f"EXCLUDE_OBJECT_START NAME={sb['obj']}")
                    cur_obj = sb["obj"]
                if c["hop"] is not None and c["hop"] > sb["z"] + 1e-6:
                    # lift the way the slicer does: rise along the start of the travel at travel_slope, carry on at that height,
                    # then drop straight down onto the patch
                    d = math.dist(cur, (sb["x"], sb["y"]))
                    rise = (c["hop"] - sb["z"]) / self.slope
                    if d > rise:
                        u = rise / d
                        out.append(f"G1 X{cur[0] + (sb['x'] - cur[0]) * u:.3f} Y{cur[1] + (sb['y'] - cur[1]) * u:.3f} Z{c['hop']:.10g} F30000")
                        out.append(f"G1 X{sb['x']:.3f} Y{sb['y']:.3f} Z{c['hop']:.10g}")
                    else:
                        out.append(f"G1 X{sb['x']:.3f} Y{sb['y']:.3f} Z{c['hop']:.10g} F30000")
                    out.append(f"G1 Z{sb['z']:.10g}")
                else:
                    out.append(f"G1 X{sb['x']:.3f} Y{sb['y']:.3f} F30000")
                for line, _ in c["trav"]:
                    if kind(line) == "der":
                        out.append(line)
                # the exact modal state at the first extrusion
                if sb["accel"]:
                    out.append(sb["accel"])
                if sb["fan"]:
                    out.append(sb["fan"])
                if sb["typ"]:
                    out.append(sb["typ"])
                if sb["F"]:
                    out.append(f"G1 F{sb['F']:.10g}")
                for line, _ in c["body"]:
                    if line.startswith("EXCLUDE_OBJECT_START"):
                        cur_obj = line.split("NAME=")[1].strip()
                    elif line.startswith("EXCLUDE_OBJECT_END"):
                        cur_obj = None
                    out.append(line)
                cur = c["end"]
        # the run's last patch was printed last, so the head is already where the original run ended: restore the modal state the
        # original had right before the tail, then replay the tail verbatim
        end_state = dict(tail[0][1]) if tail else dict(self.state)
        if cur_obj != end_state["obj"]:
            if cur_obj:
                out.append(f"EXCLUDE_OBJECT_END NAME={cur_obj}")
            if end_state["obj"]:
                out.append(f"EXCLUDE_OBJECT_START NAME={end_state['obj']}")
        if end_state["accel"]:
            out.append(end_state["accel"])
        if end_state["fan"]:
            out.append(end_state["fan"])
        if end_state["typ"]:
            out.append(end_state["typ"])
        if end_state["F"]:
            out.append(f"G1 F{end_state['F']:.10g}")
        out += [line for line, _ in tail]
        return out

    def run(self, lines):
        out = []
        seg = []
        in_tower = False
        for line in lines:
            s = line.strip()
            if s.startswith("; WIPE_TOWER_START") or s.startswith(";TYPE:Prime tower"):
                in_tower = True
            if in_tower or self.relative or not safe(line):
                out += self.flush(seg)
                seg = []
                out.append(line)
                self.track(line)
                if s.startswith("; WIPE_TOWER_END") or (s.startswith(";TYPE:") and not s.startswith(";TYPE:Prime tower")):
                    in_tower = False
                continue
            seg.append((line, dict(self.state)))
            self.track(line)
        out += self.flush(seg)
        return out


def check_supported(lines):
    """Raise Unsupported when the reorder pass can't reason about this file."""
    relative_e = False
    for line in lines:
        s = line.strip()
        if s.startswith("M83"):
            relative_e = True
        elif s.startswith("M82"):
            relative_e = False
        elif is_arc(s):
            p = params(s)
            if "X" in p or "Y" in p:
                raise Unsupported("the file uses XY arc moves (Arc fitting); turn Arc fitting off to optimize it")
    if not relative_e:
        raise Unsupported("the file doesn't use relative extrusion (M83); turn on 'Use relative E distances'")


def scan(lines):
    x = y = 0.0
    z = tool = feed = active = zpos = None
    extrusions = collections.Counter()
    wipes = collections.Counter()
    restarts = collections.Counter()  # mm of filament still held back (+) or pushed out extra (-) when printing resumes
    held = 0.0
    e_moved = False
    pushed = False
    idle_pushes = 0  # filament pushed forward, then a retract or a travel instead of printing
    fixed = []
    travel = 0.0
    object_starts = 0
    unbalanced = 0
    relative = False
    for line in lines:
        s = line.strip()
        if s.startswith(";Z:"):
            z = s[3:]
        if re.match(r"^T\d", s):
            tool = s
            held = 0.0
            e_moved = False
        if FIXED.match(s):
            fixed.append(s)
        if s.startswith("EXCLUDE_OBJECT_START"):
            if active is not None:
                unbalanced += 1
            active = s
            object_starts += 1
        elif s.startswith("EXCLUDE_OBJECT_END"):
            if active is None or active.split("NAME=")[1] != s.split("NAME=")[1]:
                unbalanced += 1
            active = None
        elif s.startswith("G90"):
            relative = False
        elif s.startswith("G91"):
            relative = True
        if MOVE.match(s) and not is_arc(s):
            p = params(s)
            if "F" in p:
                feed = p["F"]
            if relative:
                x += p.get("X", 0.0)
                y += p.get("Y", 0.0)
                continue
            zpos = p.get("Z", zpos)
            nx, ny = p.get("X", x), p.get("Y", y)
            e = p.get("E", 0.0)
            xy = "X" in p or "Y" in p
            moved = xy and math.hypot(nx - x, ny - y) > 0.01  # the slicer sometimes writes a 0.001 mm nudge; that isn't a travel
            if pushed and (e < 0 or (e == 0 and moved)):
                idle_pushes += 1
                pushed = False
            elif pushed and e > 0 and xy:
                pushed = False
            if e > 0 and not xy:
                pushed = True
            if e < 0 or (e > 0 and not xy):  # retract, wipe or deretract
                held -= e
                e_moved = True
            elif e > 0 and e_moved:  # first print move after them: nothing should be held back or pushed out
                if abs(held) > 0.05:
                    restarts[round(held, 1)] += 1
                held = 0.0
                e_moved = False
            if e < 0 and xy:
                wipes[(z, tool, round(x, 3), round(y, 3), round(nx, 3), round(ny, 3), round(e, 5))] += 1
            if e > 0 and (nx != x or ny != y):
                extrusions[(z, tool, round(x, 3), round(y, 3), round(nx, 3), round(ny, 3), round(e, 5), feed, zpos)] += 1
            elif e == 0:
                travel += math.hypot(nx - x, ny - y)
            x, y = nx, ny
    return dict(extrusions=extrusions, wipes=wipes, restarts=restarts, idle_pushes=idle_pushes, fixed=fixed, travel_mm=travel,
                object_starts=object_starts, unbalanced=unbalanced)


def verify(original, result):
    a, b = scan(original), scan(result)
    if a["extrusions"] != b["extrusions"]:
        only_a = sum((a["extrusions"] - b["extrusions"]).values())
        only_b = sum((b["extrusions"] - a["extrusions"]).values())
        raise CheckFailed(f"extrusion moves differ ({only_a} only in the original, {only_b} only in the result)")
    if a["wipes"] != b["wipes"]:
        raise CheckFailed(f"wipe moves differ ({sum(a['wipes'].values())} in the original, {sum(b['wipes'].values())} in the result)")
    if a["restarts"] != b["restarts"]:
        raise CheckFailed("retract and deretract no longer balance where printing resumes")
    if a["idle_pushes"] != b["idle_pushes"]:
        raise CheckFailed(f"filament pushed out with nothing printed after it: {a['idle_pushes']} in the original, {b['idle_pushes']} in the result")
    if a["fixed"] != b["fixed"]:
        raise CheckFailed("tool changes, temperatures, waits or layer lines differ")
    if b["unbalanced"] > a["unbalanced"]:
        raise CheckFailed("object markers became unbalanced")
    return a, b


def reorder(lines):
    check_supported(lines)
    if not any(kind(line) == "ret" for line in lines):
        return lines, "reorder: nothing to do (no retractions)"
    wipe = any(line.startswith(";WIPE_START") for line in lines)
    slope_deg = DEFAULT_TRAVEL_SLOPE_DEG
    for line in reversed(lines):
        if line.startswith("; travel_slope = "):
            try:
                slope_deg = float(line.split("=", 1)[1].split(",")[0])
            except ValueError:
                pass
            break
    worker = Reorderer(wipe, math.tan(math.radians(max(slope_deg, 0.5))))
    result = worker.run(lines)
    if worker.patches == 0:
        return lines, "reorder: nothing to do"
    a, b = verify(lines, result)
    if b["travel_mm"] >= a["travel_mm"]:
        return lines, f"reorder: kept the original order (travel wouldn't drop: {a['travel_mm'] / 1000:.2f} m -> {b['travel_mm'] / 1000:.2f} m)"
    return result, (f"reorder: {worker.patches} patches in {worker.runs} runs; {sum(a['extrusions'].values())} extrusion moves verified; "
                    f"total travel {a['travel_mm'] / 1000:.1f} m -> {b['travel_mm'] / 1000:.1f} m")


def guard_cold_tool_changes(lines):
    """Before every tool change after the first layer change, make sure the head is heating and hot enough."""
    if any(line.endswith(("fix_preheat", "never pick a cold head")) for line in lines):
        return lines, "preheat: already guarded"
    match = None
    for line in reversed(lines):
        if line.startswith("; nozzle_temperature = "):
            match = line
            break
    if match is None:
        return lines, "preheat: skipped (no nozzle_temperature in the file)"
    print_temps = [int(float(v)) for v in match.split("=", 1)[1].split(",")]
    if len(print_temps) < len(HEATERS):
        print_temps += [print_temps[-1]] * (len(HEATERS) - len(print_temps))
    target = {i: 0.0 for i in range(len(HEATERS))}
    current = None
    started = False
    guarded = 0
    out = []
    for line in lines:
        s = line.split(";")[0].strip()
        if line.startswith(";LAYER_CHANGE"):
            started = True
        heat = re.match(r"^M10[49]\b(.*)", s)
        if heat:
            head = re.search(r"\bT(\d)", heat.group(1))
            value = re.search(r"\bS(\d+(?:\.\d+)?)", heat.group(1))
            k = int(head.group(1)) if head else current
            if value and k in target:
                target[k] = float(value.group(1))
        change = re.match(r"^T([0-3])$", s)
        if change:
            k = int(change.group(1))
            if started and k != current:
                t = target[k] if target[k] >= 170 else print_temps[k]
                out.append(f"M104 T{k} S{t:g} {PREHEAT_COMMENT}")
                out.append(f"TEMPERATURE_WAIT SENSOR={HEATERS[k]} MINIMUM={t - 5:g} {PREHEAT_COMMENT}: never pick a cold head")
                guarded += 1
            current = k
        out.append(line)
    return out, f"preheat: guarded {guarded} tool changes"


def printer_model(lines):
    for line in reversed(lines):
        if line.startswith("; printer_model = "):
            return line.split("=", 1)[1].strip()
    return None


def already_processed(lines):
    return any(line.startswith(MARKER) for line in lines[-20:])


def process(lines, do_reorder=True, do_preheat=True, force=False):
    """Return (lines, messages). The lines are the input object itself when nothing changed."""
    messages = []
    if already_processed(lines):
        return lines, ["already optimized; nothing to do"]
    model = printer_model(lines)
    if model != "Snapmaker U1" and not force:
        return lines, [f"skipped: this file is for '{model}', and only the Snapmaker U1 has been tested (use --force to try anyway)"]
    result = lines
    if do_reorder:
        try:
            result, message = reorder(result)
            messages.append(message)
        except Unsupported as e:
            messages.append(f"reorder skipped: {e}")
        except CheckFailed as e:
            messages.append(f"reorder discarded by the safety check: {e}")
    if do_preheat and model == "Snapmaker U1":
        result, message = guard_cold_tool_changes(result)
        messages.append(message)
    if result is not lines and result != lines:
        result = list(result)
        tail_pos = len(result)
        while tail_pos > 0 and not result[tail_pos - 1].strip():
            tail_pos -= 1
        result.insert(tail_pos, f"{MARKER} {__version__}")
        return result, messages
    return lines, messages


def read_lines(path):
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        return f.read().split("\n")


def write_atomic(path, lines):
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".u1_travel_", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="\n") as f:
            f.write("\n".join(lines))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def log_path():
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "U1TravelOptimizer", "last_run.log")


def log(messages):
    text = "\n".join(messages)
    if sys.stdout is not None:  # None in the windowed Windows build
        print(text)
    try:
        path = log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write(text + "\n")
    except OSError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="u1_travel_optimizer", description="Cut Full Spectrum travel in Snapmaker U1 gcode.")
    parser.add_argument("gcode", help="the gcode file; Orca passes this automatically")
    parser.add_argument("--output", help="write here instead of changing the file in place")
    parser.add_argument("--no-reorder", action="store_true", help="don't reorder patches")
    parser.add_argument("--no-preheat", action="store_true", help="don't add cold tool change guards")
    parser.add_argument("--force", action="store_true", help="reorder files for printers other than the U1 (untested)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    started = time.time()
    messages = [f"u1_travel_optimizer {__version__}", f"file: {args.gcode}"]
    try:
        lines = read_lines(args.gcode)
        result, notes = process(lines, not args.no_reorder, not args.no_preheat, args.force)
        messages += notes
        if result is not lines:
            write_atomic(args.output or args.gcode, result)
            messages.append(f"written in {time.time() - started:.0f} s")
        elif args.output:
            write_atomic(args.output, lines)
            messages.append("no changes; copied the original to the output path")
        else:
            messages.append("no changes; the original was kept")
    except Exception:  # a failure here must never block a print: the original is still intact
        messages.append("failed; the original was kept")
        messages.append(traceback.format_exc())
    log(messages)
    return 0


if __name__ == "__main__":
    sys.exit(main())

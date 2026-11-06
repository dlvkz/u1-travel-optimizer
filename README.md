# U1 Travel Optimizer

A post-processing script for Snapmaker Orca and the Snapmaker U1. It cuts the travel of Full Spectrum (mixed filament) prints.

With mixed filaments, the slicer prints each layer's patches one mixed filament at a time, so one head can cross the bed again and again. This tool reorders whole patches inside each tool block into a nearest-next tour.

| Test print | Patches moved | Extrusion moves verified | Travel before | Travel after |
|---|---|---:|---:|---:|
| Large mixed-colour relief at 0.1 mm (2x convex mirror) | 327,904 | 3,658,942 | 13.9 km | 3.45 km |
| Mixed-colour plate at 0.2 mm, exported from Snapmaker Orca 2.4.1 | 1,417 | 155,877 | 163.0 m | 149.6 m |

The gain depends on how many separate patches a layer has, so simple prints gain little.

The mirror was printed start to finish on a U1 with an earlier version of this script, in 39.6 hours of printing. The same file wasn't printed without reordering, so the time saved hasn't been measured.

It also adds a guard before each tool change, so a head that is still cold can't be picked. That stops the "extruder below minimum temp" abort on tiny first layers.

This is a post-processing script, not a plugin. The slicer runs it on the G-code after slicing, and there's no button or menu for it.

## Install

You need Python 3.8 or newer, and nothing else.

1. Put the files in a folder that will stay put and **has no spaces in its path**, such as `C:\Tools`.
2. In Snapmaker Orca's process settings, open the **Others** tab and scroll to **Post-processing Scripts** at the bottom. If it isn't there, turn on **Advanced**.
3. Enter the path for your system:

| System | Enter |
|---|---|
| Windows | `C:\Tools\u1_travel_optimizer.bat` |
| macOS or Linux | `python3 /full/path/to/u1_travel_optimizer.py` |

On Windows, enter the `.bat` file and not the `.py` file: the slicer can't start a `.py` file by itself, and `python script.py` doesn't work there either. If Python isn't installed, the `.bat` stops the export with an error.

The setting is stored per process profile, so save the profile to keep it.

## Using it: export, don't send

**Snapmaker Orca runs post-processing scripts when you use Export G-code. In the source code, the U1 Print and Send buttons upload the sliced file without running them.**

1. Slice the plate.
2. Open the menu on the left of the **Print** button, choose **Export G-code file**, then click the button and save the file.
3. Upload the exported file through the printer's web interface or a USB drive.

You can also run it on any gcode file:

```
python u1_travel_optimizer.py plate_1.gcode --output plate_1_optimized.gcode
```

## Safety

Before anything is written, the result is compared with the original:

- every extrusion move must match, by layer, tool, start, end, amount and speed
- every tool change, temperature, wait and layer line must match, in the same order
- object markers must stay balanced

The original file is kept byte for byte, and the slicer still gets a success result, in any of these cases:

- a check fails
- the file isn't for the U1 (`--force` allows other printers, untested)
- the file doesn't use relative extrusion
- the file uses XY arc moves (turn off Arc fitting)
- reordering wouldn't reduce travel
- anything unexpected goes wrong

The last run's result is in `%LOCALAPPDATA%\U1TravelOptimizer\last_run.log`.

## Known limits

- The slicer's time estimate and preview still show the original order.
- Files get bigger, 3% to about 20% on the test prints, because each moved patch restores its own settings.
- Very large files take minutes (about 3 minutes for a 200 MB file).
- Z hops between moved patches become straight lifts instead of spirals.
- Only tested on the Snapmaker U1 with relative extrusion, on a few Full Spectrum models so far.
- Running it from the slicer has only been tested on Windows, with Snapmaker Orca 2.4.1. The macOS and Linux setup hasn't been tested.

## Options

| Option | Effect |
|---|---|
| `--output FILE` | Write to a new file instead of changing the input |
| `--no-reorder` | Only add the cold tool change guards |
| `--no-preheat` | Only reorder |
| `--force` | Reorder files for other printers (untested) |

## Test

```
python -m unittest discover -s tests
```

## License

MIT. See `LICENSE`.

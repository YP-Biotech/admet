# Running protocols

There are two ways to run something in `admet control`:

- **An experiment** (**6. Experiment**): a table of runs, each row its setpoints,
  stops and replicates; **Run next** runs them one after another in **7.
  Experiment run**. See [Experiments](#experiments).
- **A custom protocol** (**8. Custom protocol**): any [JSON protocol](../protocols/format),
  for what a row cannot say, such as one channel under pressure and the others
  under flow, or a calibration.

## Plan, review, execute

1. **Select** a protocol from the selector. Its parameters appear in the
   *Parameter / Value* table; the JSON can be edited too. On the Custom protocol
   step the table's first row is **Condition**, the generation condition the run
   belongs to; left empty it is the protocol with its oil, cells and beads
   setpoints, `collection o300 c40 b40`. Each custom run takes the next id of the
   day's custom runs (an experiment of its own, `Custom 2026-11-26`, that the
   Experiment page does not list), so what it collects is labelled like any
   experiment's: `M4T-02-r1`. Once executed, it is a row there with its
   protocol, condition, state and run folder.
2. **Plan** resolves the parameters and freezes the
   steps. Nothing moves and nothing is recorded. The plan table shows, per step:
   **STEP · UNIT ID · TYPE · TARGET · TRIGGER & ETA · END · CONFIRM**.
   Lines inside one row run at the same time. The action bar sums the step count,
   nominal duration and liquid use.
3. **Execute** runs exactly that reviewed plan. A changed connection, correction,
   acquisition mode or parameter needs a new plan first.

Edits and previews stay in memory. Only Execute archives the protocol, its
parameter values and the run.

## Experiments

An experiment is a table of runs designed before any of them is run, on the
**Experiment** page. Each row is a whole run; there is no protocol to pick:

| Column | What it is |
|---|---|
| **Id** | The run's id, `K7Q-03`, given by control |
| **Mode** | `flow` (µL/min) or `pressure` (mbar), for all three channels; `f` or `p` will do |
| **Oil**, **Cells**, **Beads** | The setpoints on L, M1 and M2 |
| **Stop oil µL**, **Stop total µL**, **Stop s** | What ends each collection: oil delivered, the three channels together, time. An empty stop is off; the first one met ends the collection; at least one is needed |
| **Replicates** | How many collections the run makes, one after another (1–20) |
| **Video**, **Video s** | Whether each collection is filmed (`yes`/`no`), and for how long at most (100 s) |
| **Condition** | What analyze groups the run's videos by; left empty it is the setpoints, `300/40/40` (shown grey) |
| **Note**, **State**, **Run folder** | A note; planned, running, done or failed; where the run was kept |

A value is checked as you type it; one that cannot run is put back. A run that
ran cannot be changed.

- **Add run** adds a row like the last one; **Duplicate** copies the selected
  row; **Remove** takes out a row that has not run.
- **Import JSON…** fills a new experiment from a document; **Export JSON…**
  writes the shown one out, with each run's id, state and folder. A value given
  beside `runs` is every run's unless a run gives its own; a run that names any
  stop has only the stops it names:

  ```json
  {"experiment": "ratio sweep 26 Nov", "replicates": 3, "stop_oil_ul": 150,
   "runs": [{"oil": 300, "cells": 40, "beads": 40},
            {"oil": 250, "cells": 67, "beads": 67, "stop_s": 90,
             "condition": "boundary", "note": "near the bulge"}]}
  ```

  The second run stops at 90 s only. Its keys are those of the table: `mode`,
  `oil`, `cells`, `beads`, `stop_oil_ul`, `stop_total_ul`, `stop_s`,
  `replicates`, `video`, `video_s`, `condition`, `note`. An experiment whose
  `code` is already used gets a new one, and its runs new ids.
- **Run next** runs the next planned row in the **Run** step that follows the
  page. Before each collection control asks for a fresh tube (*Replicate 2 of 3,
  sample K7Q-03-r2: fresh collection in place?*); each collection ends on its
  first stop, and the record says which one. When the run finishes, its row
  says done (or failed) with its run folder, and control asks *Run K7Q-03 is
  finished. Proceed to run K7Q-04?*. A run whose plan has an unmet guard, such
  as video with a camera that is not live, is planned but not started.

Every experiment has a code of three characters with nothing to mistake when
typed (no 0, O, 1, I or L), and each run a number in it: run 3 of experiment
K7Q is **K7Q-03**. The id goes into the run's record, and each replicate the
run collects is a sample, **K7Q-03-r2**. Name what you collect by it: a folder
of microscopy images called `K7Q-03-r2` (or `k7q_03_rep2 cells`) is matched to
that sample by `admet analyze`, see
[Samples of an experiment](../analyze/overview#samples-of-an-experiment).
Experiments are kept in the data folder's `control.db`, beside the runs.

## During a run

| Control | Effect |
|---|---|
| **Confirm** | Passes the current gate; the step then applies its targets. |
| **Pause** | Sets the protocol's channels to zero and pauses recording. |
| **Resume** | Puts the paused step's targets back; the step continues where it was. |
| **Skip** | Ends the current step and moves on. |
| **Abort** | Stops the run, zeroes its channels and closes the recording. |

A step with a `confirm_message` waits at an inline **gate** before it starts —
for example *"Collection 4: weigh the vessel and place it"*. The gate keeps its
prompt through pause and resume.

Time spent paused does not count against a step's duration. A paused or skipped
step is marked in the run's events, and calculations treat it accordingly.

## Measurements

Protocols can declare values you type in during the run: vessel masses, dead
volumes, a reference viscosity. The **Measurements** table sits under the plan,
becomes editable on Execute and stays editable after the run. Values save to the
run as you type; empty cells stay empty, never zero.

A new run starts with an empty table. To review or correct an earlier run, use
**Calculations**.

## Recording

Control keeps every run in one database, the home's `data/control.db`:

- each run's record: the protocol and the plan as they ran, the run's name and
  condition, the state and times, the rig mapping and corrections in force, the
  step events, the measurements typed during the run, the videos made and the
  results calculated from it afterwards;
- each run's timeline, recorded while its protocol runs: pressure and flow of each
  channel ten times a second and the live QC rows once a second, on one clock.

Every change to a record is one transaction, and the timeline is committed once a
second while the run records, so a crash loses at most the last second. Review and
Calculations read the runs from it.

Each run also has a folder under the home's `data/runs/`, named by date, time and
the run's name: `20261118_183000_oil_3/`. It holds the step videos, one file per step
marked `"record": true` (see [Video](../protocols/format#video)), named after the run
and the step. Once the run has finished, it also holds `run.db`: the run **sealed**,
its record and its timeline on their own, written again whenever the record changes
(a result calculated, an export noted). That folder is what goes to the server and
what analyze reads: nothing in it points outside it, so a folder copied elsewhere is
whole, and the file is left whole on its own, so a copy of it, or analyze reading it
from a network share, sees everything.

For a server the rig cannot reach, put the finished runs into one archive, copy it
over, and let analyze take it in:

```bash
admet export --tar runs.tar                       # every finished run
admet export --tar k7q.tar --run 20261126_140000_k7q_03 --run 20261126_141500_k7q_04
```

To read a finished run in another program, write its sealed record and timeline out
as a JSON and a CSV file:

```bash
admet export-run data/runs/20261118_183000_oil_3              # run.json and run.csv in the run folder
admet export-run data/runs/20261118_183000_oil_3 --into out/  # or elsewhere
```

A recording started by hand, outside a protocol, is a run folder too.
The step videos are encoded once the run is over, one after another, so no encode
competes with the next step's recording for the disk; each is noted in the record
as it finishes. A recording made outside a protocol is encoded at once.

Each video carries its own record inside the file (the AVI's `INFO` comment): the
run it belongs to and its name, the step and part, when it started and stopped, the
camera as it was set (model, exposure, gain, binning, pixel size, a frame-rate limit
if one was on), the frame rate measured as the frames arrived, each channel's set
point, and how many frames were captured, written and dropped. A step's end never waits for the disk: the frames still queued are written behind it while the
protocol goes on, and the instrument's shutdown waits for them. It also keeps the
camera's own time: the first frame's timestamp and number on the camera's clock,
the clock's rate, and the frames the camera numbered that never reached the PC
(`frames_skipped`), so frame *k* of the video is placed exactly even when some were
lost. Frames the disk cannot keep up with are counted, never a reason to stop: a
recording with gaps is still listed, marked incomplete. No side file is left once
the video is whole.

## Export

**Export data** copies the finished runs to the server. Type or browse the server
folder as it is mounted on this computer (the rig remembers it), then **Export**:
each run's folder is copied whole under that folder, every file is read back and
compared with the original, and only then do the run's videos leave this computer.
The record and the timeline stay, so Review and Calculations still work here, and
the record says where the run went, when, and the digest of every file sent.

Export again whenever you like: a record that changed since (a result calculated
later) is copied again, a video already on the server is not touched. A run still
going, or still encoding a video, is left for next time. If the server is not
mounted, nothing moves.

## Live QC

While a step records, once a second the next eight consecutive frames are
measured the way analyze measures a video, without following any droplet: the
train's speed from the shift between one frame and the next, the rate as speed over
the spacing between neighbours, and the size and its CV from every whole droplet
seen. A line under the live line shows them with 95 % intervals and the state:

```
1171±21/s  15 675  d 124.0±0.5um  cv 2.7±0.3%  STABLE
```

Rate, droplets made since the recording started, diameter (µm once the scale is
measured, px before), CV, and the state, green **STABLE** when the CV's whole
interval sits below 5 %, red **UNSTABLE** when it sits above, amber **SETTLING**
while it straddles the limit or too few droplets have been seen. The line shows
only while a recording runs and starts from nothing with each one. Each burst is
also a row of the run's fluidics CSV, on its own columns after the readings, naming
its recording, step and part, with `elapsed_s` on the same clock as every reading
and stamped with the time its frames were taken, not the time the row was written.
One file holds the whole run; nothing has to be lined up afterwards.
The measurement runs in its own thread and drops a burst when it is still busy, so
the recording never waits; **Live QC** in the camera settings switches it off.

To check that a machine keeps up with the camera, run `uv run python
tools/camera_bench.py` with the camera connected and control closed; `--contend`
adds the load the window puts on the interpreter while it paints. It says whether
every frame reached the disk and leaves no file behind.

# Running protocols

Every experiment is a [JSON protocol](../protocols/format). In `admet control` you
add one with **+ Protocol step**, pick a template, set its parameters, review the
plan and execute it. The **×** beside an added step takes it out again, with its
plan; the runs it executed stay. Steps are not kept between sessions.

## Plan, review, execute

1. **Select** a protocol from the selector. Its parameters appear in the
   *Parameter / Value* table; the JSON can be edited too.
2. **Plan** first asks for the run's name and its set. The name, proposed from
   the step's, tags the run: the step takes it, and its record, the record of each
   recording and the lists in Review and Calculations carry it. The set is the
   group the run belongs to, one condition or one experiment across runs; it is
   written into the run's record and each recording's, Review lists it, and
   `admet analyze` puts the run's videos into that set when it scans them, named
   `<set>_rep1`, `<set>_rep2`, … (see [Sets](../analyze/overview#sets)), so the
   grouping is done once, at the rig. The set given last is proposed for the
   next run; leave it empty for none. Then Plan resolves the parameters and freezes the
   steps. Nothing moves and nothing is recorded. The plan table shows, per step:
   **STEP · UNIT ID · TYPE · TARGET · TRIGGER & ETA · END · CONFIRM**.
   Lines inside one row run at the same time. The action bar sums the step count,
   nominal duration and liquid use.
3. **Execute** runs exactly that reviewed plan. A changed connection, correction,
   acquisition mode or parameter needs a new plan first.

Edits and previews stay in memory. Only Execute archives the protocol, its
parameter values and the run.

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

Every run is one folder under the home's `data/runs/`, named by date, time and
the run's name: `20261118_183000_oil_3/`. It holds:

- `run.json`, the run's record: the protocol and the plan as they ran, the run's
  name and set, the state and times, the rig mapping and corrections in force,
  the step events, the measurements typed during the run, the videos made and
  the results calculated from it afterwards;
- `run.csv`, the timeline: pressure and flow of each channel and the live QC rows,
  on one clock;
- the step videos, one file per step marked `"record": true`
  (see [Video](../protocols/format#video)), named after the run and the step.

Nothing in the folder points outside it, so a folder copied elsewhere is whole.
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

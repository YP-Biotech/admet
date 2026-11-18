# Running protocols

Every experiment is a [JSON protocol](../protocols/format). In `admet control` you
add one with **+ Protocol step**, pick a template, set its parameters, review the
plan and execute it.

## Plan, review, execute

1. **Select** a protocol from the selector. Its parameters appear in the
   *Parameter / Value* table; the JSON can be edited too.
2. **Plan** resolves the parameters and freezes the steps. Nothing moves and
   nothing is recorded. The plan table shows, per step:
   **STEP · UNIT ID · TYPE · TARGET · TRIGGER & ETA · END · CONFIRM**.
   Lines inside one row run at the same time. The action bar sums the step count,
   nominal duration and liquid use.
3. **Execute** runs exactly that reviewed plan. A changed connection, correction,
   project or parameter needs a new plan first.

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

Every run records fluidics (pressure and flow of each channel) to
`records/fluidics/`. Steps marked `"record": true` also record video, one file per
step (see [Video](../protocols/format#video)). The run's
`summary.json` points at both and holds the rig mapping and corrections in force.
The step videos are encoded once the run is over, one after another, so no encode
competes with the next step's recording for the disk; they appear in the project
as each one finishes. A recording made outside a protocol is encoded at once.

Each video carries its own record inside the file (the AVI's `INFO` comment): the
project and run it belongs to, the step and part, when it started and stopped, the
camera as it was set (model, exposure, gain, binning, pixel size, a frame-rate limit
if one was on), the frame rate measured as the frames arrived, each channel's set
point, and how many frames were captured, written and dropped. It also keeps the
camera's own time: the first frame's timestamp and number on the camera's clock,
the clock's rate, and the frames the camera numbered that never reached the PC
(`frames_skipped`), so frame *k* of the video is placed exactly even when some were
lost. Frames the disk cannot keep up with are counted, never a reason to stop: a
recording with gaps is still listed, marked incomplete. The project manifest is only an index of these
records — a video it has forgotten (a run that ended badly, a file copied in from
another machine) is listed again from the file when the project is opened, and
`admet check <project>` reports such videos (`--fix` lists them). No side file is
left once the video is whole.

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
only while a recording runs and starts from nothing with each one. The run has
one QC file beside its videos, `records/camera/<run>.qc.csv`, to which each
recording appends its rows, one a burst, each naming its recording, step and part
and stamped with the wall clock and the seconds since that recording started, the
clock the fluidics CSV and the recording's record use, so the three cover the same
seconds. A recording made outside a run gets `<recording>.qc.csv`.
The measurement runs in its own thread and drops a burst when it is still busy, so
the recording never waits; **Live QC** in the camera settings switches it off.

To check that a machine keeps up with the camera, run `uv run python
tools/camera_bench.py` with the camera connected and control closed; `--contend`
adds the load the window puts on the interpreter while it paints. It says whether
every frame reached the disk and leaves no file behind.

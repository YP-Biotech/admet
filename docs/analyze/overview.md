# admet analyze

![admet analyze](/admet_analyze.png)

`admet analyze` serves the analysis over the network. Run it on a machine that
can see the recorded runs, the server or your own; anyone on the lab network opens
it in a browser. It reads recorded data only and never touches the rig.

```bash
uv run --extra analyze admet analyze                      # every interface, port 8080
uv run --extra analyze admet analyze --port 9000
uv run --extra analyze admet analyze --host 127.0.0.1 --data /srv/admet/runs
```

| Option | Default | Meaning |
|---|---|---|
| `--host` | `0.0.0.0` | Address to listen on; the default is every interface |
| `--port` | `8080` | Port to listen on |
| `--data` | none | Another folder of runs to watch beside the home's data folder, for example the export folder on the server |
| `--home` | `ADMET_HOME` or `~/.admet` | The admet home whose data folder is watched and whose `analyze/` room holds the registry and the results |

Analyze watches the home's data folder from the start, so every run the rig
recorded is listed without a step of your own. It keeps its own room in the home:
`analyze/registry.json` lists the folders it watches and every file it knows with
its sample name, engine and settings;
`analyze/results/` holds one folder per file and detection settings, named by a
digest of the file's content and those settings, so the same video analysed the
same way is never analysed twice and a copy of the file finds its result. A video
is known by its size and its first and last megabyte; an image folder by the
names, sizes and times of its files, so listing one reads no image, and a copy
made with its times kept finds its result too.

Press **Ctrl+C** to stop. Running analyses are asked to stop cleanly first, so no
one's work is left half-written.

## The workflow

The list comes back as you left it.

| Stage | What you do there |
|---|---|
| **1. Files** | **Add folder…** takes a folder of runs (a data folder, the export folder on the server, or one run) and lists every run's videos as samples under their generation condition; **Add files…** takes single videos and image folders. Tick **Use** on the samples to take on; put an image folder into its sample from the picker. The selected sample's first frame and facts are shown below the list. Only ticked files go on to 2. Analyse and 3. Results. |
| **2. Analyse** | Fill in **µm/px** and **FPS** for every ticked file (red cells are missing; both are prefilled for recordings made by `admet control`), set a file up on its preview when it needs a crop or other detection, then **Analyse all**. It runs every ticked file that has no result yet or whose detection settings changed since, and skips the rest. **Run selected** runs one file; **Stop** ends a run at its next step. |
| **3. Results** | View only: one row per file with its numbers and their distributions, the plots, and **Export CSV** / **Export plots**. |

Image folders are analysed with Cellpose and videos with OpenCV; both go through the
same three stages.

## Samples and files

Three words cover what is listed. A **condition** is how the droplets were made: the
protocol with the oil, cells and beads setpoints of the step that recorded, see
[Generation conditions](#generation-conditions). A **sample** is one collection: a
run's replicate, shown as `rep 1`, `rep 2`, … under its condition and numbered by
time across the runs of that condition. A **file** is a video or an image folder;
a sample holds its video, the image folders of the same droplets, and the live QC
the rig measured while the video was recorded, each a chip in its column.

A video from a run gets its condition and its sample by itself. An image folder has
no run, so it waits under **Unassigned** until you pick the sample it belongs to,
or make a sample of its own for a collection with no video. Put in the wrong one,
click its chip in the sample's row: **Move to** another sample, or **Unassign** it
back to the bottom of the list. Neither redraws the page. **Use** on a sample
ticks all its files; on a condition line, all its samples; a shift-click on a
sample ticks or clears every sample between it and the one clicked last. The
**Note** field is for anything you want to write down. Click a sample to see its
first frame below, how its droplets were made, the camera as it was set, and,
under Details, the run and step it came from. The list and every file's settings
are kept in the registry, so the screen comes back as you left it; **Rescan** at
the top lists videos that arrived in the watched folders since.

## Generation conditions

A generation condition is how a file's droplets were made: the protocol and the
oil, cells and beads setpoints of the step that recorded, written
`dropseq_3n o300 c40 b40`, a pressure setpoint as `o600mbar`. Every file from a
run gets it at scan, from the condition the run was planned with in `admet
control` when there is one, else from the protocol itself. Runs of one protocol at
the same setpoints thus fall into one condition on their own, whatever day they
ran. Type over a condition's line to give it an alias, `Oil 300`; the derived
condition stays beside it, and every sample of the condition takes the alias in its
name. Conditions carry through to 3. Results: the table groups its lines by
condition with the condition in a column, every file of a condition shares its hue
in the plots with a shade per file, and the summary CSV has condition and
replicate columns. A file in no condition is grouped by its sample name alone.

## Setting a video up

The preview shows the frame at its native size, scaled down to fit. **Drag on the
frame to crop**; *Full frame* clears the crop. The bar under it sets the frames
analysed (start–end, end exclusive); the slider below it picks the preview frame.
After a run, *Show the stored analysis* draws the detected droplets and their paths
over the frame.

Droplets are found as what is darker than the empty channel. The background is the
bright side of each pixel over frames sampled across the video, each frame is matched
to it in brightness (so a lamp that drifts does not matter), and a pixel counts as
droplet when it is darker than the background by a few times the camera noise, never
less than two levels. A closed rim is filled; a rim open on its bright side, as a
droplet lit from one side shows, is completed by its convex hull when that hull has
a droplet's size and shape. An opening the size of a satellite droplet keeps
satellites, wall lines and specks off the main droplets. Droplets are then followed
from frame to frame for their speed, and the generation rate is the train's speed
over the spacing between neighbouring droplets, which needs no droplet to be
followed; when too few neighbours are seen, droplets are counted as they pass a gate
line instead. When the recording's record carries the set flows, the rate cell also
shows the rate those flows must make at the measured droplet size, every flow but
the oil over the volume of a sphere of the median diameter.

Two kinds of settings. **Frame range, crop, threshold × noise, satellite size and
min droplet area** change what the pipeline finds in the frames; change one and the
video is read again. Everything else is applied when the stored rows are read, in
under a second, so changing it updates Results and the export at once: **µm/px** and
**FPS** (the engine measures in pixels and frames), and **skip left / right** (% of
the width left out, where droplets form and where they leave) and the **gate** (% of
the width, where droplets are counted as they pass). The run always searches the
whole width and keeps every droplet; the window and the gate choose among them.
µm/px and FPS are filled from the recording's own record when control measured
them, the FPS as the frames arrived and the µm/px from the walls clicked on the
preview; a typed value wins. Tick *Show detection stages* to see each detection
step on the preview frame. *Reset file settings* puts the selected file back to
the defaults and to the recording's µm/px and FPS.

To set one thing on many files, type it in the column's header: every ticked file
whose engine has that setting takes it as soon as the value is entered. The
**Columns** dropdown above the table chooses which settings are shown as columns
(µm/px, FPS, the frame range and the detection settings; the frames and crop
summaries too), and the box in the **Use** header ticks or clears every file at
once. The **Status** dot says where a file stands: grey not run, blue running,
green analysed as set up now, amber its detection settings changed since, so
**Analyse all** will run it again. Every setting column also edits per file, in its row. A value in a header
lands, for example *Detect inclusions* off on every image folder, or
one µm/px on every video. Unticked files are left alone.

## Setting an image folder up

Image folders are grouped into frames by name (`…_z01_…a01f001d4.tif`: z-slice and
frame), min-projected over z and segmented with Cellpose. Cellpose is the slow part
and runs once per folder: inclusions are always counted after it, and *Detect
inclusions* only says whether they are shown; a *Frame limit* lower than the frames
already segmented is applied when read, a higher one segments the new frames. **Slide
height** is applied when read too: a droplet wider than the chamber it is imaged
in, 130 to 160 µm in a 100 µm slide, is flattened, and its footprint reads larger
than the droplet was. With the height given, every droplet wider than it is taken
as a disc of that height with a rounded rim, and its diameter is that of the
sphere of the same volume: a 140 µm footprint in a 100 µm slide is a 130 µm
droplet, 185 µm is 160 µm. A droplet no wider than the slide is as seen, and 0
turns the correction off. Areas stay the footprints measured. The editor shows each
image with the stored droplets drawn over it: a circle per droplet with its
inclusion count. Corrections are made on the image: **click** a droplet to switch
it off (or back on), **shift-click** to add an inclusion, **alt-click** to remove
one. They are saved with the project and count on **3. Results** and in the export;
the stored rows stay as found, and *Clear corrections* drops them.

## Results

The stage is one page with one table, in the Action Panel: one line per sample
under its condition's line. Each sample has a tick per source it has, **OpenCV**
for its video's analysis, **Cellpose** for its image folder's, **Live** for what
the rig measured while the video was recorded, with that source's numbers beside
the tick: droplets, diameter, CV and rate from OpenCV; diameter, CV and
inclusions per droplet from Cellpose; rate and CV from the rig, with its state in
the rig's colour. A source with no number yet says why, *not run* or *stale*. A
tick off greys its numbers and takes that source out of every plot and the export;
the ticks are remembered. Below the table come the plots, in sections that are
there when they have data:

- **Aggregated**: diameter, CV and rate as bars, one place per sample with a bar
  per source side by side, the video's solid, the image folder's a shade lighter,
  the rig's hatched. Samples stand in the list's order under their condition, so
  replicates sit next to each other and conditions are compared at a glance.
- **OpenCV**: the ticked videos' diameter distribution, droplets, diameter and
  rate over time, positions and area along the channel.
- **Cellpose**: the ticked image folders' diameter distribution and inclusions
  per droplet.
- **Live QC + Fluidics**: the four plots of the control app's Review stage,
  stacked as its *Stacked* view: every recording ticked Live, its step from its
  own start, so steps and runs overlay. Pressure and flow per channel, the
  channel told by the line pattern, then the droplet rate and CV the rig
  measured, each recording in its sample's colour.

Every sample has a colour of its own: files of one condition wear shades of the
condition's hue, one per sample, and a sample's video, image folder and live line
share its colour. Without a condition, samples whose names end in a replicate
marker (`oil_rep2`, `oil #2`) are replicates of one group and share its hue.

## Where results go

- `analyze/results/<key>/` in the home: one folder per file and detection settings,
  with the detections (`rows.jsonl`) and the summary (`result.json`).
- `analyze/registry.json`: the folders watched and the file list as left.

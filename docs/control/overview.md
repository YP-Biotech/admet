# admet control

![admet control](/admet_control.png)

`admet control` is the desktop app that drives the rig. It owns the devices
directly — there is no background server — and only one instance runs per user.

```bash
uv run --extra control admet control
uv run --extra control admet control --home "D:\admet"
```

Startup never connects a device. Nothing moves until you click **Connect**.

## The workflow

The left-hand list (TOC) is the session, top to bottom:

| Stage | What you do there |
|---|---|
| **1. Setup** | Choose the acquisition mode; connect the camera and start its live preview. |
| **2. Fluigent connect** | Connect the controller (real or *Simulated Hardware*); polling starts. |
| **3. Calibration** | Set each channel's liquid, sensor table, correction and dead volume; **Apply All Corrections**. See [Calibration](./calibration). |
| **4. Priming** | Fill the lines before an experiment. |
| **5. Preflight** | Plan flow and phase ratios, the tubing map and liquid consumption. Nothing moves. |
| **Experiments** | One stage per protocol step you add with **+ Protocol step**; its **×** removes it. See [Running protocols](./running). |
| **Calculations** | Calculate results from finished runs. See [Calculations](./calculations). |
| **Wash** | Flush the lines afterwards. |
| **Cleanup** | Reset safety if needed, then **Disconnect Devices**. |

Above the stage content sits the **action bar** (Plan, Execute, Confirm, Pause,
Skip, Abort); below it, the live graphs and, in camera mode, the video preview.
The camera setting **Nerd mode** draws a grid of about 256 cells on the preview,
whatever the frame size, each cell lightly tinted by its mean level from blue
(lowest) to red (highest),
and a short line under it: `320x60  Mono8  avg 80.3  grid 62-97  exp 150us
gain 0dB  76.8MB/s  focus 83%` (size, format, mean level, the levels the
colours span, exposure, gain, data rate, focus). Focus is the
sharpness of the last half second against the best seen since Nerd mode or a
camera setting last changed: turn the focus until the bar is full.
Each channel also has its own manual controls — see [Manual control](./manual).

## Acquisition mode

On **1. Setup**, choose:

- **Fluidics only** (default) — records the fluidics CSV; no camera needed.
- **Camera + fluidics** — shows the camera; protocol steps marked `"record": true`
  record video. Requires a live camera.

The mode is saved with the rig's settings and frozen into each plan, so changing
it discards unexecuted previews.

## The home

Control keeps everything in one folder, the admet home: `~/.admet`, or the
folder `ADMET_HOME` names, or the one given with `--home`. The top bar shows
which one is in use. There is nothing to create or open before a session.

```text
~/.admet/
  control/rig.json   the rig's settings: acquisition mode, each channel's calibration,
                     the profiles and liquids you added, the camera's µm per pixel,
                     the preflight set-up
  data/protocols/    the saved protocol definitions
  data/runs/         one folder per run, named by date, time and the run's name; the
                     folder analyze is pointed at
  analyze/           what analyze makes
```

The rig's settings are saved as you change them and are there again at the next
start. A second rig has its own home; the data folder is what moves between
machines.

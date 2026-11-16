# admet control

![admet control](/admet_control.png)

`admet control` is the desktop app that drives the rig. It owns the devices
directly — there is no background server — and only one instance runs per user.

```bash
uv run --extra control admet control
uv run --extra control admet control --project "D:\Experiments\today.admetp"
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
| **Experiments** | One stage per protocol step you add with **+ Protocol step**. See [Running protocols](./running). |
| **Calculations** | Calculate results from finished runs. See [Calculations](./calculations). |
| **Wash** | Flush the lines afterwards. |
| **Cleanup** | Reset safety if needed, then **Disconnect Devices**. |

Above the stage content sits the **action bar** (Plan, Execute, Confirm, Pause,
Skip, Abort); below it, the live graphs and, in camera mode, the video preview.
The camera setting **Nerd mode** draws a grid every 32 pixels on the preview,
each cell lightly tinted by its mean level from blue (lowest) to red (highest),
and a line under it with the frame size, pixel format, mean pixel value, the
levels the colours span, exposure, gain, the data rate and focus. Focus is the
sharpness of the last half second against the best seen since Nerd mode or a
camera setting last changed: turn the focus until the bar is full.
Each channel also has its own manual controls — see [Manual control](./manual).

## Acquisition mode

On **1. Setup**, choose:

- **Fluidics only** (default) — records the fluidics CSV; no camera needed.
- **Camera + fluidics** — shows the camera; protocol steps marked `"record": true`
  record video. Requires a live camera.

The mode is saved in the project and frozen into each plan, so changing it
discards unexecuted previews.

## Projects

Create or open a project from the project menu. It lists recent projects with
their run counts and when you last opened them on this computer. See
[Projects](../guide/projects) for what is stored.

**Clone Project** starts the next project where this one leaves off: a new project
with the same acquisition mode, each channel's calibration (profile and dead
volume), the profiles and liquids you added, the preflight set-up and the saved
protocols — and none of the recordings, runs, analyses or media. The same from
the command line: `admet clone <source>.admetp <new>.admetp`.

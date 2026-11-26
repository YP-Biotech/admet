# Introduction

ADMET — the **Automatic Droplet Management Extended Toolkit** — drives a
microfluidics rig, records what happens on it, and analyses the results. It has three entry points, all from one `admet` command:

| Command | What it does | Touches the rig |
|---|---|---|
| `admet control` | Desktop app (Qt). Connects the flow units and camera, runs protocols, records runs, calculates results. | Yes |
| `admet analyze` | Web server (NiceGUI). Lists the recorded runs for the lab network and analyses their videos and images. | No |
| `admet describe` | Lists the operations and engines the Python API offers. | No |

## The rig

ADMET is built around a Fluigent pressure controller with three flow units, and
an optional Basler camera:

| Channel | Name | Typical use |
|---|---|---|
| 0 | Oil L | Fluorinated oil (e.g. dSurf), on an L flow unit |
| 1 | Cells M (M1) | Aqueous phase, on an M flow unit |
| 2 | Beads M (M2) | Aqueous phase, on an M flow unit |

Each channel is flow-regulated through its flow unit, or set to a fixed pressure.

## How work is organised

Everything control makes is a **run**: the protocol as it ran, the timeline of
pressures, flows and droplet QC, the videos, the measurements you typed in and
the results calculated from them. Control keeps every run in the home's
`data/control.db`, and gives each its folder under `data/runs/`: its videos and,
once it finished, its `run.db`, the run sealed on its own. Copy the folder and you
have the whole run. The rig's own
settings live beside the runs in `control/rig.json`. See
[the home](../control/overview#the-home) and [Recording](../control/running#recording).

A typical session in `admet control`:

1. Start control; it opens on the home.
2. Connect the camera (optional) and the Fluigent controller.
3. On **Calibration**, set each channel's liquid profile and **Apply All Corrections**.
4. **Prime** the lines.
5. Add protocol steps, **Plan** each one, review it, and **Execute**.
6. Type measurements (masses, volumes) into the run's table as you go.
7. On **Calculations**, calculate results from the finished runs.
8. **Wash**, then **Cleanup** to release the devices.

`admet analyze` then reads the recorded videos and images from the home's data
folder, where every run is a folder of its own.

## What ADMET does not do

- It never applies a calculated correction to the rig by itself. Calculations
  suggest; you enter the values on the Calibration page.
- It never guesses missing data. A step that was paused, skipped or recorded
  with gaps is left out or reported, not filled in.
- It does not replace the physical emergency stop. Keep it within reach.

## Background

ADMET extends the idea of Automated Droplet Measurement (ADM), video processing
software for rapid droplet measurements:

> Z. Z. Chong, S. B. Tor, A. M. Gañán-Calvo, Z. J. Chong, N. H. Loh, N.-T. Nguyen, S. H. Tan.
> Automated droplet measurement (ADM): an enhanced video processing software for rapid droplet
> measurements. *Microfluidics and Nanofluidics* **20**, 66 (2016).
> [doi:10.1007/s10404-016-1722-5](https://doi.org/10.1007/s10404-016-1722-5)

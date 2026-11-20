# Projects

A project is a folder ending in `.admetp`, the format earlier versions recorded
into. Neither `admet control` nor `admet analyze` writes or reads one any more;
`admet migrate` lays one out as run folders.

## Migrating a project

```text
admet migrate experiment.admetp              # into the home's data folder
admet migrate experiment.admetp --into D:\admet\data --copy
```

Every protocol run of the project becomes a run folder, `runs/<date>_<name>/`,
named by the run's tag or its protocol and the time it began: its summary becomes
`run.json`, with its step events; its fluidics readings and the camera's QC rows
become one `run.csv`, the QC rows put on the readings' clock; its step videos move
into the folder. A video recorded by hand becomes a recording run of its own. The
videos are moved, which is quick on the same disk; `--copy` copies them and leaves
the project whole. The analyze registry of the home watches the folder afterwards
and lists every video, so they appear on 1. Files at once. A run already laid out
is left alone, so a migration can be run again after a stop.

`admet control` no longer writes projects. It records one folder per run into the
admet home's `data/runs/` and keeps the rig's settings in `control/rig.json`; see
[the home](../control/overview#the-home) and [Recording](../control/running#recording).
`admet analyze` is pointed at that data folder, or at the export folder on the server.

```text
experiment.admetp/
  manifest.json                     # project index, rig calibration, liquids, setup
  records/
    protocols/run_<id>/             # one folder per executed protocol
      protocol.json                 # the protocol as it ran, with its parameter values
      plan.json                     # the exact resolved steps that executed
      events.jsonl                  # every step start, gate, pause, skip and stop
      summary.json                  # outcome, rig mapping, corrections, recording paths
      measurements.json             # values typed in during the run, when declared
      calculations/                 # one file per calculated result
    fluidics/<run>.csv              # pressure and flow of every channel over time
    camera/<run>_stepNN_<time>.avi  # video of a step with "record": true
  analysis/                         # what admet analyze ran on this project
```

## What is saved, and when

| What | Saved when |
|---|---|
| A protocol and its recording | **Execute** — previews and edits are never saved |
| Measurements | As you type them into the run's table |
| Calculation results | Each **Calculate**, as a new file; earlier results stay |
| Analysis settings and results | When `admet analyze` runs or saves the matrix |

Protocol edits and plan previews live in memory only. Closing the app discards
them; only an executed run is archived.

## Results stay honest

Every calculation result records the version of the calculator, the measurement
revision and a fingerprint (SHA-256) of each input file. If a measurement or
recording changes afterwards, the result is marked **outdated** — it is kept, not
deleted, and you calculate again.

Recordings are referenced relative to the project, so a moved or copied project
still finds them.

## Older projects

ADMET reads only run folders. Projects from earlier versions are not read through
compatibility code; migrate them first, as above.

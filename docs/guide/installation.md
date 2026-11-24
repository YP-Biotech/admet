# Installation

ADMET needs **Python 3.12** and [uv](https://docs.astral.sh/uv/). The core
package has no user interface; each app is an optional extra, so a control PC
installs only Qt and an analysis server only its web and ML stack.

| Extra | For | Pulls in |
|---|---|---|
| `control` | `admet control` | PySide6, pyqtgraph, OpenCV, pypylon |
| `analyze` | `admet analyze` | NiceGUI, Cellpose, PyTorch, OpenCV, SciPy |

## macOS and Linux

```bash
git clone https://github.com/merv1n34k/admet.git
cd admet
uv python install 3.12
uv sync --locked --extra control      # or --extra analyze, or --all-extras
uv run --locked --extra control admet control
```

## Windows (control PC)

Install Git and uv from PowerShell, then reopen PowerShell so both are on `PATH`:

```powershell
winget install --id Git.Git -e --source winget
winget install --id astral-sh.uv -e --source winget
```

```powershell
git clone https://github.com/merv1n34k/admet.git
cd admet
uv python install 3.12
uv sync --locked --extra control
uv run --locked --extra control admet control
```

To update an existing clone, finish any run and close ADMET first:

```powershell
git pull --ff-only
uv sync --locked --extra control
```

Never copy a `.venv` from another machine; sync a fresh one on each PC.

## Device drivers

For live hardware, install the vendor drivers for your instruments:

- **Fluigent** — the controller and flow-unit drivers, and
  [Fluigent's SDK](https://github.com/Fluigent/fgt-SDK). Install the SDK with
  `uv run admet install-sdk`, which downloads it from Fluigent's GitHub into
  ADMET's vendor folder. To use an SDK copy elsewhere instead, set
  `ADMET_FLUIGENT_SDK_PATH` to its Python folder (the one containing
  `Fluigent/SDK`); that copy then takes precedence.
- **Basler** — the pylon runtime for the camera. Only needed for
  *Camera + fluidics* acquisition.

::: warning macOS on Apple Silicon
The Fluigent SDK has no native support for macOS on Apple Silicon. Use a Windows
or Linux PC to drive a Fluigent rig.
:::

Without hardware, `admet control` can still connect to a **simulated** Fluigent
controller (Fluigent page → *Simulated Hardware*). The camera is not simulated.

## admet analyze as a service

On a Linux server, run analyze as a systemd user service, so it starts with the
machine, survives your logout and is restarted if it stops:

```bash
make systemd                                          # home ~/admet, port 8080
make systemd ADMET_HOME=/srv/admet ADMET_DATA=/srv/nas/ADMET/data ANALYZE_PORT=9000
```

It installs the analyze extra, writes `~/.config/systemd/user/admet-analyze.service`
from `deploy/systemd/admet-analyze.service` with this checkout and the values
given, enables lingering for your user so the service runs while you are logged
out, and starts it. `ADMET_DATA` adds another folder of runs beside the home's data
folder, for example a NAS share. After a `git pull`, run `make systemd` again to
sync and restart; to watch it:

```bash
systemctl --user status admet-analyze
journalctl --user -u admet-analyze -f
```

## Where the data lives

`admet control` keeps its things in the admet home, `~/.admet` unless
`ADMET_HOME` or `--home` says otherwise; see [the home](../control/overview#the-home).
Every run it records is a folder under the home's `data/runs/`.

`admet analyze` watches the home's data folder from the start, so the runs are
listed without a step of your own; a folder added on its Files page, or given
with `--data`, is watched beside it. Its registry and results live in the home's
`analyze/`.

Older `.admetp` projects are not read; `admet migrate <project>` lays one out as
run folders in the home's data folder, see [Projects](projects#migrating-a-project).

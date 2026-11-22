# Command line

Everything starts from one command, `admet`.

```text
admet control  [--home PATH]
admet analyze  [--host HOST] [--port PORT] [--data DIR] [--home PATH]
admet migrate  PROJECT [--into DIR] [--copy] [--rig] [--home PATH]
admet describe [TARGET]
admet install-sdk [--version TAG] [--force]
```

## admet control

Opens the control desktop. It keeps the rig's settings and records every run in
the admet home, `~/.admet` unless `ADMET_HOME` or `--home` says otherwise; see
[the home](../control/overview#the-home). Requires the `control` extra.

```bash
uv run --extra control admet control
uv run --extra control admet control --home D:\admet
```

## admet analyze

Serves the analysis over the network. It watches the home's data folder from the
start, so every run the rig recorded is listed; `--data` adds another folder of
runs, for example the export folder on the server. Requires the `analyze` extra.

| Option | Default | Meaning |
|---|---|---|
| `--host` | `0.0.0.0` | Address to listen on (every interface) |
| `--port` | `8080` | Port |
| `--data` | none | Another folder of runs to watch beside the home's data folder |
| `--home` | `ADMET_HOME` or `~/.admet` | The home whose data folder is watched and whose `analyze/` room keeps the registry and the results |

```bash
uv run --extra analyze admet analyze
uv run --extra analyze admet analyze --home /Volumes/data/admet --port 9000
```

## admet migrate

Lays an old `.admetp` project out as run folders in the home's data folder; see
[Projects](../guide/projects#migrating-a-project).

```bash
uv run admet migrate experiment.admetp
uv run admet migrate experiment.admetp --rig        # and take its rig settings into the home
```

## admet describe

Lists what the Python API offers: every operation, or one operation or engine.

```bash
uv run admet describe              # everything
uv run admet describe run_steps    # one operation: its settings and triggers
uv run admet describe acquisition  # one engine
```

## admet install-sdk

Downloads Fluigent's SDK from its official GitHub into ADMET's vendor folder.

```bash
uv run admet install-sdk                    # the release ADMET is tested with
uv run admet install-sdk --version 23.0.0   # another fgt-SDK release tag
uv run admet install-sdk --force            # replace an SDK already there
```

## Environment variables

| Variable | Meaning |
|---|---|
| `ADMET_HOME` | The admet home, instead of `~/.admet` |
| `ADMET_FLUIGENT_SDK_PATH` | The Python folder of a Fluigent SDK to use (the one containing `Fluigent/SDK`); it takes precedence over the vendored copy |

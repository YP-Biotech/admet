# Command line

Everything starts from one command, `admet`.

```text
admet control  [--home PATH]
admet analyze  [--host HOST] [--port PORT] [--data DIR] [--home PATH]
admet rig      [--home PATH]
admet export   --tar FILE [--run RUN_ID ...] [--home PATH]
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
| `--data` | none | Another folder to watch beside the home's data folder: runs, archives of runs (`.tar`), folders of images named with a sample's id |
| `--home` | `ADMET_HOME` or `~/.admet` | The home whose data folder is watched and whose `analyze/` room keeps the registry and the results |

```bash
uv run --extra analyze admet analyze
uv run --extra analyze admet analyze --home /Volumes/data/admet --port 9000
```

## admet rig

Shows the home's rig settings: where `rig.json` is and each setting it keeps.

```bash
uv run admet rig
```

## admet export

Puts finished runs into one tar archive, each run's folder as the server takes it:
its videos and its sealed `run.db`. Copy the archive to a server the rig cannot reach
and let analyze take it in. Nothing on the rig is removed; runs still going or still
encoding are left out and named.

```bash
uv run admet export --tar runs.tar
uv run admet export --tar k7q.tar --run 20261126_140000_k7q_03
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

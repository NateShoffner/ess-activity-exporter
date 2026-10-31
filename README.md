# ess-activity-exporter

Exports Elastic Cloud deployment plan history (the console **Activity** page) to local files.

## Usage

Requires [uv](https://docs.astral.sh/uv/).

```
uv run ess_activity_exporter.py [--csv] [--full] [--show] [-o DIR]
                                [--deployment ID ...] [--base-url URL]
                                [--api-key-env VAR ...] [--env-file PATH ...]
```

Auth: `EC_API_KEY` (a `.env` in the current or a parent directory is auto-loaded; `--env-file PATH` loads a specific file instead). Existing env vars win. Keys are org-scoped; for multiple organizations set `EC_API_KEYS=key1,key2` or repeat `--api-key-env VAR`. Bad keys and failed fetches abort the whole run with a named error.

API URL: defaults to `https://api.elastic-cloud.com`. Override with `--base-url URL` or `EC_BASE_URL`.

Output in `export/`: `raw/<deployment_id>.json` (verbatim responses), `activity.ndjson` (one row per plan attempt), optional `activity.csv`, and `state.json`. Runs are incremental by default; `--full` re-exports everything. `--show` also prints the exported plan changes to stdout, one line each.

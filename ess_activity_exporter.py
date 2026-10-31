import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import List, Optional

import requests
import typer
from dotenv import load_dotenv
from pydantic import ValidationError

from models import ActivityRow, Deployment, PlanAttempt

DEFAULT_BASE_URL = "https://api.elastic-cloud.com"
DEPLOYMENT_QUERY = (
    "show_plan_history=true&force_all_plan_history=true"
    "&show_plan_logs=true&show_plans=true&show_settings=false"
)
MAX_RETRIES = 6
INSTANCE_NAME_RE = re.compile(r"\binstance-\d+\b")

app = typer.Typer(add_completion=False)

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("ess-activity-exporter")


class ApiError(Exception):
    pass


class AuthError(ApiError):
    pass


def api_get(session, base_url, path):
    url = base_url.rstrip("/") + path
    delay = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=120)
        except requests.RequestException as e:
            if attempt == MAX_RETRIES:
                raise ApiError(f"network error on {url}: {e}") from e
            logger.warning("network error on %s (%s), retrying in %.0fs", path, e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
            retry_after = resp.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else delay
            logger.warning("HTTP %s on %s, retrying in %.0fs (attempt %d/%d)",
                           resp.status_code, path, wait, attempt, MAX_RETRIES)
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue
        return resp.status_code, resp.text
    raise ApiError(f"exhausted retries on {url}")


def check_auth_error(status, body, key_label):
    if status == 401:
        try:
            codes = [e.get("code") for e in json.loads(body).get("errors", [])]
        except (json.JSONDecodeError, AttributeError):
            codes = []
        detail = ", ".join(c for c in codes if c) or "unauthorized"
        raise AuthError(f"API key {key_label} was rejected (HTTP 401: {detail}); "
                        "generate a new one at https://cloud.elastic.co/account/keys")
    if status == 403:
        raise AuthError(f"API key {key_label} lacks permission (HTTP 403): {body[:300]}")


def make_session(api_key):
    session = requests.Session()
    session.headers.update({"Authorization": "ApiKey " + api_key, "Accept": "application/json"})
    return session


def list_deployments(session, base_url, key_label):
    status, body = api_get(session, base_url, "/api/v1/deployments")
    check_auth_error(status, body, key_label)
    if status != 200:
        raise ApiError(f"GET /api/v1/deployments with key {key_label} "
                       f"returned HTTP {status}: {body[:300]}")
    doc = json.loads(body)
    # The list is unpaginated today; refuse a partial list if that ever changes.
    paging = {"next", "cursor", "next_page", "match_count"} & doc.keys()
    if paging:
        raise ApiError(f"deployments list for key {key_label} has pagination fields "
                       f"{sorted(paging)}; refusing to continue with a possibly partial list")
    return doc.get("deployments", [])


def fetch_deployment(session, base_url, key_label, dep_id):
    status, body = api_get(session, base_url, f"/api/v1/deployments/{dep_id}?{DEPLOYMENT_QUERY}")
    check_auth_error(status, body, key_label)
    if status != 200:
        raise ApiError(f"GET deployment {dep_id} returned HTTP {status}: {body[:300]}")
    return body


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def collect_log_instance_names(attempt: PlanAttempt) -> List[str]:
    names = set()
    for step in attempt.plan_attempt_log:
        for entry in step.info_log:
            for field in ("message", "details", "failure_type"):
                if isinstance(entry.get(field), str):
                    names.update(INSTANCE_NAME_RE.findall(entry[field]))
    return sorted(names)


def flatten_attempt(dep_id, dep_name, kind, ref_id, attempt: PlanAttempt) -> ActivityRow:
    transient = attempt.plan.transient
    source = attempt.source
    move_allocators = [m.from_ for m in transient.plan_configuration.move_allocators if m.from_]
    move_instances = [m.from_ for m in transient.plan_configuration.move_instances if m.from_]

    if source.user_id:
        initiated_by = "user"
    elif source.admin_id:
        initiated_by = "system"
    else:
        initiated_by = "unknown"

    # The API has no textual description; synthesize what the console shows.
    if move_allocators:
        description = "Move instances off allocator(s) " + ", ".join(move_allocators)
    elif move_instances:
        description = "Move instance(s) " + ", ".join(move_instances)
    else:
        description = source.action or ""

    start = parse_ts(attempt.attempt_start_time)
    end = parse_ts(attempt.attempt_end_time)
    error = attempt.error or {}
    failed_steps = [s.step_id or "" for s in attempt.plan_attempt_log
                    if s.status not in (None, "success", "pending")]

    return ActivityRow(
        deployment_name=dep_name,
        deployment_id=dep_id,
        resource_kind=kind,
        resource_ref_id=ref_id,
        plan_attempt_name=attempt.plan_attempt_name,
        plan_attempt_id=attempt.plan_attempt_id,
        attempt_start_time=attempt.attempt_start_time,
        attempt_end_time=attempt.attempt_end_time,
        duration_seconds=round((end - start).total_seconds(), 3) if start and end else None,
        healthy=attempt.healthy,
        source_action=source.action,
        source_user=source.user_id or source.admin_id,
        source_facilitator=source.facilitator,
        source_date=source.date,
        initiated_by=initiated_by,
        plan_strategy=next(iter(transient.strategy), None),
        description=description,
        move_allocator_ids=move_allocators,
        move_instance_names=move_instances,
        log_instance_names=collect_log_instance_names(attempt),
        version=attempt.plan.version(kind),
        error_failure_type=error.get("failure_type"),
        error_message=(error.get("message") or json.dumps(error)) if error else None,
        warnings_count=len(attempt.warnings),
        steps_total=len(attempt.plan_attempt_log),
        steps_failed=len(failed_steps),
        failed_step_ids=failed_steps,
    )


def iter_attempts(deployment: Deployment):
    for kind, resources in deployment.resources.items():
        for res in resources:
            ref_id = res.ref_id or res.info.id
            plan_info = res.info.plan_info
            for attempt in plan_info.history:
                yield kind, ref_id, attempt
            # current duplicates the newest history entry unless the plan is still in flight.
            current = plan_info.current
            if current and current.plan_attempt_id not in {a.plan_attempt_id for a in plan_info.history}:
                yield kind, ref_id, current


def load_state(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"deployments": {}}


def regenerate_csv(ndjson_path, csv_path):
    with open(ndjson_path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    rows.sort(key=lambda r: (r.get("deployment_name") or "", r.get("attempt_start_time") or ""))
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(ActivityRow.model_fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ";".join(v) if isinstance(v, list) else v for k, v in row.items()})
    return len(rows)


def format_change(row: ActivityRow) -> str:
    # Mirror the console's "#kibana-41" label.
    number = (row.plan_attempt_name or "").rpartition("-")[2].lstrip("0") or "?"
    outcome = {True: "ok", False: "FAILED"}.get(row.healthy, "pending")
    duration = f"{row.duration_seconds:.0f}s" if row.duration_seconds is not None else "-"
    parts = [row.attempt_start_time or "-", row.deployment_name, f"#{row.resource_kind}-{number}",
             row.initiated_by, outcome, row.version or "-", duration, row.description]
    if row.error_failure_type or row.error_message:
        parts.append(f"error: {row.error_failure_type or row.error_message}")
    return "  ".join(p for p in parts if p)


def resolve_api_keys(api_key_env):
    if api_key_env:
        keys = []
        for var in api_key_env:
            val = os.environ.get(var, "").strip()
            if not val:
                logger.error("--api-key-env %s is not set or empty", var)
                raise typer.Exit(2)
            keys.append((f"${var}", val))
        return keys
    multi = [p.strip() for p in os.environ.get("EC_API_KEYS", "").split(",") if p.strip()]
    if multi:
        return [(f"$EC_API_KEYS[{i}]", key) for i, key in enumerate(multi)]
    single = os.environ.get("EC_API_KEY", "").strip()
    if single:
        return [("$EC_API_KEY", single)]
    logger.error("no API key: set EC_API_KEY, EC_API_KEYS=key1,key2, or pass --api-key-env VAR")
    raise typer.Exit(2)


@app.command()
def export(
    base_url: str = typer.Option(
        None, "--base-url",
        help="API base URL (default: EC_BASE_URL or https://api.elastic-cloud.com)"),
    output_dir: str = typer.Option("export", "--output-dir", "-o", help="output directory"),
    api_key_env: Optional[List[str]] = typer.Option(
        None, "--api-key-env", metavar="VAR",
        help="env var holding an API key; repeat once per organization "
             "(default: EC_API_KEY, or EC_API_KEYS comma-separated)"),
    full: bool = typer.Option(False, "--full", help="ignore stored state and re-export everything"),
    csv_out: bool = typer.Option(False, "--csv", help="also write activity.csv"),
    show: bool = typer.Option(False, "--show", help="print the exported plan changes to stdout"),
    deployment: Optional[List[str]] = typer.Option(
        None, "--deployment", metavar="ID", help="limit to specific deployment id(s)"),
    env_file: Optional[List[str]] = typer.Option(
        None, "--env-file", metavar="PATH",
        help="load env vars from this .env file; repeatable "
             "(default: .env in the current or a parent directory)"),
):
    """Export plan history to raw JSON + flattened NDJSON/CSV."""
    for path in env_file or []:
        if not os.path.isfile(path):
            logger.error("--env-file %s does not exist", path)
            raise typer.Exit(2)
        load_dotenv(path)
    if not env_file:
        load_dotenv()
    if base_url is None:
        base_url = os.environ.get("EC_BASE_URL", DEFAULT_BASE_URL)
    api_keys = resolve_api_keys(api_key_env)
    raw_dir = os.path.join(output_dir, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    state_path = os.path.join(output_dir, "state.json")
    ndjson_path = os.path.join(output_dir, "activity.ndjson")

    # Every key must authenticate before anything is written, so a dead key
    # fails the run instead of silently shrinking the export.
    merged = {}
    for label, key in api_keys:
        session = make_session(key)
        try:
            deps = list_deployments(session, base_url, label)
        except AuthError as e:
            logger.error("%s", e)
            raise typer.Exit(2)
        logger.info("key %s: %d deployment(s) visible", label, len(deps))
        for d in deps:
            merged.setdefault(d["id"], (label, session, d))
    if deployment:
        missing = set(deployment) - set(merged)
        if missing:
            logger.error("deployment id(s) not visible to any provided key: %s", sorted(missing))
            raise typer.Exit(2)
        merged = {k: v for k, v in merged.items() if k in deployment}
    if not merged:
        logger.error("no deployments visible to the provided key(s); if they live in another "
                     "organization, add that organization's key")
        raise typer.Exit(2)

    state = {"deployments": {}} if full else load_state(state_path)
    if full and os.path.exists(ndjson_path):
        os.remove(ndjson_path)

    new_rows: List[ActivityRow] = []
    failures = []
    for dep_id, (label, session, listing) in merged.items():
        dep_name = listing.get("name", dep_id)
        logger.info("fetching %s (%s) via %s ...", dep_name, dep_id, label)
        try:
            body = fetch_deployment(session, base_url, label, dep_id)
            doc = Deployment.model_validate_json(body)
        except ApiError as e:
            failures.append(f"{dep_name} ({dep_id}): {e}")
            continue
        except ValidationError as e:
            failures.append(f"{dep_name} ({dep_id}): unexpected response shape: {e}")
            continue
        with open(os.path.join(raw_dir, f"{dep_id}.json"), "w", encoding="utf-8") as f:
            f.write(body)

        dep_state = state["deployments"].setdefault(dep_id, {})
        thresholds = dict(dep_state)
        for kind, ref_id, attempt in iter_attempts(doc):
            res_key = f"{kind}/{ref_id}"
            start = attempt.attempt_start_time or ""
            if not full and start and start <= thresholds.get(res_key, ""):
                continue
            new_rows.append(flatten_attempt(dep_id, dep_name, kind, ref_id, attempt))
            dep_state[res_key] = max(start, dep_state.get(res_key, ""))

    if failures:
        logger.error("export aborted, nothing saved; a missing deployment is indistinguishable "
                     "from one with no activity:")
        for failure in failures:
            logger.error("  - %s", failure)
        raise typer.Exit(1)

    new_rows.sort(key=lambda r: (r.deployment_name, r.attempt_start_time or ""))
    if show:
        for row in new_rows:
            typer.echo(format_change(row))
    with open(ndjson_path, "a", encoding="utf-8") as f:
        for row in new_rows:
            f.write(row.model_dump_json() + "\n")
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    logger.info("exported %d new plan attempt(s) across %d deployment(s) -> %s",
                len(new_rows), len(merged), ndjson_path)
    if csv_out:
        total = regenerate_csv(ndjson_path, os.path.join(output_dir, "activity.csv"))
        logger.info("wrote activity.csv (%d total rows)", total)


if __name__ == "__main__":
    try:
        app()
    except ApiError as e:
        logger.error("%s", e)
        sys.exit(1)

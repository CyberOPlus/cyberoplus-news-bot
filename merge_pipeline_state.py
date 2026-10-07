#!/usr/bin/env python3
"""Merge a pipeline run's local data snapshot into the latest main-branch data.

The workflow copies its post-run data files into a temporary snapshot, resets to
origin/main, then calls this script. This avoids losing successful Facebook
publishes when main changed while the job was running.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
FILES = (
    "state.json",
    "inbox.jsonl",
    "ai_state.json",
    "ready.jsonl",
    "facebook_events.jsonl",
)


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else dict(default)
    except Exception:
        return dict(default)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        value = json.loads(raw)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def row_score(row: dict[str, Any]) -> tuple[int, int, int]:
    truthy = sum(1 for value in row.values() if value not in (None, "", [], {}))
    return int(row.get("editorial_version", 0)), truthy, len(json.dumps(row, ensure_ascii=False, sort_keys=True))


def tid_key(row: dict[str, Any]) -> tuple[str, Any]:
    try:
        tid = int(row.get("telegram_id", 0) or 0)
    except (TypeError, ValueError):
        tid = 0
    if tid > 0:
        return ("telegram_id", tid)
    return ("raw", json.dumps(row, ensure_ascii=False, sort_keys=True))


def event_key(row: dict[str, Any]) -> tuple[Any, ...]:
    event = str(row.get("event") or "")
    try:
        tid = int(row.get("telegram_id", 0) or 0)
    except (TypeError, ValueError):
        tid = 0
    if event and tid > 0:
        return (event, tid)
    return ("raw", json.dumps(row, ensure_ascii=False, sort_keys=True))


def merge_rows(
    remote_rows: list[dict[str, Any]],
    local_rows: list[dict[str, Any]],
    *,
    key_fn,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    positions: dict[tuple[Any, ...], int] = {}

    for row in [*remote_rows, *local_rows]:
        key = key_fn(row)
        if key not in positions:
            positions[key] = len(merged)
            merged.append(row)
            continue

        index = positions[key]
        if row_score(row) > row_score(merged[index]):
            merged[index] = row

    return merged


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")


def merge_state(remote: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    r_id = int(remote.get("last_seen_id", 0) or 0)
    l_id = int(local.get("last_seen_id", 0) or 0)
    if l_id > r_id:
        return local
    if r_id > l_id:
        return remote

    r_time = str(remote.get("updated_at") or "")
    l_time = str(local.get("updated_at") or "")
    return local if l_time >= r_time else remote


def merge_ai_state(remote: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    r_last = int(remote.get("last_processed_id", 0) or 0)
    l_last = int(local.get("last_processed_id", 0) or 0)
    r_count = int(remote.get("processed_count", 0) or 0)
    l_count = int(local.get("processed_count", 0) or 0)

    newer = local if str(local.get("updated_at") or "") >= str(remote.get("updated_at") or "") else remote
    return {
        "last_processed_id": max(r_last, l_last),
        "processed_count": max(r_count, l_count),
        "updated_at": newer.get("updated_at"),
    }


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: merge_pipeline_state.py SNAPSHOT_DIR")

    snapshot = Path(sys.argv[1])
    if not snapshot.exists():
        raise SystemExit(f"snapshot directory does not exist: {snapshot}")

    remote_state = read_json(DATA / "state.json", {})
    local_state = read_json(snapshot / "state.json", {})
    (DATA / "state.json").write_text(
        json.dumps(merge_state(remote_state, local_state), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    remote_ai = read_json(DATA / "ai_state.json", {})
    local_ai = read_json(snapshot / "ai_state.json", {})
    (DATA / "ai_state.json").write_text(
        json.dumps(merge_ai_state(remote_ai, local_ai), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    write_jsonl(
        DATA / "inbox.jsonl",
        merge_rows(
            read_jsonl(DATA / "inbox.jsonl"),
            read_jsonl(snapshot / "inbox.jsonl"),
            key_fn=tid_key,
        ),
    )
    write_jsonl(
        DATA / "ready.jsonl",
        merge_rows(
            read_jsonl(DATA / "ready.jsonl"),
            read_jsonl(snapshot / "ready.jsonl"),
            key_fn=tid_key,
        ),
    )
    write_jsonl(
        DATA / "facebook_events.jsonl",
        merge_rows(
            read_jsonl(DATA / "facebook_events.jsonl"),
            read_jsonl(snapshot / "facebook_events.jsonl"),
            key_fn=event_key,
        ),
    )

    print(json.dumps({"status": "merged", "snapshot": str(snapshot)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

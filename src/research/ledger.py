"""Append-only record of every research run.

Two jobs: make results reproducible (code commit + data fingerprint + config),
and keep research honest — it counts how many strategy families have been tried
(feeding the multiple-testing adjustment) and refuses to evaluate the same
holdout sessions twice for a family.
"""
from __future__ import annotations

import json
import secrets
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=5)
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "src", "config"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return out.stdout.strip() + ("-dirty" if dirty else "") if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


class HoldoutReuse(RuntimeError):
    pass


class Ledger:
    def __init__(self, path: str | Path = "research/ledger.jsonl") -> None:
        self.path = Path(path)

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "commit": git_commit(), **record}
        with self.path.open("a") as fh:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")

    def families_tested(self, including: list[str] | None = None) -> int:
        fams = {r["family"] for r in self.records() if r.get("kind") == "walk_forward"}
        fams.update(including or [])
        return max(1, len(fams))

    def assert_holdout_unused(self, family: str, start: date, end: date) -> None:
        for r in self.records():
            if r.get("kind") != "holdout" or r.get("family") != family:
                continue
            s, e = date.fromisoformat(r["holdout_start"]), date.fromisoformat(r["holdout_end"])
            if s <= end and start <= e:
                raise HoldoutReuse(
                    f"{family}: holdout {start}..{end} overlaps sessions already used as "
                    f"holdout in run {r.get('run_id')} ({s}..{e}). Re-using them would "
                    f"make the holdout in-sample. Wait for new sessions.")

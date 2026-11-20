"""Run bookkeeping shared by all collectors."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.store import Store

OSLO = ZoneInfo("Europe/Oslo")
PACIFIC = ZoneInfo("America/Los_Angeles")  # GSC reports days in US Pacific time


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat() if dt else None


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def log(msg: str) -> None:
    print(f"[{iso(now_utc())}] {msg}", flush=True)


class Run:
    """Context for one collector run; persisted to collector_runs."""

    def __init__(self, store: Store, name: str) -> None:
        self.store, self.name = store, name
        self.started = now_utc()
        self.id = f"{name}|{iso(self.started)}"
        self.counts: dict[str, int] = {}
        self.errors: list[str] = []
        self.notes: list[str] = []
        self.status = "running"
        self._save()

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def error(self, msg: str) -> None:
        log(f"{self.name}: ERROR {msg}")
        self.errors.append(msg[:400])

    def note(self, msg: str) -> None:
        log(f"{self.name}: {msg}")
        self.notes.append(msg[:400])

    def finish(self, status: str | None = None) -> dict:
        self.status = status or ("ok" if not self.errors else "partial")
        self._save(finished=iso(now_utc()))
        log(f"{self.name}: {self.status} {self.counts} errors={len(self.errors)}")
        return self.doc()

    def doc(self) -> dict:
        return {"collector": self.name, "started": iso(self.started), "status": self.status,
                "counts": self.counts, "errors": self.errors[-50:], "notes": self.notes[-50:]}

    def _save(self, **extra) -> None:
        self.store.put("collector_runs", self.id.replace("/", "_"), {**self.doc(), **extra}, merge=True)

    # Per-collector persistent state (e.g. last GSC date fetched).
    def state(self) -> dict:
        return self.store.get("collector_runs", f"state_{self.name}") or {}

    def save_state(self, **values) -> None:
        self.store.put("collector_runs", f"state_{self.name}", {"collector": self.name, "kind": "state", **values})

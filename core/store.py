"""One store for every collector and the app: Firestore in the cloud, JSON files locally.

Collections mirror the spec's tables:
  url_inventory   one doc per URL (join key = core.urls.normalize), plus the indexing register
  index_status    URL x engine x day
  gsc_daily       day x page x query x country x device   (Search Analytics, query rows)
  gsc_page_daily  day x page                              (page totals; include anonymised queries)
  bing_daily      day x kind(site|page|query) x key       (Bing Webmaster stats)
  clarity_daily   day x URL x channel                     (Clarity live-insights snapshot)
  clarity_raw     day x request                           (verbatim export responses; window is 1-3 days)
  webflow_pages   one doc per Webflow page x locale
  changes         event: publish | submit | fix | metadata
  actions         alert -> action queue row
  collector_runs  one doc per collector run (status, counts, errors) + collector state
  settings        app settings edited from the dashboard (settings/exclusions: intentional exclusions)

Writes are upserts keyed by deterministic ids, so re-fetching an overlapping window is idempotent.
Raw daily rows only; 7/28/90-day figures are computed by the app (spec: aggregates in views).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable

from core import config

Where = tuple[str, str, Any]  # (field, op, value); op in ==, !=, <, <=, >, >=, in

COLLECTIONS = (
    "url_inventory", "index_status", "gsc_daily", "gsc_page_daily", "bing_daily",
    "clarity_daily", "clarity_raw", "webflow_pages", "changes", "actions", "collector_runs", "settings",
)


def _match(doc: dict, where: Iterable[Where]) -> bool:
    for field, op, value in where:
        v = doc.get(field)
        if op == "==" and v != value:
            return False
        if op == "!=" and v == value:
            return False
        if op == "in" and v not in value:
            return False
        if op in ("<", "<=", ">", ">="):
            if v is None:
                return False
            if op == "<" and not v < value:
                return False
            if op == "<=" and not v <= value:
                return False
            if op == ">" and not v > value:
                return False
            if op == ">=" and not v >= value:
                return False
    return True


def safe_id(doc_id: str) -> str:
    """Firestore reads '/' in an id as a path separator. Ids such as URLs are hashed instead;
    callers keep the readable key in a field (e.g. url). Both backends use this, so behaviour matches."""
    if "/" in doc_id or doc_id in (".", "..") or len(doc_id.encode()) > 1400:
        return hashlib.sha1(doc_id.encode("utf-8")).hexdigest()
    return doc_id


class Store:
    def put(self, collection: str, doc_id: str, data: dict, merge: bool = True) -> None: ...
    def put_many(self, collection: str, docs: dict[str, dict]) -> int: ...
    def get(self, collection: str, doc_id: str) -> dict | None: ...
    def query(self, collection: str, where: Iterable[Where] = (), order_by: str | None = None,
              descending: bool = False, limit: int | None = None) -> list[dict]: ...
    def delete(self, collection: str, doc_id: str) -> None: ...


class JsonStore(Store):
    """Development backend: one JSON file per collection, {doc_id: doc}."""

    def __init__(self, directory: str | os.PathLike = config.JSON_STORE_DIR) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict[str, dict]] = {}
        self._lock = threading.Lock()

    def _load(self, collection: str) -> dict[str, dict]:
        if collection not in self._cache:
            f = self.dir / f"{collection}.json"
            self._cache[collection] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        return self._cache[collection]

    def _save(self, collection: str) -> None:
        f = self.dir / f"{collection}.json"
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cache[collection], ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(f)

    def put(self, collection: str, doc_id: str, data: dict, merge: bool = True) -> None:
        doc_id = safe_id(doc_id)
        with self._lock:
            docs = self._load(collection)
            docs[doc_id] = {**docs.get(doc_id, {}), **data} if merge else dict(data)
            docs[doc_id]["_id"] = doc_id
            self._save(collection)

    def put_many(self, collection: str, docs: dict[str, dict]) -> int:
        with self._lock:
            cur = self._load(collection)
            for k, v in docs.items():
                k = safe_id(k)
                cur[k] = {**cur.get(k, {}), **v, "_id": k}
            self._save(collection)
        return len(docs)

    def get(self, collection: str, doc_id: str) -> dict | None:
        d = self._load(collection).get(safe_id(doc_id))
        return dict(d) if d else None

    def query(self, collection, where=(), order_by=None, descending=False, limit=None):
        where = list(where)
        rows = [dict(d) for d in self._load(collection).values() if _match(d, where)]
        if order_by:
            rows.sort(key=lambda d: (d.get(order_by) is None, d.get(order_by) or ""), reverse=descending)
        return rows[:limit] if limit else rows

    def delete(self, collection: str, doc_id: str) -> None:
        with self._lock:
            self._load(collection).pop(safe_id(doc_id), None)
            self._save(collection)


class FirestoreStore(Store):
    def __init__(self, project: str = config.GCP_PROJECT, database: str = "(default)") -> None:
        from google.cloud import firestore  # type: ignore

        self._fs = firestore
        self.db = firestore.Client(project=project, database=database)

    def put(self, collection, doc_id, data, merge=True):
        doc_id = safe_id(doc_id)
        self.db.collection(collection).document(doc_id).set({**data, "_id": doc_id}, merge=merge)

    def put_many(self, collection, docs):
        n = 0
        items = [(safe_id(k), v) for k, v in docs.items()]
        for i in range(0, len(items), 400):  # batch limit is 500 writes
            batch = self.db.batch()
            for k, v in items[i:i + 400]:
                batch.set(self.db.collection(collection).document(k), {**v, "_id": k}, merge=True)
            batch.commit()
            n += len(items[i:i + 400])
        return n

    def get(self, collection, doc_id):
        snap = self.db.collection(collection).document(safe_id(doc_id)).get()
        return snap.to_dict() if snap.exists else None

    def query(self, collection, where=(), order_by=None, descending=False, limit=None):
        from google.cloud.firestore_v1.base_query import FieldFilter  # type: ignore

        q = self.db.collection(collection)
        for field, op, value in where:
            q = q.where(filter=FieldFilter(field, op, value))
        if order_by:
            q = q.order_by(order_by, direction=self._fs.Query.DESCENDING if descending else self._fs.Query.ASCENDING)
        if limit:
            q = q.limit(limit)
        return [s.to_dict() for s in q.stream()]

    def delete(self, collection, doc_id):
        self.db.collection(collection).document(safe_id(doc_id)).delete()


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = FirestoreStore() if config.STORE_BACKEND == "firestore" else JsonStore()
    return _store

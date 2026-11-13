"""The release registry: seven Delta tables in Unity Catalog.

``SparkRegistry`` is used on Databricks. ``LocalRegistry`` keeps the same data
in a JSON file and is used by the unit tests and for a dry run on a laptop.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .util import utcnow

# Release lifecycle. PUBLISHED is added to the customer design's list: it means
# "approved and copied to the wheelhouse with every hash verified".
REQUESTED, DOWNLOADING, SCANNING, TESTING = "REQUESTED", "DOWNLOADING", "SCANNING", "TESTING"
READY_FOR_APPROVAL, APPROVED, PUBLISHED = "READY_FOR_APPROVAL", "APPROVED", "PUBLISHED"
FAILED, QUARANTINED, REVOKED, RETIRED = "FAILED", "QUARANTINED", "REVOKED", "RETIRED"

TABLES: dict[str, str] = {
    "dependency_releases": """
        release_id STRING NOT NULL, channel STRING, runtime_profile STRING, status STRING,
        status_reason STRING, staging_path STRING, release_path STRING, lock_sha256 STRING,
        previous_release_id STRING, upgrade_packages STRING, gate_status STRING, test_passed BOOLEAN,
        created_at TIMESTAMP, created_by STRING, approved_by STRING, approved_at TIMESTAMP,
        published_at TIMESTAMP, revoked_at TIMESTAMP, updated_at TIMESTAMP""",
    "dependency_artifacts": """
        release_id STRING NOT NULL, package STRING, version STRING, filename STRING, sha256 STRING,
        scan_status STRING, scanned_at TIMESTAMP""",
    "dependency_findings": """
        release_id STRING NOT NULL, package STRING, version STRING, filename STRING, issue_id STRING,
        cves STRING, severity STRING, severity_source STRING, fixed_versions STRING, component STRING,
        is_violation BOOLEAN, summary STRING, source STRING, first_seen_at TIMESTAMP""",
    "dependency_exceptions": """
        exception_id STRING NOT NULL, release_id STRING, channel STRING, package STRING, version STRING,
        issue_id STRING, severity STRING, kind STRING, status STRING, opened_at TIMESTAMP,
        deadline_at TIMESTAMP, approver STRING, rationale STRING, closed_at TIMESTAMP""",
    "dependency_references": """
        channel STRING NOT NULL, wave STRING, reference_type STRING, reference_id STRING NOT NULL,
        release_id STRING, previous_release_id STRING, updated_at TIMESTAMP, updated_by STRING""",
    "dependency_consumers": """
        snapshot_at TIMESTAMP, workspace_host STRING, object_type STRING, object_id STRING,
        object_name STRING, owner STRING, policy_id STRING, library_kind STRING, library_spec STRING""",
    "dependency_events": """
        event_id STRING NOT NULL, event_time TIMESTAMP, actor STRING, action STRING, release_id STRING,
        channel STRING, details STRING""",
}


def _columns(table: str) -> list[tuple[str, str]]:
    cols = []
    for part in TABLES[table].split(","):
        name, ctype = part.split()[:2]
        cols.append((name, ctype))
    return cols


def ddl(catalog: str, schema: str) -> str:
    """The CREATE TABLE statements, as kept in sql/02_registry_tables.sql."""
    out = []
    for name, cols in TABLES.items():
        body = ",\n  ".join(" ".join(c.split()) for c in cols.split(","))
        out.append(f"CREATE TABLE IF NOT EXISTS {catalog}.{schema}.{name} (\n  {body}\n);")
    return "\n\n".join(out) + "\n"


class Registry:
    """Interface shared by SparkRegistry and LocalRegistry."""

    actor = "unknown"

    # -- storage primitives implemented by subclasses --------------------------
    def _insert(self, table: str, rows: list[dict]) -> None:
        raise NotImplementedError

    def _select(self, table: str, where: dict[str, Any] | None = None) -> list[dict]:
        raise NotImplementedError

    def _update(self, table: str, keys: dict[str, Any], fields: dict[str, Any]) -> None:
        raise NotImplementedError

    def _delete_all(self, table: str) -> None:
        raise NotImplementedError

    def init_tables(self) -> None:
        raise NotImplementedError

    # -- releases --------------------------------------------------------------
    def create_release(self, row: dict) -> None:
        now = utcnow()
        self._insert("dependency_releases", [{"created_at": now, "updated_at": now,
                                              "created_by": self.actor, **row}])

    def update_release(self, release_id: str, **fields: Any) -> None:
        self._update("dependency_releases", {"release_id": release_id},
                     {**fields, "updated_at": utcnow()})

    def get_release(self, release_id: str) -> dict | None:
        rows = self._select("dependency_releases", {"release_id": release_id})
        return rows[0] if rows else None

    def list_releases(self, channel: str | None = None, statuses: list[str] | None = None) -> list[dict]:
        rows = self._select("dependency_releases", {"channel": channel} if channel else None)
        if statuses:
            rows = [r for r in rows if r.get("status") in statuses]
        return sorted(rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)

    # -- artifacts and findings ------------------------------------------------
    def add_artifacts(self, rows: list[dict]) -> None:
        self._insert("dependency_artifacts", rows)

    def add_findings(self, rows: list[dict]) -> None:
        now = utcnow()
        self._insert("dependency_findings", [{"first_seen_at": now, **r} for r in rows])

    def list_findings(self, release_id: str) -> list[dict]:
        return self._select("dependency_findings", {"release_id": release_id})

    # -- exceptions ------------------------------------------------------------
    def add_exception(self, row: dict) -> str:
        exception_id = row.get("exception_id") or uuid.uuid4().hex
        self._insert("dependency_exceptions", [{"opened_at": utcnow(), "status": "OPEN",
                                                **row, "exception_id": exception_id}])
        return exception_id

    def list_exceptions(self, status: str | None = None, release_id: str | None = None) -> list[dict]:
        where = {k: v for k, v in {"status": status, "release_id": release_id}.items() if v}
        return self._select("dependency_exceptions", where or None)

    def update_exception(self, exception_id: str, **fields: Any) -> None:
        self._update("dependency_exceptions", {"exception_id": exception_id}, fields)

    # -- compute references ----------------------------------------------------
    def list_references(self, channel: str | None = None) -> list[dict]:
        return self._select("dependency_references", {"channel": channel} if channel else None)

    def set_reference(self, channel: str, wave: str, reference_type: str, reference_id: str,
                      release_id: str, previous_release_id: str | None) -> None:
        keys = {"channel": channel, "reference_type": reference_type, "reference_id": reference_id}
        fields = {"wave": wave, "release_id": release_id, "previous_release_id": previous_release_id,
                  "updated_at": utcnow(), "updated_by": self.actor}
        if self._select("dependency_references", keys):
            self._update("dependency_references", keys, fields)
        else:
            self._insert("dependency_references", [{**keys, **fields}])

    def releases_in_use(self) -> list[str]:
        return sorted({r["release_id"] for r in self.list_references() if r.get("release_id")})

    # -- consumers and events --------------------------------------------------
    def replace_consumers(self, rows: list[dict]) -> None:
        self._delete_all("dependency_consumers")
        if rows:
            self._insert("dependency_consumers", rows)

    def add_event(self, action: str, release_id: str = "", channel: str = "",
                  details: dict | None = None) -> None:
        self._insert("dependency_events", [{
            "event_id": uuid.uuid4().hex, "event_time": utcnow(), "actor": self.actor,
            "action": action, "release_id": release_id, "channel": channel,
            "details": json.dumps(details or {}, default=str, sort_keys=True),
        }])

    def list_events(self, release_id: str | None = None) -> list[dict]:
        rows = self._select("dependency_events", {"release_id": release_id} if release_id else None)
        return sorted(rows, key=lambda r: str(r.get("event_time") or ""))


class LocalRegistry(Registry):
    """JSON-file registry for tests and laptop dry runs. Not for production."""

    def __init__(self, path: str | Path | None = None, actor: str = "local"):
        self.path = Path(path) if path else None
        self.actor = actor
        self.data: dict[str, list[dict]] = {name: [] for name in TABLES}
        if self.path and self.path.exists():
            self.data.update(json.loads(self.path.read_text()))

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2, default=str))

    @staticmethod
    def _clean(row: dict) -> dict:
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}

    def init_tables(self) -> None:
        self._save()

    def _insert(self, table, rows):
        names = [c for c, _ in _columns(table)]
        for row in rows:
            unknown = set(row) - set(names)
            if unknown:
                raise KeyError(f"{table}: unknown columns {sorted(unknown)}")
            self.data[table].append({**dict.fromkeys(names), **self._clean(row)})
        self._save()

    def _select(self, table, where=None):
        where = where or {}
        return [dict(r) for r in self.data[table] if all(r.get(k) == v for k, v in where.items())]

    def _update(self, table, keys, fields):
        names = {c for c, _ in _columns(table)}
        unknown = set(fields) - names
        if unknown:
            raise KeyError(f"{table}: unknown columns {sorted(unknown)}")
        for row in self.data[table]:
            if all(row.get(k) == v for k, v in keys.items()):
                row.update(self._clean(fields))
        self._save()

    def _delete_all(self, table):
        self.data[table] = []
        self._save()


class SparkRegistry(Registry):
    """Delta tables in ``<catalog>.<schema>``, accessed with Spark SQL."""

    def __init__(self, spark, catalog: str, schema: str):
        self.spark = spark
        self.catalog, self.schema = catalog, schema
        try:
            self.actor = spark.sql("SELECT current_user() AS u").collect()[0]["u"]
        except Exception:
            self.actor = "unknown"

    def _name(self, table: str) -> str:
        return f"{self.catalog}.{self.schema}.{table}"

    def init_tables(self) -> None:
        for statement in ddl(self.catalog, self.schema).split(";"):
            if statement.strip():
                self.spark.sql(statement)

    @staticmethod
    def _param(value: Any) -> Any:
        return value.isoformat() if isinstance(value, datetime) else value

    def _expr(self, table: str, column: str, value: Any) -> str:
        """NULL is written inline; other values are passed as named parameters."""
        ctype = dict(_columns(table))[column]
        return f"CAST(NULL AS {ctype})" if value is None else f"CAST(:{column} AS {ctype})"

    def _insert(self, table, rows):
        names = [c for c, _ in _columns(table)]
        for row in rows:
            unknown = set(row) - set(names)
            if unknown:
                raise KeyError(f"{table}: unknown columns {sorted(unknown)}")
            values = ", ".join(self._expr(table, c, row.get(c)) for c in names)
            args = {c: self._param(row[c]) for c in names if row.get(c) is not None}
            self.spark.sql(f"INSERT INTO {self._name(table)} ({', '.join(names)}) VALUES ({values})",
                           args=args)

    def _select(self, table, where=None):
        where = where or {}
        clause = " AND ".join(f"{k} = :{k}" for k in where) or "1 = 1"
        rows = self.spark.sql(f"SELECT * FROM {self._name(table)} WHERE {clause}", args=dict(where))
        return [r.asDict() for r in rows.collect()]

    def _update(self, table, keys, fields):
        sets = ", ".join(f"{c} = {self._expr(table, c, v)}" for c, v in fields.items())
        clause = " AND ".join(f"{k} = :key_{k}" for k in keys)
        args = {c: self._param(v) for c, v in fields.items() if v is not None}
        args.update({f"key_{k}": v for k, v in keys.items()})
        self.spark.sql(f"UPDATE {self._name(table)} SET {sets} WHERE {clause}", args=args)

    def _delete_all(self, table):
        self.spark.sql(f"DELETE FROM {self._name(table)}")

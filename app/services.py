"""Application services: candidate creation, publishing and read models."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import storage
from .db import transaction
from .validation import Observation

PER_PAGE_DEFAULT = 100


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Conflict(Exception):
    def __init__(self, newer: sqlite3.Row) -> None:
        self.newer = newer
        super().__init__("该日期已存在更新版本")


@dataclass
class PublishOutcome:
    status: str
    message: str = ""
    version: sqlite3.Row | None = None
    newer: sqlite3.Row | None = None
    error: Exception | None = None


def create_candidate(
    conn: sqlite3.Connection,
    dataset_date: date,
    original_filename: str,
    stored: storage.StoredFile,
    observations: Sequence[Observation],
) -> int:
    """Insert a validated candidate version and its observations atomically."""
    now = utc_now()
    with transaction(conn):
        row = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no "
            "FROM versions WHERE dataset_date = ?",
            (dataset_date.isoformat(),),
        ).fetchone()
        version_no = int(row["next_no"])
        cursor = conn.execute(
            """
            INSERT INTO versions (
                dataset_date, version_no, status, original_filename,
                stored_path, sha256, size_bytes, row_count, created_at
            ) VALUES (?, ?, 'candidate', ?, ?, ?, ?, ?, ?)
            """,
            (
                dataset_date.isoformat(),
                version_no,
                original_filename,
                stored.relative_path,
                stored.sha256,
                stored.size_bytes,
                len(observations),
                now,
            ),
        )
        version_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO observations (
                version_id, row_no, site, metric, observed_at,
                observed_date, value, value_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    version_id,
                    item.row_no,
                    item.site,
                    item.metric,
                    item.observed_at,
                    item.observed_date,
                    item.value,
                    item.value_text,
                )
                for item in observations
            ],
        )
    return version_id


def publish_candidate(
    conn: sqlite3.Connection,
    files_dir: str | Path,
    version_id: int,
    *,
    now: str | None = None,
) -> PublishOutcome:
    """Publish a candidate unless a newer version for its date exists.

    The original file is verified *before* the transaction.  Files are
    content-addressed and immutable, so a verified file cannot change while
    the transaction replaces the current published version.  The update is
    one transaction: either the new version becomes the single published
    version for the date, or nothing changes and the previous published
    version stays fully usable.
    """
    candidate = conn.execute(
        "SELECT * FROM versions WHERE id = ?", (version_id,)
    ).fetchone()
    if candidate is None:
        return PublishOutcome("not_found", "版本不存在")

    if candidate["status"] == "published":
        return PublishOutcome("already_published", "该版本已是当前发布版本", candidate)
    if candidate["status"] != "candidate":
        return PublishOutcome("not_candidate", "该版本不是待发布候选", candidate)

    # A cheap pre-check gives the conflict message priority over file errors.
    newer = _latest_version_for_date(conn, candidate["dataset_date"])
    if newer is not None and newer["version_no"] > candidate["version_no"]:
        return PublishOutcome(
            "conflict",
            "该日期已存在更新版本，旧候选不能发布",
            candidate,
            newer,
        )

    try:
        storage.verify_file(
            files_dir, candidate["stored_path"], candidate["sha256"]
        )
    except storage.StorageError as exc:
        return PublishOutcome("file_error", str(exc), candidate, error=exc)

    timestamp = now or utc_now()
    try:
        with transaction(conn):
            # Re-read under the write lock: another request may have created
            # a newer version or published this candidate meanwhile.
            current = conn.execute(
                "SELECT * FROM versions WHERE id = ?", (version_id,)
            ).fetchone()
            if current is None:
                return PublishOutcome("not_found", "版本不存在")
            if current["status"] == "published":
                return PublishOutcome(
                    "already_published", "该版本已是当前发布版本", current
                )
            if current["status"] != "candidate":
                return PublishOutcome(
                    "not_candidate", "该版本不是待发布候选", current
                )
            latest = _latest_version_for_date(conn, current["dataset_date"])
            if latest is not None and latest["version_no"] > current["version_no"]:
                raise Conflict(latest)

            conn.execute(
                """
                UPDATE versions
                   SET status = 'superseded', superseded_at = ?
                 WHERE dataset_date = ? AND status = 'published'
                """,
                (timestamp, current["dataset_date"]),
            )
            conn.execute(
                """
                UPDATE versions
                   SET status = 'published', published_at = ?, superseded_at = NULL
                 WHERE id = ?
                """,
                (timestamp, version_id),
            )
    except Conflict as exc:
        return PublishOutcome(
            "conflict", "该日期已存在更新版本，旧候选不能发布", candidate, exc.newer
        )
    except sqlite3.Error as exc:
        return PublishOutcome(
            "db_error",
            "数据库提交失败，原有已发布版本保持不变",
            candidate,
            error=exc,
        )

    published = conn.execute(
        "SELECT * FROM versions WHERE id = ?", (version_id,)
    ).fetchone()
    return PublishOutcome(
        "published", "发布成功，版本已对外可见", published
    )


def _latest_version_for_date(
    conn: sqlite3.Connection, dataset_date: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM versions
         WHERE dataset_date = ?
         ORDER BY version_no DESC
         LIMIT 1
        """,
        (dataset_date,),
    ).fetchone()


def get_version(conn: sqlite3.Connection, version_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM versions WHERE id = ?", (version_id,)
    ).fetchone()


def get_current_published(
    conn: sqlite3.Connection, dataset_date: str
) -> sqlite3.Row | None:
    """Return the single externally visible version for a date."""
    return conn.execute(
        """
        SELECT * FROM versions
         WHERE dataset_date = ? AND status = 'published'
         ORDER BY version_no DESC
         LIMIT 1
        """,
        (dataset_date,),
    ).fetchone()


def list_published(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM versions
         WHERE status = 'published'
         ORDER BY dataset_date DESC
        """
    ).fetchall()


def list_versions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM versions
         ORDER BY dataset_date DESC, version_no DESC
        """
    ).fetchall()


def build_admin_groups(versions: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for version in versions:
        day = version["dataset_date"]
        group = groups.setdefault(
            day,
            {"dataset_date": day, "versions": [], "latest_no": 0, "published": None},
        )
        group["versions"].append(version)
        group["latest_no"] = max(group["latest_no"], version["version_no"])
        if version["status"] == "published":
            group["published"] = version
    return list(groups.values())


def fetch_observations(
    conn: sqlite3.Connection,
    version_id: int,
    *,
    site: str = "",
    metric: str = "",
    page: int = 1,
    per_page: int = PER_PAGE_DEFAULT,
) -> dict[str, Any]:
    conditions = ["version_id = ?"]
    params: list[Any] = [version_id]
    if site:
        conditions.append("site LIKE ?")
        params.append(f"%{site}%")
    if metric:
        conditions.append("metric LIKE ?")
        params.append(f"%{metric}%")
    where = " AND ".join(conditions)

    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM observations WHERE {where}", params
        ).fetchone()[0]
    )
    per_page = max(1, min(int(per_page), 500))
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(int(page), pages))
    rows = conn.execute(
        f"""
        SELECT * FROM observations
         WHERE {where}
         ORDER BY site, metric, observed_at, row_no
         LIMIT ? OFFSET ?
        """,
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "site": site,
        "metric": metric,
    }


def parse_page(value: str | None) -> int:
    try:
        return max(1, int(value or "1"))
    except (TypeError, ValueError):
        return 1

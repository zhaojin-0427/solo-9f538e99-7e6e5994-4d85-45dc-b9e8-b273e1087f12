from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest

from app import create_app
from app import services
from app.db import connect

ROWS = [
    ("2026-11-14", "东闸断面", "氨氮", "2026-11-14 08:00:00", "1.24"),
    ("2026-11-14", "西渡断面", "氨氮", "2026-11-14 09:00:00", "0.86"),
]


def csv_bytes(rows):
    header = "dataset_date,site,metric,observed_at,value"
    return ("\n".join([header, *[",".join(r) for r in rows]]) + "\n").encode()


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(tmp_path),
            "DATABASE": str(tmp_path / "test.db"),
            "FILES_DIR": str(tmp_path / "files"),
            "SECRET_KEY": "t",
            "CSRF_ENABLED": False,
        }
    )
    yield application


def test_concurrent_publishes_leave_one_visible_version(app):
    application = app
    client = application.test_client()

    def upload(payload: bytes):
        response = client.post(
            "/admin/upload",
            data={
                "dataset_date": "2026-11-14",
                "file": (io.BytesIO(payload), "obs.csv"),
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 302

    upload(csv_bytes(ROWS))
    upload(csv_bytes(ROWS + [("2026-11-14", "西渡断面", "pH", "2026-11-14 09:00:00", "7.2")]))

    conn = connect(application.config["DATABASE"])
    ids = [r["id"] for r in conn.execute("SELECT id FROM versions ORDER BY id")]
    conn.close()
    assert len(ids) == 2

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def publish(version_id: int):
        conn = connect(application.config["DATABASE"])
        try:
            barrier.wait()
            outcome = services.publish_candidate(
                conn, application.config["FILES_DIR"], version_id
            )
            with lock:
                outcomes.append(outcome.status)
        finally:
            conn.close()

    threads = [threading.Thread(target=publish, args=(vid,)) for vid in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    conn = connect(application.config["DATABASE"])
    try:
        visible = conn.execute(
            "SELECT COUNT(*) FROM versions WHERE dataset_date = ? AND status = 'published'",
            ("2026-11-14",),
        ).fetchone()[0]
        assert visible == 1
    finally:
        conn.close()

    # Either both publishes serialised (newest wins) or the stale one was
    # rejected — in all cases at most one succeeded and none errored hard.
    assert set(outcomes) <= {"published", "conflict"}
    assert "published" in outcomes


def test_duplicate_upload_is_idempotent_on_disk(app):
    client = app.test_client()
    for _ in range(2):
        response = client.post(
            "/admin/upload",
            data={
                "dataset_date": "2026-11-14",
                "file": (io.BytesIO(csv_bytes(ROWS)), "obs-a.csv"),
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 302

    conn = connect(app.config["DATABASE"])
    try:
        versions = conn.execute("SELECT * FROM versions ORDER BY id").fetchall()
        assert len(versions) == 2
        # Same bytes, same content-addressed path, one file on disk.
        assert versions[0]["stored_path"] == versions[1]["stored_path"]
        stored = Path(app.config["FILES_DIR"]) / versions[0]["stored_path"]
        assert stored.is_file()
        assert stored.read_bytes() == csv_bytes(ROWS)
    finally:
        conn.close()

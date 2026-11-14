from __future__ import annotations

import io
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from app import create_app
from app import services
from app.db import connect
from app.validation import validate_file


@pytest.fixture()
def app(tmp_path: Path):
    application = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(tmp_path),
            "DATABASE": str(tmp_path / "test.db"),
            "FILES_DIR": str(tmp_path / "files"),
            "SECRET_KEY": "test-secret",
            "CSRF_ENABLED": False,
        }
    )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def make_csv(rows: list[tuple[str, str, str, str]], header: str | None = None) -> bytes:
    header = header or "dataset_date,site,metric,observed_at,value"
    lines = [header] + [",".join(row) for row in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


VALID_ROWS = [
    ("2026-11-14", "东闸断面", "氨氮", "2026-11-14 08:00:00", "1.24"),
    ("2026-11-14", "东闸断面", "pH", "2026-11-14 08:00:00", "7.31"),
    ("2026-11-14", "西渡断面", "氨氮", "2026-11-14 14:00:00", "0.86"),
]


def upload(
    client,
    content: bytes,
    dataset_date: str = "2026-11-14",
    name="obs.csv",
    follow_redirects: bool = False,
):
    return client.post(
        "/admin/upload",
        data={
            "dataset_date": dataset_date,
            "file": (io.BytesIO(content), name),
        },
        content_type="multipart/form-data",
        follow_redirects=follow_redirects,
    )


def version_rows(app):
    conn = connect(app.config["DATABASE"])
    try:
        return conn.execute(
            "SELECT * FROM versions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_valid_file_becomes_candidate_with_observations(client, app):
    response = upload(client, make_csv(VALID_ROWS))
    assert response.status_code == 302

    conn = connect(app.config["DATABASE"])
    versions = conn.execute("SELECT * FROM versions").fetchall()
    assert len(versions) == 1
    assert versions[0]["status"] == "candidate"
    assert versions[0]["version_no"] == 1
    assert versions[0]["row_count"] == 3
    observations = conn.execute("SELECT * FROM observations").fetchall()
    assert len(observations) == 3
    conn.close()

    stored = Path(app.config["FILES_DIR"]) / versions[0]["stored_path"]
    assert stored.is_file()
    assert stored.read_bytes() == make_csv(VALID_ROWS)


def test_chinese_header_is_supported(tmp_path, app):
    path = tmp_path / "cn.csv"
    path.write_text(
        "站点,指标,观测时刻,数值\n"
        "东闸断面,氨氮,2026-11-14 08:00,1.2\n",
        encoding="utf-8",
    )
    result = validate_file(path, date(2026, 11, 14))
    assert result.ok, result.issues
    assert result.observations[0].site == "东闸断面"
    assert result.observations[0].metric == "氨氮"


def test_gb18030_file_is_supported(tmp_path):
    path = tmp_path / "gb.csv"
    path.write_bytes(
        "站点,指标,观测时刻,数值\n东闸断面,氨氮,2026-11-14 08:00,1.2\n".encode("gb18030")
    )
    result = validate_file(path, date(2026, 11, 14))
    assert result.ok, result.issues
    assert result.encoding == "gb18030"


def test_wrong_date_blocks_upload(client, app):
    rows = [("2026-11-14", "东闸断面", "氨氮", "2026-11-13 08:00:00", "1.2")]
    response = upload(client, make_csv(rows))
    assert response.status_code == 422
    assert "不一致" in response.get_data(as_text=True)
    assert version_rows(app) == []


def test_invalid_value_blocks_upload(client, app):
    rows = [
        ("2026-11-14", "东闸断面", "氨氮", "2026-11-14 08:00:00", "abc"),
        ("2026-11-14", "东闸断面", "氨氮", "2026-11-14 09:00:00", ""),
        ("2026-11-14", "东闸断面", "氨氮", "2026-11-14 10:00:00", "NaN"),
        ("2026-11-14", "东闸断面", "氨氮", "2026-11-14 11:00:00", "inf"),
    ]
    response = upload(client, make_csv(rows))
    assert response.status_code == 422
    body = response.get_data(as_text=True)
    assert "数值无效" in body
    assert "数值为空" in body
    assert version_rows(app) == []


def test_duplicate_site_metric_time_blocks_upload(client, app):
    rows = [
        ("2026-11-14", "东闸断面", "氨氮", "2026-11-14 08:00:00", "1.2"),
        ("2026-11-14", "东闸断面", "氨氮", "2026-11-14 08:00", "1.3"),
    ]
    response = upload(client, make_csv(rows))
    assert response.status_code == 422
    assert "重复" in response.get_data(as_text=True)
    assert version_rows(app) == []


def test_missing_columns_blocks_upload(client, app):
    response = upload(
        client,
        "站点,指标\n东闸断面,氨氮\n".encode("utf-8"),
    )
    assert response.status_code == 422
    body = response.get_data(as_text=True)
    assert "缺少必需列" in body
    assert version_rows(app) == []


def test_header_only_file_is_rejected(client, app):
    response = upload(client, make_csv([]))
    assert response.status_code == 422
    assert version_rows(app) == []


def test_column_count_mismatch_is_reported(client, app):
    content = (
        "dataset_date,site,metric,observed_at,value\n"
        "2026-11-14,东闸断面,氨氮,2026-11-14 08:00:00\n"
    ).encode("utf-8")
    response = upload(client, content)
    assert response.status_code == 422
    assert "列数" in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def test_publish_makes_version_public(client, app):
    upload(client, make_csv(VALID_ROWS))
    version = version_rows(app)[0]

    response = client.post(f"/admin/versions/{version['id']}/publish")
    assert response.status_code == 302

    public = client.get(f"/versions/{version['id']}")
    assert public.status_code == 200
    body = public.get_data(as_text=True)
    assert "东闸断面" in body
    assert "已发布" in body

    listing = client.get("/")
    assert "2026-11-14" in listing.get_data(as_text=True)

    download = client.get(f"/versions/{version['id']}/download")
    assert download.status_code == 200
    assert download.data == make_csv(VALID_ROWS)


def test_new_publish_supersedes_previous_version(client, app):
    upload(client, make_csv(VALID_ROWS))
    first = version_rows(app)[0]
    client.post(f"/admin/versions/{first['id']}/publish")

    new_rows = VALID_ROWS + [
        ("2026-11-14", "西渡断面", "pH", "2026-11-14 14:00:00", "7.28"),
    ]
    upload(client, make_csv(new_rows))
    second = version_rows(app)[1]
    client.post(f"/admin/versions/{second['id']}/publish")

    rows = {row["id"]: row for row in version_rows(app)}
    assert rows[first["id"]]["status"] == "superseded"
    assert rows[second["id"]]["status"] == "published"

    assert client.get(f"/versions/{first['id']}").status_code == 404
    assert client.get(f"/versions/{second['id']}").status_code == 200

    # The date entry point resolves to the newest published version.
    landing = client.get("/datasets/2026-11-14")
    assert landing.status_code == 302
    assert f"/versions/{second['id']}" in landing.headers["Location"]


def test_publishing_stale_candidate_conflicts(client, app):
    upload(client, make_csv(VALID_ROWS))
    stale = version_rows(app)[0]

    upload(
        client,
        make_csv(
            VALID_ROWS
            + [("2026-11-14", "西渡断面", "pH", "2026-11-14 14:00:00", "7.28")]
        ),
    )
    newest = version_rows(app)[1]

    response = client.post(
        f"/admin/versions/{stale['id']}/publish", follow_redirects=True
    )
    body = response.get_data(as_text=True)
    assert "已存在更新版本" in body

    rows = {row["id"]: row for row in version_rows(app)}
    assert rows[stale["id"]]["status"] == "candidate"
    assert rows[newest["id"]]["status"] == "candidate"
    assert client.get(f"/versions/{stale['id']}").status_code == 404


def test_stale_candidate_after_newer_publish_conflicts(client, app):
    upload(client, make_csv(VALID_ROWS))
    stale = version_rows(app)[0]

    upload(
        client,
        make_csv(
            VALID_ROWS
            + [("2026-11-14", "西渡断面", "pH", "2026-11-14 14:00:00", "7.28")]
        ),
    )
    newest = version_rows(app)[1]
    client.post(f"/admin/versions/{newest['id']}/publish")

    client.post(f"/admin/versions/{stale['id']}/publish")
    rows = {row["id"]: row for row in version_rows(app)}
    assert rows[stale["id"]]["status"] == "candidate"
    assert rows[newest["id"]]["status"] == "published"


def test_failed_database_commit_keeps_published_version(client, app, monkeypatch):
    upload(client, make_csv(VALID_ROWS))
    first = version_rows(app)[0]
    client.post(f"/admin/versions/{first['id']}/publish")
    assert version_rows(app)[0]["status"] == "published"

    new_rows = VALID_ROWS + [
        ("2026-11-14", "西渡断面", "pH", "2026-11-14 14:00:00", "7.28"),
    ]
    upload(client, make_csv(new_rows))
    second = version_rows(app)[1]

    from app import db as db_module

    class FailingCommitConnection:
        """Delegating proxy whose COMMIT always fails."""

        def __init__(self, inner):
            self._inner = inner

        def commit(self):
            raise sqlite3.OperationalError("simulated commit failure")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    real_connect = db_module.connect

    def failing_connect(database):
        return FailingCommitConnection(real_connect(database))

    monkeypatch.setattr(db_module, "connect", failing_connect)
    response = client.post(
        f"/admin/versions/{second['id']}/publish", follow_redirects=True
    )

    assert "数据库提交失败" in response.get_data(as_text=True)

    rows = {row["id"]: row for row in version_rows(app)}
    assert rows[second["id"]]["status"] == "candidate"
    assert rows[first["id"]]["status"] == "published"

    # The previously published version and its file remain fully usable.
    public = client.get(f"/versions/{first['id']}")
    assert public.status_code == 200
    download = client.get(f"/versions/{first['id']}/download")
    assert download.status_code == 200
    assert download.data == make_csv(VALID_ROWS)


def test_failed_file_write_does_not_create_candidate(client, app, monkeypatch):
    from app import storage

    def boom(*_args, **_kwargs):
        raise storage.StorageError("模拟写入失败")

    monkeypatch.setattr(storage, "store_file", boom)
    response = upload(client, make_csv(VALID_ROWS), follow_redirects=True)
    assert "模拟写入失败" in response.get_data(as_text=True)
    assert version_rows(app) == []


def test_download_missing_file_returns_503(client, app):
    upload(client, make_csv(VALID_ROWS))
    version = version_rows(app)[0]
    client.post(f"/admin/versions/{version['id']}/publish")

    stored = Path(app.config["FILES_DIR"]) / version["stored_path"]
    stored.unlink()

    response = client.get(f"/versions/{version['id']}/download")
    assert response.status_code == 503


def test_tampered_file_blocks_publish(client, app):
    upload(client, make_csv(VALID_ROWS))
    version = version_rows(app)[0]
    stored = Path(app.config["FILES_DIR"]) / version["stored_path"]
    stored.write_bytes(b"tampered")

    response = client.post(
        f"/admin/versions/{version['id']}/publish", follow_redirects=True
    )
    assert "校验失败" in response.get_data(as_text=True)
    assert version_rows(app)[0]["status"] == "candidate"


def test_candidate_requires_csrf_when_enabled(tmp_path):
    application = create_app(
        {
            "DATA_DIR": str(tmp_path),
            "DATABASE": str(tmp_path / "t.db"),
            "FILES_DIR": str(tmp_path / "files"),
            "SECRET_KEY": "s",
        }
    )
    client = application.test_client()
    response = client.post(
        "/admin/upload",
        data={"dataset_date": "2026-11-14"},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


def test_public_hides_candidates(client, app):
    upload(client, make_csv(VALID_ROWS))
    version = version_rows(app)[0]
    assert client.get("/").status_code == 200
    assert client.get(f"/versions/{version['id']}").status_code == 404
    assert client.get(f"/versions/{version['id']}/download").status_code == 404
    assert client.get("/datasets/2026-11-14").status_code == 404


def test_different_dates_are_independent(client, app):
    upload(client, make_csv(VALID_ROWS), dataset_date="2026-11-14")
    rows_b = [
        ("2026-11-15", "东闸断面", "氨氮", "2026-11-15 08:00:00", "1.1"),
    ]
    upload(client, make_csv(rows_b), dataset_date="2026-11-15")

    versions = version_rows(app)
    assert len(versions) == 2
    assert {v["dataset_date"] for v in versions} == {"2026-11-14", "2026-11-15"}


def test_values_are_canonicalised(client, app):
    rows = [
        ("2026-11-14", "东闸断面", "氨氮", "2026-11-14 08:00:00", "01.2400"),
        ("2026-11-14", "东闸断面", "总磷", "2026-11-14 08:00:00", "-0"),
    ]
    upload(client, make_csv(rows))
    conn = connect(app.config["DATABASE"])
    stored = conn.execute(
        "SELECT value_text FROM observations ORDER BY row_no"
    ).fetchall()
    conn.close()
    assert [row["value_text"] for row in stored] == ["1.24", "0"]


def test_legacy_dataset_date_column_is_tolerated(tmp_path):
    path = tmp_path / "with_extra.csv"
    path.write_text(
        "dataset_date,site,metric,observed_at,value\n"
        "2026-11-14,东闸断面,氨氮,2026-11-14 08:00:00,1.2\n",
        encoding="utf-8",
    )
    result = validate_file(path, date(2026, 11, 14))
    assert result.ok, result.issues


def test_service_publish_is_single_published_per_date(client, app):
    upload(client, make_csv(VALID_ROWS))
    first = version_rows(app)[0]

    conn = connect(app.config["DATABASE"])
    try:
        outcome = services.publish_candidate(
            conn, app.config["FILES_DIR"], first["id"]
        )
        assert outcome.status == "published"
    finally:
        conn.close()

    upload(
        client,
        make_csv(
            VALID_ROWS
            + [("2026-11-14", "西渡断面", "pH", "2026-11-14 14:00:00", "7.28")]
        ),
    )
    second = version_rows(app)[1]

    conn = connect(app.config["DATABASE"])
    try:
        outcome = services.publish_candidate(
            conn, app.config["FILES_DIR"], second["id"]
        )
        assert outcome.status == "published"
        published = conn.execute(
            "SELECT COUNT(*) FROM versions WHERE status = 'published' AND dataset_date = ?",
            ("2026-11-14",),
        ).fetchone()[0]
        assert published == 1
    finally:
        conn.close()


def test_semicolon_delimiter_and_decimal_comma(tmp_path):
    path = tmp_path / "semi.csv"
    path.write_text(
        "站点;指标;观测时刻;数值\n东闸断面;氨氮;2026-11-14 08:00;1,5\n",
        encoding="utf-8",
    )
    result = validate_file(path, date(2026, 11, 14))
    assert result.ok, result.issues
    assert result.observations[0].value_text == "1.5"


def test_timestamp_with_timezone_is_rejected(tmp_path):
    path = tmp_path / "tz.csv"
    path.write_text(
        "站点,指标,观测时刻,数值\n东闸断面,氨氮,2026-11-14T08:00:00+08:00,1.5\n",
        encoding="utf-8",
    )
    result = validate_file(path, date(2026, 11, 14))
    assert not result.ok
    assert any(issue.code == "invalid_time" for issue in result.issues)


def test_blank_rows_are_ignored(client, app):
    content = (
        "﻿dataset_date,site,metric,observed_at,value\r\n"
        "2026-11-14,东闸断面,氨氮,2026-11-14 08:00:00,1.2\r\n"
        "\r\n"
        "2026-11-14,东闸断面,pH,2026-11-14 08:00:00,7.2\r\n"
    ).encode("utf-8")
    response = upload(client, content)
    assert response.status_code == 302
    assert version_rows(app)[0]["row_count"] == 2


def test_extra_column_does_not_break_validation(client, app):
    response = upload(client, make_csv(VALID_ROWS))
    assert response.status_code == 302
    version = version_rows(app)[0]
    public = client.get("/admin/versions/%d" % version["id"])
    assert public.status_code == 200
    assert "东闸断面" in public.get_data(as_text=True)

"""Tests for the cross-midnight total-phosphorus load analysis."""

from __future__ import annotations

import io
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app import create_app
from app import services
from app.db import connect
from app.loads import (
    LoadInputError,
    ObservedReading,
    analyse,
    build_pieces,
    format_decimal,
    joint_pieces,
    parse_request,
)


def make_app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(tmp_path),
            "DATABASE": str(tmp_path / "test.db"),
            "FILES_DIR": str(tmp_path / "files"),
            "SECRET_KEY": "test-secret",
            "CSRF_ENABLED": False,
        }
    )


@pytest.fixture()
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture()
def client(app):
    return app.test_client()


def base_form(**overrides) -> dict[str, str]:
    form = {
        "start_date": "2026-11-14",
        "end_date": "2026-11-15",
        "upstream_site": "上游断面",
        "downstream_site": "下游断面",
        "upstream_flow_min": "10",
        "upstream_flow_max": "12",
        "upstream_max_interval": "2",
        "downstream_flow_min": "20",
        "downstream_flow_max": "25",
        "downstream_max_interval": "2",
    }
    form.update(overrides)
    return form


def upload_rows(client, day, rows):
    lines = ["site,metric,observed_at,value"]
    for site, stamp, value in rows:
        lines.append(f"{site},总磷,{stamp},{value}")
    response = client.post(
        "/admin/upload",
        data={
            "dataset_date": day,
            "file": (io.BytesIO(("\n".join(lines) + "\n").encode()), "obs.csv"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302, response.get_data(as_text=True)


def publish_latest(app, day):
    conn = connect(app.config["DATABASE"])
    try:
        row = conn.execute(
            "SELECT id FROM versions WHERE dataset_date = ? ORDER BY version_no DESC LIMIT 1",
            (day,),
        ).fetchone()
        outcome = services.publish_candidate(conn, app.config["FILES_DIR"], row["id"])
        assert outcome.status == "published", outcome.message
    finally:
        conn.close()


def two_day_constant(client, app, *, upstream_value="1.0", downstream_value="2.0", start_hour=0, step=2):
    for day in ("2026-11-14", "2026-11-15"):
        rows = []
        for hour in range(start_hour, 24, step):
            rows.append(("上游断面", f"{day} {hour:02d}:00:00", upstream_value))
            rows.append(("下游断面", f"{day} {hour:02d}:00:00", downstream_value))
        upload_rows(client, day, rows)
        publish_latest(app, day)


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------


def test_request_requires_consecutive_days():
    with pytest.raises(LoadInputError) as excinfo:
        parse_request(base_form(end_date="2026-11-16"))
    assert any("连续" in message for message in excinfo.value.errors)


def test_request_rejects_bad_flow_and_interval():
    with pytest.raises(LoadInputError) as excinfo:
        parse_request(
            base_form(
                upstream_flow_min="0",
                upstream_flow_max="-1",
                downstream_flow_min="NaN",
                downstream_flow_max="Infinity",
                upstream_max_interval="0.5",
                downstream_max_interval="7",
            )
        )
    errors = "\n".join(excinfo.value.errors)
    assert "必须大于 0" in errors
    assert "必须大于 0" in errors
    assert "必须是有限数字" in errors
    assert "不能小于 1" in errors
    assert "不能大于 6" in errors


def test_request_rejects_inverted_flow_bounds():
    with pytest.raises(LoadInputError) as excinfo:
        parse_request(base_form(upstream_flow_min="12", upstream_flow_max="10"))
    assert any("上限不能小于下限" in message for message in excinfo.value.errors)


def test_request_rejects_same_station():
    with pytest.raises(LoadInputError) as excinfo:
        parse_request(base_form(downstream_site="上游断面"))
    assert any("不同" in message for message in excinfo.value.errors)


def test_flow_bounds_must_be_positive_and_finite():
    with pytest.raises(LoadInputError) as excinfo:
        parse_request(base_form(downstream_flow_min="0", downstream_flow_max="1e31"))
    errors = "\n".join(excinfo.value.errors)
    assert "必须大于 0" in errors
    assert "超出合理范围" in errors


# ---------------------------------------------------------------------------
# Interpolation rules
# ---------------------------------------------------------------------------


def _reading(site: str, stamp: str, value: str):
    return ObservedReading(1, site, datetime.fromisoformat(stamp), Decimal(value))


def test_interpolation_skips_gap_beyond_limit():
    readings = [
        _reading("上游断面", "2026-11-14 00:00:00", "1"),
        _reading("上游断面", "2026-11-14 02:00:00", "1"),
        _reading("上游断面", "2026-11-14 05:00:00", "1"),
    ]
    pieces = build_pieces(readings, Decimal("2"))
    assert len(pieces) == 1
    assert pieces[0].end == datetime(2026, 11, 14, 2)


def test_interpolation_skips_negative_readings():
    readings = [
        _reading("上游断面", "2026-11-14 00:00:00", "1"),
        _reading("上游断面", "2026-11-14 01:00:00", "-0.5"),
        _reading("上游断面", "2026-11-14 02:00:00", "1"),
    ]
    assert build_pieces(readings, Decimal("6")) == []


def test_zero_concentration_is_usable():
    readings = [
        _reading("上游断面", "2026-11-14 00:00:00", "0"),
        _reading("上游断面", "2026-11-14 01:00:00", "1"),
    ]
    assert len(build_pieces(readings, Decimal("1"))) == 1


def test_joint_pieces_intersect_partial_overlap():
    upstream = build_pieces(
        [
            _reading("上游断面", "2026-11-14 00:00:00", "1"),
            _reading("上游断面", "2026-11-14 04:00:00", "1"),
        ],
        Decimal("6"),
    )
    downstream = build_pieces(
        [
            _reading("下游断面", "2026-11-14 02:00:00", "2"),
            _reading("下游断面", "2026-11-14 06:00:00", "2"),
        ],
        Decimal("6"),
    )
    overlaps = joint_pieces(upstream, downstream)
    assert len(overlaps) == 1
    assert overlaps[0].start == datetime(2026, 11, 14, 2)
    assert overlaps[0].end == datetime(2026, 11, 14, 4)


# ---------------------------------------------------------------------------
# End-to-end analysis
# ---------------------------------------------------------------------------


def test_constant_two_day_analysis_math(client, app):
    two_day_constant(client, app)
    conn = connect(app.config["DATABASE"])
    try:
        analysis = analyse(conn, parse_request(base_form()))
    finally:
        conn.close()

    result = analysis.result
    assert result is not None
    # Readings at 00:00..22:00 on both days: support runs from day-1 00:00 to
    # day-2 22:00 = 46 h.  The final 2 h are not extrapolated.
    assert result.coverage_hours == Decimal(46)
    assert not result.fully_covered
    assert len(result.gaps) == 1
    assert result.gaps[0].hours == Decimal(2)
    # Upstream integral = 1 mg/L * 46 h = 46 mg/L·h; flow 10–12 m³/s.
    assert result.upstream.integral == Decimal(46)
    assert result.upstream.load_low == Decimal("1656.0")   # 3.6*10*46
    assert result.upstream.load_high == Decimal("1987.2")  # 3.6*12*46
    # Downstream integral = 2 * 46 = 92; flow 20–25.
    assert result.downstream.load_low == Decimal("6624.0")   # 3.6*20*92
    assert result.downstream.load_high == Decimal("8280.0")  # 3.6*25*92
    # Difference pairs downstream min with upstream max and vice versa.
    assert result.diff_low == Decimal("6624.0") - Decimal("1987.2")
    assert result.diff_high == Decimal("8280.0") - Decimal("1656.0")
    assert result.verdict == "added"


def test_missing_one_day_blocks_calculation(client, app):
    upload_rows(
        client,
        "2026-11-14",
        [
            ("上游断面", "2026-11-14 00:00:00", "1"),
            ("下游断面", "2026-11-14 00:00:00", "2"),
        ],
    )
    publish_latest(app, "2026-11-14")
    conn = connect(app.config["DATABASE"])
    try:
        analysis = analyse(conn, parse_request(base_form()))
    finally:
        conn.close()
    assert analysis.result is None
    assert [day.day.isoformat() for day in analysis.missing_days] == ["2026-11-15"]


def test_missing_day_renders_explicit_notice(client, app):
    upload_rows(
        client,
        "2026-11-14",
        [("上游断面", "2026-11-14 00:00:00", "1")],
    )
    publish_latest(app, "2026-11-14")
    response = client.post("/loads", data=base_form())
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "缺少当前已发布版本" in body
    assert "2026-11-15" in body
    assert "未进行核算" in body


def test_candidate_version_is_not_used(client, app):
    two_day_constant(client, app)
    # A newer *candidate* for day 2 must not affect the published snapshot.
    upload_rows(
        client,
        "2026-11-15",
        [
            ("上游断面", "2026-11-15 00:00:00", "9"),
            ("下游断面", "2026-11-15 00:00:00", "9"),
        ],
    )
    conn = connect(app.config["DATABASE"])
    try:
        analysis = analyse(conn, parse_request(base_form()))
        published = services.get_current_published(conn, "2026-11-15")
    finally:
        conn.close()
    assert analysis.result is not None
    assert analysis.snapshot.days[1].version["id"] == published["id"]
    assert analysis.result.coverage_hours == Decimal(46)


def test_snapshot_is_isolated_from_concurrent_republish(client, app):
    """A republish committed mid-analysis cannot leak into the snapshot."""
    two_day_constant(client, app)
    # Prepare a second published version for day 14 (values of 9 mg/L).
    upload_rows(
        client,
        "2026-11-14",
        [
            ("上游断面", f"2026-11-14 {hour:02d}:00:00", "9") for hour in range(0, 24, 2)
        ]
        + [
            ("下游断面", f"2026-11-14 {hour:02d}:00:00", "9") for hour in range(0, 24, 2)
        ],
    )
    conn = connect(app.config["DATABASE"])
    try:
        replacement_id = conn.execute(
            "SELECT id FROM versions WHERE dataset_date = '2026-11-14' "
            "ORDER BY version_no DESC LIMIT 1"
        ).fetchone()["id"]
        assert (
            conn.execute(
                "SELECT status FROM versions WHERE id = ?", (replacement_id,)
            ).fetchone()["status"]
            == "candidate"
        )

        from app.db import read_snapshot

        # The first read inside the transaction pins the snapshot; a publish
        # committed afterwards by another connection must stay invisible.
        with read_snapshot(conn):
            day15_snapshot = services.get_current_published(conn, "2026-11-15")
            other = connect(app.config["DATABASE"])
            try:
                outcome = services.publish_candidate(
                    other, app.config["FILES_DIR"], replacement_id
                )
                assert outcome.status == "published", outcome.message
            finally:
                other.close()
            day14_snapshot = services.get_current_published(conn, "2026-11-14")
        # Fresh reads outside the old transaction see the republished day 14.
        fresh = services.get_current_published(conn, "2026-11-14")
        assert day14_snapshot["version_no"] == 1
        assert fresh["version_no"] == 2
        assert day15_snapshot["version_no"] == 1
    finally:
        conn.close()


def test_partial_coverage_is_not_called_two_day_total(client, app):
    # Readings only on day 1, every hour, but no day-2 readings at all.
    rows = [
        ("上游断面", f"2026-11-14 {hour:02d}:00:00", "1") for hour in range(24)
    ] + [("下游断面", f"2026-11-14 {hour:02d}:00:00", "2") for hour in range(24)]
    upload_rows(client, "2026-11-14", rows)
    publish_latest(app, "2026-11-14")
    # Day 2 has only a single reading, so no interval can be formed there.
    upload_rows(
        client,
        "2026-11-15",
        [
            ("上游断面", "2026-11-15 00:00:00", "1"),
            ("下游断面", "2026-11-15 00:00:00", "2"),
        ],
    )
    publish_latest(app, "2026-11-15")

    response = client.post("/loads", data=base_form(upstream_max_interval="1", downstream_max_interval="1"))
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "参与时段" in body
    assert "两日总量" in body
    assert "缺口" in body


def test_no_joint_support_reports_unknown(client, app):
    # Upstream only on day 1, downstream only on day 2: no common support.
    upload_rows(
        client,
        "2026-11-14",
        [("上游断面", f"2026-11-14 {hour:02d}:00:00", "1") for hour in range(24)],
    )
    publish_latest(app, "2026-11-14")
    upload_rows(
        client,
        "2026-11-15",
        [("下游断面", f"2026-11-15 {hour:02d}:00:00", "2") for hour in range(24)],
    )
    publish_latest(app, "2026-11-15")

    conn = connect(app.config["DATABASE"])
    try:
        result = analyse(conn, parse_request(base_form())).result
    finally:
        conn.close()
    assert result is not None
    assert result.coverage_hours == 0
    assert result.upstream.load_low is None
    assert result.verdict == "unknown"

    body = client.post("/loads", data=base_form()).get_data(as_text=True)
    assert "无法确定" in body
    assert "没有共同观测支撑时段" in body


def test_verdict_bounds(client, app):
    two_day_constant(client, app)
    conn = connect(app.config["DATABASE"])
    try:
        # Downstream flow far below upstream: upper bound <= 0 -> no addition.
        request = parse_request(
            base_form(
                downstream_flow_min="1",
                downstream_flow_max="2",
                downstream_max_interval="2",
            )
        )
        result = analyse(conn, request).result
        assert result is not None
        assert result.diff_high <= 0
        assert result.verdict == "none"

        # Overlapping ranges straddle zero -> undetermined.
        request = parse_request(
            base_form(
                upstream_flow_min="20",
                upstream_flow_max="25",
                downstream_flow_min="10",
                downstream_flow_max="14",
            )
        )
        result = analyse(conn, request).result
        assert result is not None
        assert result.diff_low <= 0 < result.diff_high
        assert result.verdict == "unknown"
    finally:
        conn.close()


def test_zero_flow_difference_is_not_confirmation(client, app):
    two_day_constant(client, app, upstream_value="1.0", downstream_value="1.0")
    conn = connect(app.config["DATABASE"])
    try:
        result = analyse(conn, parse_request(base_form())).result
    finally:
        conn.close()
    # Downstream is never below upstream here, but the lower bound is not > 0
    # when ranges overlap? Flows are 10–12 and 20–25, so the difference is
    # strictly positive.
    assert result.diff_low > 0
    assert result.verdict == "added"


def test_form_validation_errors_are_rendered(client, app):
    response = client.post("/loads", data=base_form(upstream_flow_min="0"))
    assert response.status_code == 422
    body = response.get_data(as_text=True)
    assert "必须大于 0" in body
    assert "参数有误" in body


def test_loads_page_requires_csrf_when_enabled(tmp_path):
    application = create_app(
        {
            "DATA_DIR": str(tmp_path),
            "DATABASE": str(tmp_path / "t.db"),
            "FILES_DIR": str(tmp_path / "files"),
            "SECRET_KEY": "s",
        }
    )
    client = application.test_client()
    response = client.post("/loads", data=base_form())
    assert response.status_code == 400


def test_hash_decimal_formatting():
    assert format_decimal(Decimal("1728.000000")) == "1728"
    assert format_decimal(Decimal("0.123456789")) == "0.123457"
    assert format_decimal(None) == "—"

"""Cross-midnight total-phosphorus load balance analysis.

The analyst supplies two consecutive dataset dates, an upstream and a
downstream site, per-site flow bounds (m³/s) and a maximum sampling interval
(1–6 h).  Total-phosphorus concentrations are read from the *published*
versions of both dates in one database snapshot.  For each site we linearly
interpolate only between adjacent non-negative readings whose time gap does
not exceed that site's maximum sampling interval.  Loads are integrated only
over intervals where both sites have observational support; nothing is
extrapolated.

Load rate for a concentration ``C`` (mg/L) and flow ``Q`` (m³/s) is
``C * Q * 3.6`` kg/h:

    mg/L = g/m³,  m³/s * g/m³ = g/s,  * 3600 s/h / 1000 g/kg = 3.6 kg/h

Only the flow bounds are known, so each site's load becomes a range
``[3.6 * Qmin * ∫C dt, 3.6 * Qmax * ∫C dt]``.  The downstream-minus-upstream
difference range is obtained by pairing the downstream minimum with the
upstream maximum and vice versa.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any, Iterable, Mapping, Sequence

from . import services
from .db import read_snapshot

LOAD_METRIC = "总磷"
LOAD_FACTOR = Decimal("3.6")  # kg per (mg/L * m³/s * h)
MAX_FLOW = Decimal("1e30")  # guards against absurd exponents, not a physical limit
ZERO = Decimal(0)
HUNDRED = Decimal(100)

FORM_FIELDS = (
    "start_date",
    "end_date",
    "upstream_site",
    "downstream_site",
    "upstream_flow_min",
    "upstream_flow_max",
    "upstream_max_interval",
    "downstream_flow_min",
    "downstream_flow_max",
    "downstream_max_interval",
)

DEFAULT_INTERVAL = "6"


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


class LoadInputError(ValueError):
    """Raised when submitted analysis parameters are invalid."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("；".join(self.errors))


@dataclass(frozen=True)
class StationSpec:
    name: str
    flow_min: Decimal
    flow_max: Decimal
    max_interval_hours: Decimal


@dataclass(frozen=True)
class LoadRequest:
    day1: date
    day2: date
    upstream: StationSpec
    downstream: StationSpec


def _text(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key, "")
    if value is None:
        return ""
    return str(value).strip()


def _parse_decimal(
    raw: str,
    label: str,
    errors: list[str],
    *,
    positive: bool = False,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal | None:
    text = raw.strip()
    if not text:
        errors.append(f"{label}不能为空")
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        errors.append(f"{label}必须是有效数字")
        return None
    if not value.is_finite():
        errors.append(f"{label}必须是有限数字")
        return None
    if positive and value <= 0:
        errors.append(f"{label}必须大于 0")
        return None
    if value < 0:
        errors.append(f"{label}不能为负数")
        return None
    if minimum is not None and value < minimum:
        errors.append(f"{label}不能小于 {minimum}")
        return None
    if maximum is not None and value > maximum:
        errors.append(f"{label}不能大于 {maximum}")
        return None
    if value > MAX_FLOW:
        errors.append(f"{label}超出合理范围")
        return None
    return value


def parse_request(values: Mapping[str, Any]) -> LoadRequest:
    """Validate browser/form parameters, collecting all problems."""
    errors: list[str] = []

    day1 = _parse_date(_text(values, "start_date"), "起始日期", errors)
    day2 = _parse_date(_text(values, "end_date"), "结束日期", errors)
    if day1 and day2 and day2 != day1 + timedelta(days=1):
        errors.append("两个日期必须连续：结束日期应为起始日期的次日")

    upstream_name, downstream_name = _parse_station_names(values, errors)

    upstream_flow_min = _parse_decimal(
        _text(values, "upstream_flow_min"),
        "上游断面流量下限",
        errors,
        positive=True,
    )
    upstream_flow_max = _parse_decimal(
        _text(values, "upstream_flow_max"),
        "上游断面流量上限",
        errors,
        positive=True,
    )
    downstream_flow_min = _parse_decimal(
        _text(values, "downstream_flow_min"),
        "下游断面流量下限",
        errors,
        positive=True,
    )
    downstream_flow_max = _parse_decimal(
        _text(values, "downstream_flow_max"),
        "下游断面流量上限",
        errors,
        positive=True,
    )

    if (
        upstream_flow_min is not None
        and upstream_flow_max is not None
        and upstream_flow_max < upstream_flow_min
    ):
        errors.append("上游断面流量上限不能小于下限")
    if (
        downstream_flow_min is not None
        and downstream_flow_max is not None
        and downstream_flow_max < downstream_flow_min
    ):
        errors.append("下游断面流量上限不能小于下限")

    upstream_interval = _parse_interval(values, "upstream_max_interval", "上游断面", errors)
    downstream_interval = _parse_interval(
        values, "downstream_max_interval", "下游断面", errors
    )

    if errors:
        raise LoadInputError(errors)

    assert day1 is not None and day2 is not None
    assert upstream_flow_min is not None and upstream_flow_max is not None
    assert downstream_flow_min is not None and downstream_flow_max is not None
    assert upstream_interval is not None and downstream_interval is not None
    return LoadRequest(
        day1=day1,
        day2=day2,
        upstream=StationSpec(
            upstream_name, upstream_flow_min, upstream_flow_max, upstream_interval
        ),
        downstream=StationSpec(
            downstream_name, downstream_flow_min, downstream_flow_max, downstream_interval
        ),
    )


def _parse_date(raw: str, label: str, errors: list[str]) -> date | None:
    if not raw:
        errors.append(f"{label}不能为空")
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        errors.append(f"{label}格式应为 YYYY-MM-DD")
        return None


def _parse_station_names(
    values: Mapping[str, Any], errors: list[str]
) -> tuple[str, str]:
    upstream = unicodedata.normalize("NFKC", _text(values, "upstream_site")).strip()
    downstream = unicodedata.normalize("NFKC", _text(values, "downstream_site")).strip()
    if not upstream:
        errors.append("请选择或填写上游断面")
    if not downstream:
        errors.append("请选择或填写下游断面")
    for label, name in (("上游断面", upstream), ("下游断面", downstream)):
        if len(name) > 200:
            errors.append(f"{label}名称过长")
    if upstream and downstream and upstream == downstream:
        errors.append("上游与下游必须是两个不同的断面")
    return upstream, downstream


def _parse_interval(
    values: Mapping[str, Any], key: str, station_label: str, errors: list[str]
) -> Decimal | None:
    return _parse_decimal(
        _text(values, key),
        f"{station_label}最大采样间隔（小时）",
        errors,
        minimum=Decimal("1"),
        maximum=Decimal("6"),
    )


# ---------------------------------------------------------------------------
# Snapshot read
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotDay:
    day: date
    version: sqlite3.Row | None


@dataclass(frozen=True)
class ObservedReading:
    version_id: int
    site: str
    at: datetime
    concentration: Decimal


@dataclass(frozen=True)
class Snapshot:
    days: tuple[SnapshotDay, SnapshotDay]
    readings: tuple[ObservedReading, ...]


def read_published_snapshot(
    conn: sqlite3.Connection, request: LoadRequest
) -> Snapshot:
    """Read both days' published versions and observations atomically.

    The two version lookups and the observation query run inside one SQLite
    read transaction.  A version republished for either date while the
    analysis is being served therefore cannot leak into the calculation: both
    days always come from the same snapshot.
    """
    sites = (request.upstream.name, request.downstream.name)
    with read_snapshot(conn):
        versions = [
            services.get_current_published(conn, request.day1.isoformat()),
            services.get_current_published(conn, request.day2.isoformat()),
        ]
        readings: list[ObservedReading] = []
        version_ids = [v["id"] for v in versions if v is not None]
        if version_ids:
            placeholders = ",".join("?" for _ in version_ids)
            rows = conn.execute(
                f"""
                SELECT version_id, site, observed_at, value_text
                  FROM observations
                 WHERE version_id IN ({placeholders})
                   AND metric = ?
                   AND site IN (?, ?)
                 ORDER BY observed_at
                """,
                [*version_ids, LOAD_METRIC, *sites],
            ).fetchall()
            for row in rows:
                try:
                    at = datetime.fromisoformat(row["observed_at"])
                    concentration = Decimal(row["value_text"])
                except (ValueError, InvalidOperation):
                    # Stored data is validated on upload; a malformed row is
                    # skipped rather than silently coerced.
                    continue
                readings.append(
                    ObservedReading(
                        version_id=int(row["version_id"]),
                        site=row["site"],
                        at=at,
                        concentration=concentration,
                    )
                )
    return Snapshot(
        days=(
            SnapshotDay(request.day1, versions[0]),
            SnapshotDay(request.day2, versions[1]),
        ),
        readings=tuple(readings),
    )


# ---------------------------------------------------------------------------
# Interpolation and integration
# ---------------------------------------------------------------------------


def _seconds(delta: timedelta) -> Decimal:
    return (
        Decimal(delta.days * 86400 + delta.seconds)
        + Decimal(delta.microseconds) / Decimal(1_000_000)
    )


def _hours(delta: timedelta) -> Decimal:
    return _seconds(delta) / Decimal(3600)


@dataclass(frozen=True)
class Piece:
    """One linearly interpolated segment between adjacent readings."""

    start: datetime
    end: datetime
    start_value: Decimal
    end_value: Decimal

    def value_at(self, moment: datetime) -> Decimal:
        if moment <= self.start:
            return self.start_value
        if moment >= self.end:
            return self.end_value
        total = _seconds(self.end - self.start)
        offset = _seconds(moment - self.start)
        return self.start_value + (self.end_value - self.start_value) * offset / total


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    hours: Decimal


@dataclass(frozen=True)
class JointPiece:
    start: datetime
    end: datetime
    upstream_start: Decimal
    upstream_end: Decimal
    downstream_start: Decimal
    downstream_end: Decimal

    @property
    def hours(self) -> Decimal:
        return _hours(self.end - self.start)


@dataclass(frozen=True)
class StationResult:
    spec: StationSpec
    readings: tuple[ObservedReading, ...]
    pieces: tuple[Piece, ...]
    support: tuple[Interval, ...]
    support_hours: Decimal
    negative_count: int
    integral: Decimal | None
    load_low: Decimal | None
    load_high: Decimal | None


def build_pieces(
    readings: Iterable[ObservedReading], max_interval_hours: Decimal
) -> list[Piece]:
    ordered = sorted(readings, key=lambda item: item.at)
    pieces: list[Piece] = []
    for first, second in zip(ordered, ordered[1:]):
        if first.concentration < 0 or second.concentration < 0:
            continue
        gap_hours = _hours(second.at - first.at)
        if gap_hours <= 0 or gap_hours > max_interval_hours:
            continue
        pieces.append(
            Piece(
                start=first.at,
                end=second.at,
                start_value=first.concentration,
                end_value=second.concentration,
            )
        )
    return pieces


def merge_intervals(intervals: Iterable[tuple[datetime, datetime]]) -> list[Interval]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return [Interval(start, end, _hours(end - start)) for start, end in merged]


def joint_pieces(upstream: Sequence[Piece], downstream: Sequence[Piece]) -> list[JointPiece]:
    """Intersect two sorted, non-overlapping piece lists in linear time."""
    result: list[JointPiece] = []
    i = j = 0
    while i < len(upstream) and j < len(downstream):
        a = upstream[i]
        b = downstream[j]
        start = max(a.start, b.start)
        end = min(a.end, b.end)
        if start < end:
            result.append(
                JointPiece(
                    start=start,
                    end=end,
                    upstream_start=a.value_at(start),
                    upstream_end=a.value_at(end),
                    downstream_start=b.value_at(start),
                    downstream_end=b.value_at(end),
                )
            )
        if a.end <= b.end:
            i += 1
        else:
            j += 1
    return result


def _complement(
    window_start: datetime, window_end: datetime, intervals: Sequence[Interval]
) -> list[Interval]:
    gaps: list[Interval] = []
    cursor = window_start
    for interval in intervals:
        start = max(interval.start, window_start)
        end = min(interval.end, window_end)
        if end <= cursor:
            continue
        if start > cursor:
            gaps.append(Interval(cursor, start, _hours(start - cursor)))
        cursor = end
    if cursor < window_end:
        gaps.append(Interval(cursor, window_end, _hours(window_end - cursor)))
    return gaps


def _site_result(
    spec: StationSpec, readings: Sequence[ObservedReading], integral: Decimal | None
) -> StationResult:
    pieces = build_pieces(readings, spec.max_interval_hours)
    support = merge_intervals((piece.start, piece.end) for piece in pieces)
    support_hours = sum((interval.hours for interval in support), ZERO)
    load_low = load_high = None
    if integral is not None:
        load_low = LOAD_FACTOR * spec.flow_min * integral
        load_high = LOAD_FACTOR * spec.flow_max * integral
    return StationResult(
        spec=spec,
        readings=tuple(readings),
        pieces=tuple(pieces),
        support=tuple(support),
        support_hours=support_hours,
        negative_count=sum(1 for item in readings if item.concentration < 0),
        integral=integral,
        load_low=load_low,
        load_high=load_high,
    )


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentResult:
    """One participating sub-period where both sites are supported."""

    start: datetime
    end: datetime
    hours: Decimal
    upstream_integral: Decimal
    downstream_integral: Decimal
    upstream_load_low: Decimal
    upstream_load_high: Decimal
    downstream_load_low: Decimal
    downstream_load_high: Decimal
    diff_low: Decimal
    diff_high: Decimal


@dataclass(frozen=True)
class LoadResult:
    upstream: StationResult
    downstream: StationResult
    window_start: datetime
    window_end: datetime
    window_hours: Decimal
    segments: tuple[SegmentResult, ...]
    joint_intervals: tuple[Interval, ...]
    gaps: tuple[Interval, ...]
    coverage_hours: Decimal
    coverage_percent: Decimal
    diff_low: Decimal | None
    diff_high: Decimal | None
    verdict: str
    verdict_title: str
    verdict_detail: str

    @property
    def has_support(self) -> bool:
        return self.coverage_hours > 0

    @property
    def fully_covered(self) -> bool:
        return self.coverage_hours >= self.window_hours


@dataclass(frozen=True)
class Analysis:
    snapshot: Snapshot
    request: LoadRequest
    missing_days: tuple[SnapshotDay, ...]
    result: LoadResult | None


def analyse(conn: sqlite3.Connection, request: LoadRequest) -> Analysis:
    snapshot = read_published_snapshot(conn, request)
    missing = tuple(day for day in snapshot.days if day.version is None)
    if missing:
        # A missing day blocks the whole calculation — no partial two-day
        # comparison is produced.
        return Analysis(snapshot, request, missing, None)

    by_site: dict[str, list[ObservedReading]] = {
        request.upstream.name: [],
        request.downstream.name: [],
    }
    for reading in snapshot.readings:
        by_site.setdefault(reading.site, []).append(reading)
    upstream_readings = sorted(by_site[request.upstream.name], key=lambda r: r.at)
    downstream_readings = sorted(by_site[request.downstream.name], key=lambda r: r.at)

    upstream_pieces = build_pieces(
        upstream_readings, request.upstream.max_interval_hours
    )
    downstream_pieces = build_pieces(
        downstream_readings, request.downstream.max_interval_hours
    )
    overlaps = joint_pieces(upstream_pieces, downstream_pieces)

    upstream_integral = ZERO
    downstream_integral = ZERO
    for piece in overlaps:
        hours = piece.hours
        upstream_integral += (
            (piece.upstream_start + piece.upstream_end) / 2 * hours
        )
        downstream_integral += (
            (piece.downstream_start + piece.downstream_end) / 2 * hours
        )

    support = bool(overlaps)
    upstream = _site_result(
        request.upstream,
        upstream_readings,
        upstream_integral if support else None,
    )
    downstream = _site_result(
        request.downstream,
        downstream_readings,
        downstream_integral if support else None,
    )

    window_start = datetime.combine(request.day1, time.min)
    window_end = window_start + timedelta(days=2)
    window_hours = _hours(window_end - window_start)
    joint_intervals = merge_intervals(
        (piece.start, piece.end) for piece in overlaps
    )
    coverage_hours = sum((interval.hours for interval in joint_intervals), ZERO)
    coverage_percent = (
        coverage_hours / window_hours * HUNDRED if window_hours else ZERO
    )
    gaps = tuple(_complement(window_start, window_end, joint_intervals))
    segments = _build_segments(
        overlaps,
        request.upstream.flow_min,
        request.upstream.flow_max,
        request.downstream.flow_min,
        request.downstream.flow_max,
    )

    diff_low = diff_high = None
    if support and upstream.load_low is not None and downstream.load_low is not None:
        assert upstream.load_high is not None and downstream.load_high is not None
        # The difference is summed from the participating segments so the
        # table and the headline figure can never disagree.
        diff_low = sum((segment.diff_low for segment in segments), ZERO)
        diff_high = sum((segment.diff_high for segment in segments), ZERO)

    verdict, title, detail = _verdict(
        support, diff_low, diff_high, coverage_hours, window_hours
    )

    result = LoadResult(
        upstream=upstream,
        downstream=downstream,
        window_start=window_start,
        window_end=window_end,
        window_hours=window_hours,
        segments=segments,
        joint_intervals=tuple(joint_intervals),
        gaps=gaps,
        coverage_hours=coverage_hours,
        coverage_percent=coverage_percent,
        diff_low=diff_low,
        diff_high=diff_high,
        verdict=verdict,
        verdict_title=title,
        verdict_detail=detail,
    )
    return Analysis(snapshot, request, (), result)


def _build_segments(
    overlaps: Sequence[JointPiece],
    upstream_min: Decimal,
    upstream_max: Decimal,
    downstream_min: Decimal,
    downstream_max: Decimal,
) -> tuple[SegmentResult, ...]:
    """Group contiguous overlap pieces and compute per-segment load ranges."""
    groups: list[list[JointPiece]] = []
    for piece in overlaps:
        if groups and groups[-1][-1].end == piece.start:
            groups[-1].append(piece)
        else:
            groups.append([piece])

    segments: list[SegmentResult] = []
    for group in groups:
        upstream_integral = sum(
            (
                (piece.upstream_start + piece.upstream_end)
                / 2
                * _hours(piece.end - piece.start)
                for piece in group
            ),
            ZERO,
        )
        downstream_integral = sum(
            (
                (piece.downstream_start + piece.downstream_end)
                / 2
                * _hours(piece.end - piece.start)
                for piece in group
            ),
            ZERO,
        )
        upstream_low = LOAD_FACTOR * upstream_min * upstream_integral
        upstream_high = LOAD_FACTOR * upstream_max * upstream_integral
        downstream_low = LOAD_FACTOR * downstream_min * downstream_integral
        downstream_high = LOAD_FACTOR * downstream_max * downstream_integral
        segments.append(
            SegmentResult(
                start=group[0].start,
                end=group[-1].end,
                hours=sum(
                    (_hours(piece.end - piece.start) for piece in group), ZERO
                ),
                upstream_integral=upstream_integral,
                downstream_integral=downstream_integral,
                upstream_load_low=upstream_low,
                upstream_load_high=upstream_high,
                downstream_load_low=downstream_low,
                downstream_load_high=downstream_high,
                diff_low=downstream_low - upstream_high,
                diff_high=downstream_high - upstream_low,
            )
        )
    return tuple(segments)


def _verdict(
    support: bool,
    diff_low: Decimal | None,
    diff_high: Decimal | None,
    coverage_hours: Decimal,
    window_hours: Decimal,
) -> tuple[str, str, str]:
    if not support or diff_low is None or diff_high is None:
        return (
            "unknown",
            "无法确定",
            "两站没有共同观测支撑时段，未进行积分，因此不能判断是否新增负荷。",
        )
    if diff_low > 0:
        code = "added"
        title = "确认新增负荷"
        detail = "下游减上游的差额下界大于 0，可确认下游断面存在新增总磷负荷。"
    elif diff_high <= 0:
        code = "none"
        title = "未见新增负荷"
        detail = "下游减上游的差额上界不大于 0，在本次核算范围内未见新增总磷负荷。"
    else:
        code = "unknown"
        title = "无法确定"
        detail = "下游减上游的差额区间跨越 0，现有观测与流量范围不足以确认新增负荷。"
    if coverage_hours < window_hours:
        detail += (
            f" 结论仅适用于两站共同有观测支撑的 {format_decimal(coverage_hours, 4)}"
            f" 小时，不代表完整两日窗口。"
        )
    return code, title, detail


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def format_decimal(value: Decimal | int | float | None, places: int = 6) -> str:
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        number = value
    else:
        number = Decimal(str(value))
    if not number.is_finite():
        return "—"
    quantum = Decimal(1).scaleb(-places)
    try:
        with localcontext() as context:
            context.prec = max(40, len(number.as_tuple().digits) + places + 5)
            rendered = format(
                number.quantize(quantum, rounding=ROUND_HALF_UP), "f"
            )
    except (InvalidOperation, ValueError):
        rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in ("", "-0"):
        rendered = "0"
    return rendered


def format_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    rendered = value.strftime("%Y-%m-%d %H:%M:%S")
    if value.microsecond:
        rendered += f".{value.microsecond:06d}".rstrip("0")
    return rendered

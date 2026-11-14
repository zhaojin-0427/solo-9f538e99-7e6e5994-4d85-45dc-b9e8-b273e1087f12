"""CSV parsing and validation.

A file is only accepted when it has the required columns and every data row
has a non-empty site/metric, a parseable observation timestamp belonging to
the selected dataset date, a finite numeric value, and no duplicate
(site, metric, observed_at) key within the file.  Every detected problem is
reported; any problem blocks candidate creation.
"""

from __future__ import annotations

import csv
import io
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

MAX_REPORTED_ISSUES = 500

REQUIRED_FIELDS = ("site", "metric", "observed_at", "value")
FIELD_LABELS = {
    "site": "站点",
    "metric": "指标",
    "observed_at": "观测时刻",
    "value": "数值",
}

# Header aliases, compared after NFKC normalisation, lower-casing and
# whitespace collapsing.  Both Chinese and English files are accepted.
HEADER_ALIASES: dict[str, set[str]] = {
    "site": {
        "site", "station", "station id", "station name", "station_id",
        "站点", "监测站点", "站点名称", "测站", "断面", "断面名称",
    },
    "metric": {
        "metric", "indicator", "parameter", "item", "pollutant",
        "指标", "监测指标", "指标名称", "参数", "项目", "污染物",
    },
    "observed_at": {
        "observed_at", "observed at", "observed time", "datetime",
        "timestamp", "time", "sample time",
        "观测时刻", "观测时间", "监测时间", "采样时间", "时间",
    },
    "value": {
        "value", "result", "measurement", "concentration", "reading",
        "数值", "监测值", "测定值", "浓度", "值",
    },
}

DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
)


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    line: int | None = None
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "line": self.line,
            "field": self.field,
            "field_label": FIELD_LABELS.get(self.field or "", self.field),
        }


@dataclass(frozen=True)
class Observation:
    row_no: int
    site: str
    metric: str
    observed_at: str
    observed_date: str
    value: float
    value_text: str


@dataclass
class ValidationResult:
    issues: list[Issue] = field(default_factory=list)
    issue_count: int = 0
    rows_seen: int = 0
    observations: list[Observation] = field(default_factory=list)
    encoding: str | None = None

    @property
    def ok(self) -> bool:
        return self.issue_count == 0 and bool(self.observations)

    @property
    def truncated(self) -> bool:
        return self.issue_count > len(self.issues)

    def add(
        self,
        code: str,
        message: str,
        *,
        line: int | None = None,
        field_name: str | None = None,
    ) -> None:
        self.issue_count += 1
        if len(self.issues) < MAX_REPORTED_ISSUES:
            self.issues.append(Issue(code, message, line, field_name))

    def issues_as_dicts(self) -> list[dict[str, Any]]:
        return [issue.as_dict() for issue in self.issues]


def _normalise_header(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace("﻿", "")
    value = re.sub(r"\s+", " ", value.strip()).lower()
    return value


def _collapse(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def _decode(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("utf-8", raw, 0, min(len(raw), 1), "无法识别文件编码")


def _sniff_delimiter(sample: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        return dialect.delimiter
    except csv.Error:
        return ","


def _parse_observed_at(raw: str) -> datetime:
    text = raw.strip()
    if not text:
        raise ValueError("观测时刻为空")
    if text.endswith(("Z", "z")):
        raise ValueError("观测时刻不能带时区标记")
    # ISO 8601 with a T separator is common; normalise before falling back
    # to the explicit formats (which also cover Chinese and slash formats).
    iso_text = text.replace("T", " ", 1)
    candidates = (iso_text, text)
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            raise ValueError("观测时刻不能带时区偏移")
        return parsed
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError("无法解析观测时刻")


def _format_observed_at(value: datetime) -> str:
    if value.microsecond:
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _parse_value(raw: str, *, decimal_comma: bool = False) -> tuple[float, str]:
    text = raw.strip()
    if not text:
        raise ValueError("数值为空")
    if len(text) > 128:
        raise ValueError("数值文本过长")
    if "_" in text:
        raise ValueError("数值不能包含下划线")
    if decimal_comma:
        if "," in text and "." in text:
            raise ValueError("数值不能同时包含小数点和逗号")
        text = text.replace(",", ".")
    try:
        decimal_value = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("数值不是有效数字") from None
    if not decimal_value.is_finite():
        raise ValueError("数值必须是有限数字")
    if decimal_value and abs(decimal_value.adjusted()) > 30:
        raise ValueError("数值超出合理范围")
    number = float(decimal_value)
    if not math.isfinite(number):
        raise ValueError("数值超出可表示范围")
    canonical = format(decimal_value.normalize(), "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if canonical in ("", "-0"):
        canonical = "0"
    return number, canonical


def validate_file(path: str | Path, dataset_date: date) -> ValidationResult:
    """Parse and validate *path* for publication on *dataset_date*."""
    result = ValidationResult()
    try:
        raw = Path(path).read_bytes()
    except OSError:
        result.add("read_error", "无法读取上传文件")
        return result

    if not raw.strip():
        result.add("empty_file", "文件为空")
        return result

    try:
        text, encoding = _decode(raw)
    except UnicodeDecodeError:
        result.add("encoding", "文件编码无法识别，请使用 UTF-8 或 GB18030 编码的 CSV")
        return result
    result.encoding = encoding

    delimiter = _sniff_delimiter(text[:8192])
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)

    header_row: list[str] | None = None
    try:
        for row in reader:
            if any(cell.strip() for cell in row):
                header_row = row
                break
    except csv.Error as exc:
        result.add("csv_error", f"CSV 解析失败：{exc}")
        return result

    if header_row is None:
        result.add("empty_file", "文件为空或不包含表头")
        return result

    header_line = reader.line_num
    field_columns: dict[str, int] = {}
    for index, heading in enumerate(header_row):
        normalised = _normalise_header(heading)
        if not normalised:
            continue
        for field_name, aliases in HEADER_ALIASES.items():
            if normalised in aliases:
                if field_name in field_columns:
                    result.add(
                        "duplicate_header",
                        f"表头列“{heading.strip()}”与另一列重复映射到“{FIELD_LABELS[field_name]}”",
                        line=header_line,
                        field_name=field_name,
                    )
                else:
                    field_columns[field_name] = index
                break

    missing = [name for name in REQUIRED_FIELDS if name not in field_columns]
    if missing:
        labels = "、".join(FIELD_LABELS[name] for name in missing)
        result.add(
            "missing_header",
            f"缺少必需列：{labels}",
            line=header_line,
        )
        return result

    width = len(header_row)
    seen_keys: dict[tuple[str, str, str], int] = {}
    data_rows = 0
    # With a non-comma delimiter, a comma inside the value is a decimal comma.
    decimal_comma = delimiter != ","

    try:
        for row in reader:
            line = reader.line_num
            if not any(cell.strip() for cell in row):
                continue
            data_rows += 1
            result.rows_seen += 1

            if len(row) != width:
                result.add(
                    "column_count",
                    f"该行列数为 {len(row)}，与表头列数 {width} 不一致",
                    line=line,
                )
                continue

            site = _collapse(row[field_columns["site"]])
            metric = _collapse(row[field_columns["metric"]])
            observed_raw = row[field_columns["observed_at"]].strip()
            value_raw = row[field_columns["value"]]

            row_ok = True
            if not site:
                result.add("missing_site", "站点为空", line=line, field_name="site")
                row_ok = False
            if not metric:
                result.add("missing_metric", "指标为空", line=line, field_name="metric")
                row_ok = False

            observed_at: str | None = None
            try:
                parsed_at = _parse_observed_at(observed_raw)
            except ValueError as exc:
                result.add(
                    "invalid_time",
                    f"观测时刻无效：{exc}",
                    line=line,
                    field_name="observed_at",
                )
                row_ok = False
            else:
                observed_at = _format_observed_at(parsed_at)
                if parsed_at.date() != dataset_date:
                    result.add(
                        "date_mismatch",
                        (
                            f"观测日期 {parsed_at.date().isoformat()} 与所选发布日期 "
                            f"{dataset_date.isoformat()} 不一致"
                        ),
                        line=line,
                        field_name="observed_at",
                    )
                    row_ok = False

            value: float | None = None
            value_text: str | None = None
            try:
                value, value_text = _parse_value(value_raw, decimal_comma=decimal_comma)
            except ValueError as exc:
                result.add(
                    "invalid_value",
                    f"数值无效：{exc}",
                    line=line,
                    field_name="value",
                )
                row_ok = False

            if site and metric and observed_at:
                key = (site, metric, observed_at)
                first_line = seen_keys.get(key)
                if first_line is not None:
                    result.add(
                        "duplicate",
                        (
                            f"与第 {first_line} 行重复：同站点、同指标、同时刻"
                            f"（{site} / {metric} / {observed_at}）"
                        ),
                        line=line,
                    )
                    row_ok = False
                else:
                    seen_keys[key] = line

            if row_ok and value is not None and value_text is not None:
                assert observed_at is not None
                result.observations.append(
                    Observation(
                        row_no=line,
                        site=site,
                        metric=metric,
                        observed_at=observed_at,
                        observed_date=dataset_date.isoformat(),
                        value=value,
                        value_text=value_text,
                    )
                )
    except csv.Error as exc:
        result.add("csv_error", f"CSV 解析失败：{exc}", line=reader.line_num)

    if data_rows == 0:
        result.add("no_data", "文件只有表头，没有数据行")

    return result

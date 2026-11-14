"""Immutable, content-addressed storage for uploaded original files.

Files are written to a temporary name, flushed to disk, then atomically
renamed into their final content-addressed path before any database row
references them.  A published version therefore never points at a partially
written file, and a failure while storing a *new* version cannot disturb
files backing already published versions (they are immutable).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

CHUNK_SIZE = 1024 * 1024


class StorageError(Exception):
    """Raised when an uploaded file cannot be stored or verified."""


@dataclass(frozen=True)
class StoredFile:
    relative_path: str
    sha256: str
    size_bytes: int


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def new_temp_path(files_dir: str | Path) -> Path:
    tmp_dir = Path(files_dir) / ".tmp"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="upload-", dir=tmp_dir)
        os.close(fd)
        return Path(name)
    except OSError as exc:
        raise StorageError("无法创建临时存储目录，请检查数据目录权限") from exc


def discard_temp(path: str | Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def absolute_path(files_dir: str | Path, relative_path: str) -> Path:
    """Resolve a stored relative path, refusing anything outside files_dir."""
    root = Path(files_dir).resolve()
    candidate = (root / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise StorageError("存储路径非法")
    return candidate


def store_file(
    source: str | Path,
    files_dir: str | Path,
    dataset_date: date,
    original_filename: str,
) -> StoredFile:
    """Move *source* into immutable local storage and return its metadata.

    The stored name is derived from the content hash, so storing the same
    bytes again is idempotent and can never overwrite a different version.
    """
    source_path = Path(source)
    try:
        size = source_path.stat().st_size
        digest = sha256_file(source_path)
    except OSError as exc:
        raise StorageError("读取上传文件失败") from exc

    relative = Path(dataset_date.isoformat()) / f"{digest}.csv"
    destination = absolute_path(files_dir, relative.as_posix())
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError("无法创建文件存储目录，请检查数据目录权限") from exc

    if destination.exists():
        # Content-addressed: identical content is already stored.  Still
        # verify the existing file was not truncated or tampered with.
        try:
            if destination.stat().st_size == size and sha256_file(destination) == digest:
                discard_temp(source_path)
                return StoredFile(relative.as_posix(), digest, size)
        except OSError:
            pass
        # Existing file is damaged; replace it below.  No verified version
        # can currently depend on it, because publishes verify checksums.

    tmp_destination = destination.with_name(f".{digest}.{os.getpid()}.part")
    try:
        with open(source_path, "rb") as src, open(tmp_destination, "wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK_SIZE)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp_destination, destination)
        _fsync_dir(destination.parent)
    except OSError as exc:
        try:
            tmp_destination.unlink()
        except OSError:
            pass
        raise StorageError("写入原始文件失败，请重试") from exc

    # Verify what actually landed on disk before the database refers to it.
    try:
        stored_size = destination.stat().st_size
        stored_digest = sha256_file(destination)
    except OSError as exc:
        raise StorageError("写入后无法校验原始文件") from exc
    if stored_size != size or stored_digest != digest:
        raise StorageError("原始文件写入不完整，请重试")

    discard_temp(source_path)
    return StoredFile(relative.as_posix(), digest, size)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def verify_file(files_dir: str | Path, relative_path: str, expected_sha: str) -> Path:
    """Return the absolute path of an intact stored file or raise."""
    path = absolute_path(files_dir, relative_path)
    if not path.is_file():
        raise StorageError("原始文件缺失，无法发布")
    try:
        if sha256_file(path) != expected_sha:
            raise StorageError("原始文件校验失败（内容已被改动），无法发布")
    except OSError as exc:
        raise StorageError("原始文件校验失败，无法发布") from exc
    return path

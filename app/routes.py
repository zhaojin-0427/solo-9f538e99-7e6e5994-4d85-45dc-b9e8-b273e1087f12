"""HTTP routes for the admin console and the public viewer."""

from __future__ import annotations

import hmac
import secrets
import sqlite3
from datetime import date
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from . import services, storage
from .db import get_db
from .validation import validate_file

public_bp = Blueprint("public", __name__)
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _parse_dataset_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def _check_csrf() -> None:
    if not current_app.config.get("CSRF_ENABLED", True):
        return
    expected = session.get("_csrf_token", "")
    supplied = request.form.get("csrf_token", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        abort(400, description="表单已过期，请刷新页面后重试。")


@public_bp.get("/healthz")
def healthz():
    conn = get_db()
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Public viewer
# ---------------------------------------------------------------------------


@public_bp.get("/")
def index():
    conn = get_db()
    versions = services.list_published(conn)
    return render_template("public/index.html", versions=versions)


@public_bp.get("/datasets/<dataset_date>")
def dataset(dataset_date: str):
    conn = get_db()
    version = services.get_current_published(conn, dataset_date)
    if version is None:
        abort(404)
    return redirect(url_for("public.version_detail", version_id=version["id"]))


@public_bp.get("/versions/<int:version_id>")
def version_detail(version_id: int):
    conn = get_db()
    version = services.get_version(conn, version_id)
    if version is None or version["status"] != "published":
        abort(404)
    site = request.args.get("site", "").strip()
    metric = request.args.get("metric", "").strip()
    page = services.parse_page(request.args.get("page"))
    detail = services.fetch_observations(
        conn,
        version_id,
        site=site,
        metric=metric,
        page=page,
        per_page=current_app.config["PER_PAGE"],
    )
    return render_template(
        "public/version_detail.html", version=version, detail=detail
    )


@public_bp.get("/versions/<int:version_id>/download")
def download(version_id: int):
    conn = get_db()
    version = services.get_version(conn, version_id)
    if version is None or version["status"] != "published":
        abort(404)
    try:
        path = storage.verify_file(
            current_app.config["FILES_DIR"],
            version["stored_path"],
            version["sha256"],
        )
    except storage.StorageError:
        abort(503, description="原始文件暂时不可用，请稍后重试。")
    return send_file(
        path,
        mimetype="text/csv",
        as_attachment=True,
        download_name=version["original_filename"],
        conditional=True,
    )


# ---------------------------------------------------------------------------
# Admin console
# ---------------------------------------------------------------------------


@admin_bp.get("/")
def dashboard():
    conn = get_db()
    versions = services.list_versions(conn)
    groups = services.build_admin_groups(versions)
    default_date = current_app.config.get("ADMIN_DATASET_DATE") or date.today().isoformat()
    return render_template(
        "admin/dashboard.html", groups=groups, default_date=default_date
    )


@admin_bp.post("/upload")
def upload():
    _check_csrf()
    dataset_date = _parse_dataset_date(request.form.get("dataset_date"))
    if dataset_date is None:
        flash("请选择有效的发布日期。", "error")
        return redirect(url_for("admin.dashboard"))

    upload_file = request.files.get("file")
    if upload_file is None or not upload_file.filename:
        flash("请选择要上传的 CSV 文件。", "error")
        return redirect(url_for("admin.dashboard"))

    original_name = Path(upload_file.filename).name
    # Keep the display/download name bounded and free of control characters.
    original_name = "".join(
        ch for ch in original_name if ch.isprintable() and ch not in "\\/"
    )[:200] or "observations.csv"
    if not original_name.lower().endswith((".csv", ".txt")):
        flash("仅支持 CSV 文件。", "error")
        return redirect(url_for("admin.dashboard"))

    files_dir = current_app.config["FILES_DIR"]
    temp_path = storage.new_temp_path(files_dir)
    try:
        upload_file.save(temp_path)
    except OSError:
        storage.discard_temp(temp_path)
        flash("上传文件保存失败，请重试。", "error")
        return redirect(url_for("admin.dashboard"))

    result = validate_file(temp_path, dataset_date)
    if not result.ok:
        storage.discard_temp(temp_path)
        return (
            render_template(
                "admin/upload_result.html",
                accepted=False,
                dataset_date=dataset_date.isoformat(),
                filename=original_name,
                result=result,
            ),
            422,
        )

    try:
        stored = storage.store_file(
            temp_path, files_dir, dataset_date, original_name
        )
    except storage.StorageError as exc:
        storage.discard_temp(temp_path)
        flash(str(exc), "error")
        return redirect(url_for("admin.dashboard"))

    conn = get_db()
    try:
        version_id = services.create_candidate(
            conn,
            dataset_date,
            original_name,
            stored,
            result.observations,
        )
    except sqlite3.Error:
        current_app.logger.exception("candidate insert failed")
        flash("数据库写入失败，候选未创建；已发布版本不受影响。", "error")
        return redirect(url_for("admin.dashboard"))

    flash(
        f"校验通过：{len(result.observations)} 条明细，已形成待发布候选。",
        "success",
    )
    return redirect(url_for("admin.candidate_detail", version_id=version_id))


@admin_bp.get("/versions/<int:version_id>")
def candidate_detail(version_id: int):
    conn = get_db()
    version = services.get_version(conn, version_id)
    if version is None:
        abort(404)
    page = services.parse_page(request.args.get("page"))
    detail = services.fetch_observations(
        conn,
        version_id,
        page=page,
        per_page=current_app.config["PER_PAGE"],
    )
    latest = conn.execute(
        """
        SELECT MAX(version_no) AS latest_no FROM versions WHERE dataset_date = ?
        """,
        (version["dataset_date"],),
    ).fetchone()["latest_no"]
    newer = conn.execute(
        "SELECT * FROM versions WHERE dataset_date = ? ORDER BY version_no DESC LIMIT 1",
        (version["dataset_date"],),
    ).fetchone()
    current_published = services.get_current_published(conn, version["dataset_date"])
    conflict = latest is not None and int(latest) > version["version_no"]
    return render_template(
        "admin/candidate_detail.html",
        version=version,
        detail=detail,
        conflict=conflict,
        newer=newer,
        current_published=current_published,
    )


@admin_bp.post("/versions/<int:version_id>/publish")
def publish(version_id: int):
    _check_csrf()
    conn = get_db()
    outcome = services.publish_candidate(
        conn, current_app.config["FILES_DIR"], version_id
    )
    if outcome.status == "published":
        flash(outcome.message, "success")
        return redirect(
            url_for("public.version_detail", version_id=version_id)
        )
    if outcome.status == "conflict":
        flash(
            f"{outcome.message}：已存在版本 v{outcome.newer['version_no']}，"
            "请改为发布该日期的最新候选。",
            "error",
        )
        return redirect(url_for("admin.candidate_detail", version_id=version_id))
    if outcome.status == "already_published":
        flash(outcome.message, "info")
        return redirect(url_for("public.version_detail", version_id=version_id))
    if outcome.status == "not_found":
        abort(404)

    flash(outcome.message or "发布失败。", "error")
    return redirect(url_for("admin.candidate_detail", version_id=version_id))


@admin_bp.get("/versions/<int:version_id>/download")
def admin_download(version_id: int):
    conn = get_db()
    version = services.get_version(conn, version_id)
    if version is None:
        abort(404)
    try:
        path = storage.verify_file(
            current_app.config["FILES_DIR"],
            version["stored_path"],
            version["sha256"],
        )
    except storage.StorageError as exc:
        abort(503, description=str(exc))
    return send_file(
        path,
        mimetype="text/csv",
        as_attachment=True,
        download_name=version["original_filename"],
        conditional=True,
    )

"""Application factory."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, render_template

from . import db as database
from . import routes


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)

    data_dir = Path(os.environ.get("DATA_DIR", Path(app.root_path).parent / "data"))
    app.config.from_mapping(
        DATA_DIR=str(data_dir),
        DATABASE=os.environ.get("DATABASE", str(data_dir / "water.db")),
        FILES_DIR=os.environ.get("FILES_DIR", str(data_dir / "files")),
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_BYTES", 64 * 1024 * 1024)),
        PER_PAGE=100,
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-water-quality-console"),
        ADMIN_DATASET_DATE=os.environ.get("ADMIN_DATASET_DATE", ""),
    )
    if config:
        app.config.update(config)

    Path(app.config["FILES_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    database.init_db(app.config["DATABASE"])
    database.register_db(app)

    @app.context_processor
    def inject_globals():
        from .routes import _csrf_token

        return {"csrf_token": _csrf_token()}

    app.register_blueprint(routes.public_bp)
    app.register_blueprint(routes.admin_bp)

    @app.template_filter("filesize")
    def filesize(value: int | None) -> str:
        if not value:
            return "0 B"
        units = ("B", "KB", "MB", "GB")
        amount = float(value)
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
            amount /= 1024
        return f"{amount:.1f} GB"

    @app.template_filter("datetime_local")
    def datetime_local(value: str | None) -> str:
        if not value:
            return "—"
        try:
            from datetime import datetime

            parsed = datetime.fromisoformat(value)
            return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")
        except ValueError:
            return value

    @app.errorhandler(400)
    def bad_request(error):
        return (
            render_template(
                "public/not_found.html",
                title="请求无效",
                message=getattr(error, "description", None)
                or "请求无法处理，请返回后重试。",
            ),
            400,
        )

    @app.errorhandler(404)
    def not_found(_error):
        return (
            render_template("public/not_found.html"),
            404,
        )

    @app.errorhandler(413)
    def too_large(_error):
        return (
            render_template(
                "public/not_found.html",
                title="文件过大",
                message="上传文件超过允许的大小上限。",
            ),
            413,
        )

    @app.errorhandler(500)
    def server_error(_error):
        return (
            render_template(
                "public/not_found.html",
                title="服务器错误",
                message="操作未能完成，请稍后重试。已发布数据不受影响。",
            ),
            500,
        )

    @app.errorhandler(503)
    def unavailable(error):
        return (
            render_template(
                "public/not_found.html",
                title="数据暂时不可用",
                message=getattr(error, "description", None)
                or "所需文件暂时不可用，请稍后重试。",
            ),
            503,
        )

    return app

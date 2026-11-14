"""WSGI entry point used by gunicorn in Docker."""

from app import create_app

app = create_app()

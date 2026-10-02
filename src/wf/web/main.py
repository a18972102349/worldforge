"""ASGI entry point for ``uvicorn wf.web.main:app``."""
from .app import app, create_app
__all__ = ["app", "create_app"]

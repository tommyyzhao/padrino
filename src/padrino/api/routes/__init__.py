"""HTTP route modules for the Padrino API."""

from __future__ import annotations

from padrino.api.routes.admin import router as admin_router
from padrino.api.routes.admin_anticheat import router as admin_anticheat_router

__all__ = ["admin_anticheat_router", "admin_router"]

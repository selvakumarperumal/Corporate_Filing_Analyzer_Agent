"""The transport layer — HTTP and Socket.IO.

Thin by design: it resolves who is asking (:mod:`api.dependencies`), assembles
the routers the domains own (:mod:`api.routes`), and streams a run back over a
socket (:mod:`api.socket`). No analysis, no persistence, no rules of its own.
"""

from api.routes import api_router
from api.socket import register_handlers

__all__ = ["api_router", "register_handlers"]

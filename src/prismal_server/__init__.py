"""prismal-server — reference host for the prismal engine.

A thin FastAPI/ASGI process that boots the engine once and serves it over
REST/SSE plus the inbound A2A endpoints. It orchestrates the engine through
exactly four seams and reimplements none of its logic (see ``CLAUDE.md``).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("prismal-server")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]

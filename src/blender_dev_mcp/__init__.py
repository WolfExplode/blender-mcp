"""Blender addon development and debugging over the Model Context Protocol."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("blender-dev-mcp")
except PackageNotFoundError:
    # Not installed (e.g. running from a source checkout)
    __version__ = "unknown"

from .server import BlenderConnection, get_blender_connection

__all__ = ["BlenderConnection", "get_blender_connection", "__version__"]

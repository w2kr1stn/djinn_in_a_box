"""Djinn in a Box - CLI tools for managing AI development containers."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("djinn-in-a-box")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

__all__ = ["__version__"]

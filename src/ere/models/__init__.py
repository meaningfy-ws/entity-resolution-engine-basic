"""ERE domain models: resolver-specific concepts."""

from . import resolver
from .exceptions import ConflictError

__all__ = ["resolver", "ConflictError"]

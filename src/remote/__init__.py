"""Encrypted GitHub Actions remote-compute integration."""

from .protocol import PROTOCOL_VERSION, ProtocolError

__all__ = ["PROTOCOL_VERSION", "ProtocolError"]

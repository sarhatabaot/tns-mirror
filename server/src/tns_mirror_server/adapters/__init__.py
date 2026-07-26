"""Adapters — the only modules that talk to the network, the disk, or SQL.

Importing this package registers the shipped sources.
"""

from .memory import FakeSource, MemoryStore
from .source_tns import TnsPublicObjects
from .store_postgres import PostgresStore

__all__ = ["FakeSource", "MemoryStore", "PostgresStore", "TnsPublicObjects"]

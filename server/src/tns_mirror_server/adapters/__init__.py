"""Adapters — the only modules that talk to the network, the disk, or SQL.

Importing this package registers the shipped sources.
"""

from .memory import FakeHistoricSource, FakeSource, MemoryStore
from .source_tns import TnsPublicObjects
from .source_tns_historic import TnsHistoricSne
from .store_postgres import PostgresStore

__all__ = [
    "FakeHistoricSource",
    "FakeSource",
    "MemoryStore",
    "PostgresStore",
    "TnsHistoricSne",
    "TnsPublicObjects",
]

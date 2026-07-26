"""tns-mirror-server — mirror the TNS public objects catalogue into SQL.

The server downloads TNS's daily snapshot and hourly deltas, authenticates to
TNS, and owns the database it writes to. It performs no cone search and no
cross-matching: querying the mirror is ``tns-mirror-client``'s job, and the
schema is the contract between them.
"""

__version__ = "1.0.0"

#: The project's major version IS this number: v1.x.y speaks schema v1, for
#: both the server image and the client library. A schema break bumps both.
SCHEMA_VERSION = 1

__all__ = ["SCHEMA_VERSION", "__version__"]

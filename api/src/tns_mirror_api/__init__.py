"""tns-mirror-api — optional read-only HTTP access to a tns-mirror catalogue.

A transport in front of ``tns-mirror-client``, not a second implementation of
the cone search. Ships as a Docker image; the sync server stays responsible for
downloading and writing, and this connects with the read-only role.
"""

__version__ = "1.0.2"

__all__ = ["__version__"]

"""tns-mirror-client — typed, read-only access to a tns-mirror database.

    from tns_mirror_client import TnsMirror

    with TnsMirror(dsn=os.environ["TNS_RO_DSN"]) as tns:
        hit = tns.nearest(ra=203.1, dec=10.2, radius_arcsec=3.0)
        if hit:
            print(hit.name, hit.type, hit.redshift)

The library is the schema, packaged as a Python API. It reads; it never writes,
and it carries no TNS credentials — those belong to the server. Connect with the
read-only role the mirror's operator gives you.

**Versioning.** This is 1.x, and it speaks schema v1. Pin
``tns-mirror-client>=1,<2`` and you are insulated from additive schema changes;
a breaking one bumps both majors together. The client checks the mirror's
published schema version on connect and refuses to guess.
"""

from .client import SCHEMA_VERSION, TnsMirror
from .errors import MirrorUnavailable, SchemaVersionError, TnsMirrorClientError
from .geometry import angular_separation_deg, bounding_box
from .models import MirrorMeta, TnsObject

__version__ = "1.0.0"

__all__ = [
    "SCHEMA_VERSION",
    "MirrorMeta",
    "MirrorUnavailable",
    "SchemaVersionError",
    "TnsMirror",
    "TnsMirrorClientError",
    "TnsObject",
    "__version__",
    "angular_separation_deg",
    "bounding_box",
]

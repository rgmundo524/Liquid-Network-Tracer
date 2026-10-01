"""Shared SQLite storage compatibility rules for mutable local databases."""

import sqlite3


def wal_runtime_safe(version=None):
    """Use WAL only with SQLite's 2026 WAL-reset correction.

    https://sqlite.org/wal.html#walreset names 3.51.3 and later, plus
    maintained 3.44.6 and 3.50.7 backports. Other older branches remain in
    DELETE/FULL mode rather than assuming a distributor applied that patch.
    """
    version = sqlite3.sqlite_version_info if version is None else version
    return (version >= (3, 51, 3) or
            (version[:2] == (3, 44) and version >= (3, 44, 6)) or
            (version[:2] == (3, 50) and version >= (3, 50, 7)))

"""Everything the tool service keeps in its database.

Only the tables and their migrations exist so far: the tool service is a
contract, and no store reads or writes them yet, so `open()` refuses every
backend.
"""

from __future__ import annotations

from pathlib import Path

from openengine.persistence import ServicePersistence, Store

PERSISTENCE = ServicePersistence[Store]("tools", Path(__file__).parent, {})

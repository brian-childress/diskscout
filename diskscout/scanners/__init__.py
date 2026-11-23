from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from diskscout.config import Settings
from diskscout.inventory import Inventory
from diskscout.protect import Guard, V2cRun


@dataclass
class Context:
    settings: Settings
    inv: Inventory
    guard: Guard
    runs: list[V2cRun]
    pool: ThreadPoolExecutor
    log: object

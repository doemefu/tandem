"""The long-lived state bundle one server process passes to its role tools."""

from __future__ import annotations

from dataclasses import dataclass

from tandem.child import ChildRegistry
from tandem.config import Config
from tandem.runlog import RunLog
from tandem.sessions import SessionRegistry
from tandem.state import RollingCounter, StatePaths


@dataclass(frozen=True)
class Runtime:
    """Everything a role tool needs; constructed once in ``tandem.__main``."""

    cfg: Config
    paths: StatePaths
    registry: ChildRegistry
    counter: RollingCounter
    sessions: SessionRegistry
    log: RunLog

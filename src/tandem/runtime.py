"""The long-lived state bundle one server process passes to its role tools."""

from __future__ import annotations

from dataclasses import dataclass, field

from tandem.child import ChildRegistry
from tandem.config import Config
from tandem.runlog import RunLog
from tandem.sessions import SessionRegistry
from tandem.state import RollingCounter, StatePaths


@dataclass(frozen=True)
class Runtime:
    """Everything a role tool needs; constructed once in ``tandem.__main__``.

    ``versions`` is a lazy once-per-process cache of the two CLI version
    probes (unset until the first child spawn or a ``bridge_status`` call -
    probing at server start would race the host's startup timeout). The
    versions ride the first run's ``consult_started``/``delegate_started``
    event; the cache pins them for the process lifetime, so drift across a
    long-lived server surfaces that process's day-one truth - comparing
    events across server restarts is what shows an auto-updated CLI.
    """

    cfg: Config
    paths: StatePaths
    registry: ChildRegistry
    counter: RollingCounter
    sessions: SessionRegistry
    log: RunLog
    versions: dict[str, str | None] = field(default_factory=dict)

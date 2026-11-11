"""Entrypoint of the tandem stdio MCP server.

``TANDEM_ROLE`` selects the tool set: "thinker" (mounted in Vibe, consults
Claude) or "worker" (mounted in Claude Code, delegates to Vibe). A missing or
unknown role is a configuration error: the server refuses to start (exit 2).
Diagnostics go to stderr - stdout is MCP protocol. Sync bootstrap (config,
state layout, signal handlers) runs before the event loop; everything inside
``anyio`` is async. Child cleanup on any teardown path is owned by
``tandem.child.run_child``'s shielded finally and the signal choreography.
"""

from __future__ import annotations

import sys
import threading

import anyio

from tandem import __version__
from tandem.config import Config
from tandem.errors import ConfigError

EXIT_CONFIG_ERROR = 2


def main() -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"tandem {__version__}: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    anyio.run(_serve, cfg)
    return 0


async def _serve(cfg: Config) -> None:
    # Imported here so `python -m tandem` with a bad role never loads the stack.
    from tandem.child import ChildRegistry
    from tandem.proctree import install_shutdown_handlers, spawn_watchdog
    from tandem.runlog import RunLog
    from tandem.runtime import Runtime
    from tandem.server import build_server
    from tandem.sessions import SessionRegistry
    from tandem.state import RollingCounter, StatePaths

    paths = StatePaths(cfg.state_dir)
    await anyio.to_thread.run_sync(paths.ensure_layout)
    registry = ChildRegistry(paths)
    counter = RollingCounter(paths.counters_file)
    sessions = SessionRegistry(paths)
    log = RunLog(paths.runs_dir, role=cfg.role)
    await anyio.to_thread.run_sync(lambda: RunLog.prune(paths.runs_dir))

    requested = threading.Event()
    install_shutdown_handlers(registry, requested)
    watchdog_pid = await spawn_watchdog(cfg.state_dir, grace_s=cfg.grace_seconds, poll_s=2.0)
    log.event("server_started", role=cfg.role, state_dir=str(cfg.state_dir), watchdog_pid=watchdog_pid)

    runtime = Runtime(cfg=cfg, paths=paths, registry=registry, counter=counter, sessions=sessions, log=log)
    server = build_server(runtime)
    try:
        # Shutdown signals kill children in the handler and terminate this
        # process with the signal's default disposition - the serve loop
        # cannot be unwound cooperatively (blocking stdin read). This try/
        # finally covers the graceful stdin-EOF path.
        await server.run_stdio_async()
    finally:
        registry.terminate_own()
        log.event("server_stopped")
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())

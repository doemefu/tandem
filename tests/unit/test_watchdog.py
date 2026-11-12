"""The detached watchdog reaps owned child groups when its server dies."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from tandem.child import ChildRegistry, RegistryEntry
from tandem.state import StatePaths, pid_alive

SRC = str(Path(__file__).resolve().parents[2] / "src")


def test_watchdog_kills_owned_children_on_server_death(tmp_path: Path) -> None:
    paths = StatePaths(tmp_path / "state")
    paths.ensure_layout()
    fake_server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    registry = ChildRegistry(paths)
    registry.register(
        RegistryEntry(
            run_id="r1",
            pgid=child.pid,
            started_at=time.time(),
            argv_hash="x",
            owner_pid=fake_server.pid,
        )
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(part for part in (SRC, env.get("PYTHONPATH", "")) if part)
    dog = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tandem.watchdog",
            "--state-dir",
            str(paths.root),
            "--server-pid",
            str(fake_server.pid),
            "--poll-s",
            "0.2",
            "--grace-s",
            "0.5",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        fake_server.kill()  # SIGKILL: no handler in the fake server can run
        fake_server.wait(timeout=5)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            child.poll()  # reap if exited so a zombie does not read as alive
            if not pid_alive(child.pid):
                break
            time.sleep(0.05)
        assert not pid_alive(child.pid)
        dog.wait(timeout=8)
        assert dog.returncode == 0
        assert registry.persistent() == ()
    finally:
        for proc in (dog, fake_server, child):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

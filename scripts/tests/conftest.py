"""conftest.py — the whole suite runs against a machine that does not exist.

Every module here builds its own fixture database and hands the paths it needs to
the code explicitly, but the code defaults every path it is *not* handed to the
real home. A fixture that forgets one — or the TUI, which syncs on its own timer
and passes nothing — then reads the developer's actual OpenCode install, and a
test that is green on this laptop is red on a runner that has no such tree (or the
reverse). Two guards already existed, one per module, and both stopped short of
the same default: ``db.PLUGIN_EVENTS``. The real ``plugin-events.jsonl`` named
three plugins no fixture declared, so the Plugins tab grew rows nobody built and
two sync tests saw a ``dcp-compress`` command that was never in their config.

``HOME`` joins that list for the same reason and with more force: the host loads
skills from ``~/.claude`` and ``~/.agents``, which sit outside the config home,
so an unpinned home folds a developer's real skill store into a fixture's
inventory. That is not a path a fixture can be expected to pass — it is the
default of a stdlib call, reached from the sync worker on the TUI's own timer as
well — so it is pinned where every default is.

Pointing each default at a path that does not exist turns a forgotten argument
into the same, loud failure on every machine. That is the whole point of the
fixture below, and the reason it lives here instead of being copied per module.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocut_db as db          # noqa: E402


@pytest.fixture(autouse=True)
def no_real_machine(monkeypatch, tmp_path):
    """No test may read the machine it runs on.

    ``HOST_CONFIG_DIR`` and ``HOST_DB`` were guarded per module before this file;
    the hook sidecar (``PLUGIN_EVENTS``) was not, so every ``ocut-sync.run()`` in
    the suite — the fixture builds and the app's own sync alike — folded the
    developer's real plugin events into a fixture database. Guarding all three in
    one place is what makes a missing ``host_config`` / ``host_db`` /
    ``plugin_events`` argument fail identically everywhere instead of only on a
    clean runner.
    """
    monkeypatch.setattr(db, "HOST_CONFIG_DIR", tmp_path / "no-such-config-home")
    monkeypatch.setattr(db, "HOST_DB", tmp_path / "no-such-host.db")
    monkeypatch.setattr(db, "PLUGIN_EVENTS",
                        tmp_path / "no-such-plugin-events.jsonl")
    # ``Path.home()`` resolves HOME on POSIX and USERPROFILE on Windows; the CI
    # matrix is Linux-only but pinning both keeps the fixture honest elsewhere.
    # Set, not created: the reader treats a missing directory as an empty root,
    # which is exactly the state a machine without those trees is in.
    monkeypatch.setenv("HOME", str(tmp_path / "no-such-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "no-such-home"))

"""The settings layer that survives the plugin: `~/.config/ocut/config.toml`, `OCUT_*`, `DEFAULTS`.

Three promises, each of which is a way a settings layer can lie to you:

* **zero-config still works** — no file is the normal case, and the normal case
  creates no artifact;
* **a value you typed is never ignored** — every failure names the file or variable
  and the key, and the shipped default is never silently substituted for a value the
  file already contains;
* **a key that reaches nothing is not a setting** — every key is read by the
  interface it is documented for, so a knob cannot exist only inside the dict.

The precedence is environment **>** file **>** defaults: the resolver applies the file
first and lets the environment overwrite it, which is the opposite order to the one
the plugin-era registry used, so it is pinned here rather than remembered.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import ocut_db as db                                    # noqa: E402
from textual.widgets import DataTable                   # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ocut_settings_tui", str(SCRIPTS / "ocut-tui.py"))
tui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tui)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ocut_drill as drill_tests                   # noqa: E402
import test_ocut_tui as tui_tests                       # noqa: E402

LONG_NAME = drill_tests.LONG_NAME


@pytest.fixture
def no_env(monkeypatch):
    """No ambient override for any key, so a developer's shell cannot be a fixture."""
    for env in list(db.ENV_NAMES.values()) + ["OCUT_CONFIG", "XDG_CONFIG_HOME"]:
        monkeypatch.delenv(env, raising=False)
    return monkeypatch


@pytest.fixture
def tracker(tmp_path):
    """A synced database — the drill suite's, because it carries a name the cap bites."""
    host = tmp_path / "opencode.db"
    drill_tests.build_host(host)
    cfg = drill_tests.build_config(tmp_path / "cfg")
    out = tmp_path / "usage.db"
    db.load_sync().run(db_path=out, host_db=host, host_config=cfg, quiet=True)
    return out


# --- zero-config and precedence --------------------------------------------

def test_no_file_is_the_normal_case_and_creates_nothing(tmp_path, no_env):
    """`load_config` on a path that does not exist is not an error and not a write."""
    path = tmp_path / "ocut" / "config.toml"
    got = db.load_config(path)
    assert got == db.DEFAULTS, got
    assert not path.parent.exists(), "zero-config created a directory it was only reading"
    assert not path.exists()
    # A copy, so a caller editing its own dict cannot move the module's defaults.
    assert got is not db.DEFAULTS
    got["top_n"] = 1
    assert db.DEFAULTS["top_n"] != 1


def test_environment_beats_the_file_and_the_file_beats_the_default(tmp_path, no_env):
    """The order the resolver actually applies, pinned from both sides."""
    path = tmp_path / "config.toml"
    path.write_text("top_n = 3\n")
    assert db.load_config(path)["top_n"] == 3, "the file was ignored"
    no_env.setenv("OCUT_TOP_N", "7")
    assert db.load_config(path)["top_n"] == 7, "the environment did not win"
    # Keys neither source mentions keep their default.
    assert db.load_config(path)["sync_secs"] == db.DEFAULTS["sync_secs"]


@pytest.mark.parametrize("env,value,expect", [
    ("OCUT_TOP_N", "9", 9),
    ("OCUT_NAME_CAP", "33", 33),
    ("OCUT_REFRESH_SECS", "2.5", 2.5),
    ("OCUT_SYNC_SECS", "45", 45),
])
def test_an_environment_value_is_parsed_as_a_number(tmp_path, no_env, env, value, expect):
    """int first, then float: `2.5` is a period, `0.5` is the floor, not a typo."""
    no_env.setenv(env, value)
    key = next(k for k, v in db.ENV_NAMES.items() if v == env)
    got = db.load_config(tmp_path / "absent.toml")[key]
    assert got == expect and isinstance(got, type(expect)), (env, got)


# --- the failure modes, each named by source -------------------------------

@pytest.mark.parametrize("key", sorted(db.DEFAULTS))
def test_every_key_is_bounded_on_both_sides_and_says_so(key, no_env):
    low, high, _kind = db._BOUNDS[key]
    for bad in (low - 1, high + 1):
        with pytest.raises(db.ConfigError) as exc:
            db._setting_value(key, bad, "unit test")
        message = str(exc.value)
        assert key in message, message
        assert f"{low}..{high}" in message, message
        assert "unit test" in message, message
    # The endpoints themselves are in range — a floor that rejects its own number
    # would make the bound untypable.
    for good in (low, high):
        assert db._setting_value(key, good, "unit test") == good


@pytest.mark.parametrize("key,bad", [
    ("top_n", True),        # a bool is an int subclass: `true` must not become 1
    ("top_n", "5"),         # a string of digits is not a number in a parsed file
    ("top_n", 2.5),        # an int key does not take a fraction
    ("refresh_secs", "fast"),
    ("name_cap", 1.5),
])
def test_a_value_of_the_wrong_type_is_rejected_not_coerced(key, bad):
    with pytest.raises(db.ConfigError) as exc:
        db._setting_value(key, bad, "unit test")
    assert key in str(exc.value) and "must be" in str(exc.value), str(exc.value)


def test_an_environment_value_that_is_not_a_number_is_reported_as_one(tmp_path, no_env):
    no_env.setenv("OCUT_TOP_N", "many")
    with pytest.raises(db.ConfigError) as exc:
        db.load_config(tmp_path / "absent.toml")
    message = str(exc.value)
    assert "OCUT_TOP_N" in message and "many" in message, message
    # …and the bound still applies through the environment, with the variable as
    # the named source rather than the file.
    no_env.setenv("OCUT_TOP_N", "0")
    with pytest.raises(db.ConfigError) as exc:
        db.load_config(tmp_path / "absent.toml")
    assert "OCUT_TOP_N environment variable" in str(exc.value), str(exc.value)


def test_an_unknown_key_is_refused_and_names_the_real_ones(tmp_path, no_env):
    """A typo must not look like a setting that was ignored."""
    path = tmp_path / "config.toml"
    path.write_text("topn = 4\n")
    with pytest.raises(db.ConfigError) as exc:
        db.load_config(path)
    message = str(exc.value)
    assert "topn" in message and "top_n" in message, message
    for key in db.DEFAULTS:
        assert key in message, message


def test_a_file_that_does_not_parse_names_the_file(tmp_path, no_env):
    path = tmp_path / "ocut" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("top_n = \n")
    with pytest.raises(db.ConfigError) as exc:
        db.load_config(path)
    assert str(path) in str(exc.value) and "TOML" in str(exc.value), str(exc.value)


# --- where the file lives --------------------------------------------------

def _config_path_under(env_extra, home):
    """Re-import the module in a clean interpreter: CONFIG_PATH is an import-time
    expression, so the only way to watch it resolve is to resolve it again."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)}
    env.update(env_extra)
    out = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); "
         "import ocut_db; print(ocut_db.CONFIG_PATH)"],
        env=env, capture_output=True, text=True, check=True, timeout=60).stdout.strip()
    return Path(out)


def test_the_file_is_found_where_the_platform_says(tmp_path):
    home = tmp_path / "home"
    assert _config_path_under({}, home) == home / ".config" / "ocut" / "config.toml"
    xdg = tmp_path / "xdg"
    assert _config_path_under({"XDG_CONFIG_HOME": str(xdg)}, home) \
        == xdg / "ocut" / "config.toml"
    direct = tmp_path / "elsewhere.toml"
    assert _config_path_under({"OCUT_CONFIG": str(direct),
                               "XDG_CONFIG_HOME": str(xdg)}, home) == direct


# --- every key is read by the thing it documents ---------------------------

def test_the_defaults_are_the_constants_the_interface_shipped_with():
    """`DEFAULTS` replaces class constants; if it drifts, the layer changed the
    product instead of describing it."""
    assert db.DEFAULTS["top_n"] == tui.TrackerApp.DASH_TOP
    assert db.DEFAULTS["log_limit"] == tui.TrackerApp.USAGE_LOG_LIMIT
    assert db.DEFAULTS["detail_limit"] == tui.TrackerApp.DETAIL_LIMIT
    assert db.DEFAULTS["name_cap"] == tui.WideTableMixin.NAME_CAP


def test_every_setting_reaches_the_interface_and_not_just_the_dict(tracker, no_env):
    cfg = dict(db.DEFAULTS, top_n=2, log_limit=33, detail_limit=44, name_cap=15,
               refresh_secs=7, sync_secs=11)

    async def body(pilot, app):
        assert app.DASH_TOP == 2, app.DASH_TOP
        assert app.USAGE_LOG_LIMIT == 33, app.USAGE_LOG_LIMIT
        assert app.DETAIL_LIMIT == 44, app.DETAIL_LIMIT
        assert app.name_cap == 15, app.name_cap
        periods = sorted(t._interval for t in app._timers)
        assert 7 in periods or 7.0 in periods, periods
        assert 11 in periods or 11.0 in periods, periods

        # The cap is spent on a real cell at a narrow width, and the number a
        # pushed screen reports is the configured one, not the class default.
        await tui_tests.activate(app, pilot, "tab-tools")
        table = app.query_one("#t-tools", DataTable)
        cells = [str(table.get_cell_at((i, 0))) for i in range(table.row_count)]
        cut = [c for c in cells if c.endswith("…")]
        assert cut, cells
        assert all(len(c) == 15 for c in cut), cut
        assert any(c == LONG_NAME[:14] + "…" for c in cells), cells

        app.push_screen(tui.HistoryScreen("tool", LONG_NAME))
        await pilot.pause(0.5)
        screen = app.screen
        assert screen.name_cap == 15, screen.name_cap
        assert type(screen).__name__ == "HistoryScreen", screen
    tui_tests.drive(lambda: tui.TrackerApp(str(tracker), config=cfg), body,
                    size=(52, 40))


def test_the_shipped_timers_are_the_configured_periods(tracker, no_env):
    """No config passed: the app falls back to `DEFAULTS`, so the timers must be
    the documented 5s and 30s — the fallback is the same layer, not a second one."""
    async def body(pilot, app):
        periods = sorted(t._interval for t in app._timers)
        assert db.DEFAULTS["refresh_secs"] in periods, periods
        assert db.DEFAULTS["sync_secs"] in periods, periods
        assert app.name_cap == db.DEFAULTS["name_cap"], app.name_cap
    tui_tests.drive(lambda: tui.TrackerApp(str(tracker)), body)


def test_a_broken_file_stops_the_app_and_is_not_replaced_by_defaults(tmp_path, no_env,
                                                                      capsys):
    """Starting with defaults would leave the owner editing a file nothing reads."""
    path = tmp_path / "config.toml"
    path.write_text("top_n = 999999\n")
    # `main` asks whether the database exists before it reads any settings, so the
    # file has to be there for the settings failure to be the one reported.
    (tmp_path / "usage.db").write_bytes(b"")
    original, db.CONFIG_PATH = db.CONFIG_PATH, path
    try:
        rc = tui.main(["--db", str(tmp_path / "usage.db")])
    finally:
        db.CONFIG_PATH = original
    captured = capsys.readouterr()
    assert rc == 2, (rc, captured.err)
    assert "your settings are not usable" in captured.err, captured.err
    assert "top_n" in captured.err and "999999" in captured.err, captured.err
    assert "Traceback" not in captured.err, captured.err

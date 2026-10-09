"""Settings: precedence, the file's failure modes, and the plain-language table.

The promise being tested is the boring one — *zero-config still works* — plus the
two ways a settings layer lies to you: it applies a value that was rejected, or it
ignores a value that was typed. Both are made visible here.
"""

from __future__ import annotations

import json
import os

import pytest

import settings as st


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A config path in the sandbox, and no ambient env for any key."""
    path = tmp_path / "skillt-config.json"
    monkeypatch.setenv(st.CONFIG_PATH_ENV, str(path))
    for spec in st.REGISTRY:
        monkeypatch.delenv(st.env_name(spec["key"]), raising=False)
    return path


def test_no_config_file_is_the_normal_case_and_changes_nothing(cfg):
    """The tool must stay usable before anyone configures it."""
    assert not cfg.exists()
    values, problems = st.effective()
    assert problems == []
    for spec in st.REGISTRY:
        assert values[spec["key"]] == (spec["default"], "default"), spec["key"]


def test_precedence_is_flag_then_file_then_env_then_default(cfg):
    cfg.write_text(json.dumps({"view.days": 45}), encoding="utf-8")
    resolved, _ = st.effective({"view.days": 90})
    assert resolved["view.days"] == (90, "flag --days")

    resolved, _ = st.effective()
    assert resolved["view.days"] == (45, "config file")

    monkey = dict(os.environ, **{st.env_name("view.days"): "60"})
    resolved, _ = st.effective(env=monkey)
    # the file still outranks the environment, and the environment outranks default
    assert resolved["view.days"] == (45, "config file")
    cfg.unlink()
    resolved, _ = st.effective(env=monkey)
    assert resolved["view.days"] == (60, f"env {st.env_name('view.days')}")


def test_a_rejected_value_is_reported_not_disguised_as_a_default(cfg):
    """`view.days: "thirty"` must not read back as though the default were chosen.

    Falling back silently is how a settings layer becomes untrustworthy: the user
    sees a number and believes it is theirs.
    """
    cfg.write_text(json.dumps({"view.days": "thirty", "view.limit": -3}), encoding="utf-8")
    resolved, problems = st.effective()
    assert resolved["view.days"][1] == "config file (rejected)"
    assert resolved["view.limit"][1] == "config file (rejected)"
    assert resolved["view.days"][0] == st.spec("view.days")["default"]
    assert len(problems) == 2, problems
    assert all("view.days" in p or "view.limit" in p for p in problems), problems


def test_an_unknown_key_is_named_and_ignored(cfg):
    """A typo is a fact about the file, not a reason to stop reading it."""
    cfg.write_text(json.dumps({"view.dayz": 7, "view.days": 21}), encoding="utf-8")
    resolved, problems = st.effective()
    assert resolved["view.days"] == (21, "config file")
    assert len(problems) == 1 and "unknown key 'view.dayz'" in problems[0], problems


@pytest.mark.parametrize("body", ["{not json", '["a list"]', ""])
def test_an_unparsable_file_never_falls_back_quietly(cfg, body):
    cfg.write_text(body, encoding="utf-8")
    values, problems = st.read_file(cfg)
    assert values == {}
    assert problems, "a broken file reported nothing"
    assert "defaults are in force" in problems[0], problems


def test_write_set_and_unset_round_trip(cfg):
    code, msg = st.write_value("view.days", "45")
    assert code == 0, msg
    assert cfg.is_file() and json.loads(cfg.read_text(encoding="utf-8")) == {"view.days": 45}
    assert st.value("view.days") == 45

    # 0600 like the database beside it: this file sits in a shared data directory.
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"

    code, msg = st.write_value("view.limit", "20")
    assert code == 0, msg
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"view.days": 45, "view.limit": 20}

    code, msg = st.reset_value("view.days")
    assert code == 0, msg
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"view.limit": 20}
    assert st.value("view.days") == st.spec("view.days")["default"]


def test_setting_a_value_that_is_not_a_setting_fails_loudly(cfg):
    assert st.write_value("view.days", "nope")[0] == 2
    assert st.write_value("view.dayz", "7")[0] == 2
    code, msg = st.write_value("view.dayz", "7")
    assert "view.days" in msg, f"a near-miss key must suggest the real one: {msg}"
    assert not cfg.exists(), "a rejected write must leave the file alone"


def test_a_file_that_cannot_be_parsed_is_never_overwritten(cfg):
    """`config set` on a broken file refuses, rather than "repairing" by replacing.

    The owner's bytes are the evidence of what went wrong; replacing them to apply
    one setting would destroy the only copy of the mistake.
    """
    cfg.write_text("{oops", encoding="utf-8")
    code, msg = st.write_value("view.days", "30")
    assert code == 2 and "refusing to rewrite" in msg, msg
    assert cfg.read_text(encoding="utf-8") == "{oops"


def test_every_key_carries_an_explanation_in_both_languages():
    """A setting nobody can describe is a setting nobody can use.

    The README gate is the outside check; this is the inside one, so a key cannot
    ship with the English filled in and the Chinese blank.
    """
    for spec in st.REGISTRY:
        assert spec["en"].strip().endswith((".", "?")), spec["key"]
        assert any(0x4E00 <= ord(c) <= 0x9FFF for c in spec["zh"]), (
            f"{spec['key']} has no Chinese explanation")
        assert spec["key"].count(".") == 1, "group.key, one level, no deeper"


# --- the `skillt config` surface --------------------------------------------
from conftest import load_module  # noqa: E402

tui = load_module("skill-tui.py", "skill_tui_cli")


def _config_argv(*words):
    """Build the args object `skillt config …` hands to the dispatcher."""
    return tui.parse_args(["--cli", "config", *words])


def test_config_list_prints_value_origin_and_explanation(cfg, capsys):
    assert tui._cli_config(_config_argv("list")) == 0
    out = capsys.readouterr().out
    for spec in st.REGISTRY:
        assert spec["key"] in out, spec["key"]
        assert spec["en"] in out, f"{spec['key']} listed without its explanation"
    assert "from default" in out


def test_config_get_reports_the_origin_and_the_json_carries_both(cfg, capsys):
    cfg.write_text('{"view.days": 45}', encoding="utf-8")
    assert tui._cli_config(_config_argv("get", "view.days")) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("view.days = 45") and "config file" in line

    assert tui._cli_config(_config_argv("--json", "get", "view.days")) == 0
    import json as _json
    doc = _json.loads(capsys.readouterr().out)
    assert doc == {"key": "view.days", "value": 45, "origin": "config file", "default": 30}


def test_a_refused_value_is_visible_in_config_list(cfg, capsys):
    """The one screen that proves the 30 on the dashboard is not the user's 45."""
    cfg.write_text('{"view.days": "thirty"}', encoding="utf-8")
    assert tui._cli_config(_config_argv("list")) == 0
    out = capsys.readouterr().out
    assert "config file (rejected)" in out, out
    assert "is not a number" in out, "the problem itself must be printed, not just flagged"


def test_config_set_get_unset_round_trip_through_the_command(cfg, capsys):
    assert tui._cli_config(_config_argv("set", "view.days", "14")) == 0
    assert st.value("view.days") == 14
    capsys.readouterr()

    tui._cli_config(_config_argv("get", "view.days"))
    assert "14" in capsys.readouterr().out

    assert tui._cli_config(_config_argv("unset", "view.days")) == 0
    assert st.value("view.days") == st.spec("view.days")["default"]


def test_config_path_prints_the_file_it_would_use(cfg, capsys):
    assert tui._cli_config(_config_argv("path")) == 0
    assert capsys.readouterr().out.strip() == str(cfg)


def test_config_explain_answers_in_both_languages(cfg, capsys):
    assert tui._cli_config(_config_argv("explain", "view.min_uses")) == 0
    out = capsys.readouterr().out
    spec = st.spec("view.min_uses")
    assert spec["en"] in out and spec["zh"] in out
    assert st.env_name("view.min_uses") in out and "--min-uses" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["set"],
        ["get"],
        ["explain"],
        ["get", "view.dayz"],
        ["set", "view.dayz", "7"],
        ["frobnicate"],
    ],
)
def test_config_refuses_every_way_it_can_be_misspelled(cfg, capsys, argv):
    assert tui._cli_config(_config_argv(*argv)) == 2
    err = capsys.readouterr().err
    assert err.strip(), f"{argv} failed with no message"


def test_config_never_writes_outside_its_own_file(cfg, capsys, monkeypatch):
    """The privacy invariant is about paths too: `config set` may not open a DB,
    and the file it writes is the one in the caller's sandbox, never OpenCode's
    config directory."""
    def no_db(*a, **k):
        raise AssertionError("config must not open the database")

    monkeypatch.setattr(tui.db, "open_db", no_db)
    tui._cli_config(_config_argv("set", "view.limit", "5"))
    assert st.value("view.limit") == 5
    assert not str(cfg).startswith(tui.db.CONFIG_DIR), (
        f"the settings file must not live in {tui.db.CONFIG_DIR}")

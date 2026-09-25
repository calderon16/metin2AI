import pytest

from qa.config import ConfigError, QaConfig, load_config
from qa.scenario.loader import ScenarioError, dump_scenario, parse_scenario, save_scenario


def test_roundtrip():
    text = """
name: rt
seed: 5
setup:
  - set_gold: 100
  - give_item: {vnum: 27001, count: 2}
steps:
  - wait: 500
  - kill_monster: {vnum: 101, count: 2}
    label: dogs
    expect:
      - alive
  - buy_item: {vnum: 27001}
    expect_error: NOT_ENOUGH_GOLD
assert:
  - server_errors: 0
  - gold: {min: 0}
"""
    sc = parse_scenario(text)
    assert sc.steps[0].args == {"ms": 500}
    assert sc.asserts[0].args == {"equals": 0}
    assert sc.steps[2].expect_error == "NOT_ENOUGH_GOLD"
    again = parse_scenario(dump_scenario(sc))
    assert again.model_dump() == sc.model_dump()
    assert sc.setup[0].to_command() == "/qa gold 100"
    assert sc.setup[1].to_command() == "/qa item 27001 2"


@pytest.mark.parametrize("bad,msg", [
    ("name: x\nsteps:\n  - fly_to_moon: 1\n", "Bilinmeyen adım"),
    ("name: x\nsteps:\n  - walk_to: {x: 1}\n", "walk_to"),
    ("name: x\nassert:\n  - nope: 1\n", "Bilinmeyen assertion"),
    ("name: x\nsetup:\n  - rm_rf: 1\n", "Bilinmeyen setup"),
    ("name: 'bad name!'\n", "name"),
    ("name: x\nunknown_key: 1\n", "unknown_key"),
    ("name: x\nsteps:\n  - {walk_to: {x: 1, y: 2}, wait: 3}\n", "tek bir anahtar"),
])
def test_invalid_scenarios_rejected(bad, msg):
    with pytest.raises(ScenarioError, match=msg):
        parse_scenario(bad)


def test_save_scenario_guards(tmp_path):
    save_scenario(tmp_path, "ok1", "name: ok1\nsteps:\n  - wait: 1\n")
    with pytest.raises(ScenarioError, match="zaten var"):
        save_scenario(tmp_path, "ok1", "name: ok1\nsteps:\n  - wait: 1\n")
    with pytest.raises(ScenarioError, match="aynı olmalı"):
        save_scenario(tmp_path, "ok2", "name: other\n")
    with pytest.raises(ScenarioError, match="Geçersiz senaryo adı"):
        save_scenario(tmp_path, "../evil", "name: evil\n")


def test_production_refused():
    with pytest.raises(ConfigError):
        QaConfig(env="production").check_environment()


def test_account_prefix():
    c = QaConfig()
    c.check_account("AI_QA_007")
    with pytest.raises(ConfigError):
        c.check_account("RealPlayer")


def test_load_config(tmp_path, monkeypatch):
    p = tmp_path / "qa.toml"
    p.write_text('env = "staging"\n[bridge]\nmode = "tcp"\nport = 5000\n[build.commands]\nserver = ["make"]\n')
    monkeypatch.setenv("QA_ACCOUNT_PASSWORD", "s3cret")
    c = load_config(p)
    assert (c.env, c.bridge.mode, c.bridge.port, c.accounts.password) == ("staging", "tcp", 5000, "s3cret")
    assert c.build.commands == {"server": ["make"]}
    assert c.root == tmp_path
    p.write_text("[bridge]\nnope = 1\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_client_bridge_is_python2_compatible():
    """integration/client/qa_bridge.py klasik istemcinin Python 2.7'siyle çalışmalı."""
    import warnings
    from pathlib import Path

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        lib2to3 = pytest.importorskip("lib2to3")
        try:
            from lib2to3 import pygram, pytree
            from lib2to3.pgen2 import driver
        except FileNotFoundError:
            pytest.skip("Bu Python kurulumunda lib2to3 gramer dosyası eksik")
    src = (Path(__file__).resolve().parents[1] / "integration/client/qa_bridge.py").read_text(encoding="utf-8")
    driver.Driver(pygram.python_grammar_no_print_statement, convert=pytree.convert).parse_string(src)

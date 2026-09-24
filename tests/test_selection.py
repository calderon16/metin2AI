import subprocess

import pytest

from qa.selection import match, select


def names(service, files):
    return service.select_affected(files)["selected"]


def test_match_patterns():
    assert match("shop*.cpp", "game/src/shop_manager.cpp")
    assert match("shop*.cpp", "game\\src\\shop.cpp")
    assert not match("shop*.cpp", "game/src/char_shop.h")
    assert match("qa/*", "qa/engine/behaviours.py")
    assert match("src/game/*", "server/src/game/char.cpp")  # alt dizinde de eşleşir
    assert not match("qa/*", "integration/qa_bridge.py")


def test_rule_and_covers_selection(service):
    sel = service.select_affected(["game/src/shop.cpp"])
    assert set(sel["selected"]) == {"npc_shop_buy_sell", "shop_insufficient_gold", "smoke_login_walk"}
    assert any("covers" in r for r in sel["reasons"]["npc_shop_buy_sell"])
    assert "kill_mob_pickup" in sel["skipped"]


def test_scenario_file_change_selects_itself(service):
    assert "quest_dog_hunt" in names(service, ["scenarios/quest_dog_hunt.yaml"])


def test_star_rule_selects_all(service):
    assert len(names(service, ["qa/engine/behaviours.py"])) == len(service.list_scenarios())


def test_fallback_and_docs(service):
    sel = service.select_affected(["some/unknown_file.cpp"])
    assert sel["unmatched_files"] == ["some/unknown_file.cpp"] and sel["selected"] == ["smoke_login_walk"]
    sel = service.select_affected(["README.md"])
    assert sel["unmatched_files"] == [] and sel["selected"] == ["smoke_login_walk"]  # sadece always
    assert service.select_affected([])["selected"] == []


def test_run_affected(service):
    res = service.run_affected(["uiQuest.py"], seed=3)
    assert res["ran"] and res["ok"]
    assert {r["scenario"] for r in res["results"]} == {"quest_dog_hunt", "smoke_login_walk"}
    assert not service.run_affected(["uiQuest.py"], dry_run=True)["ran"]


def test_git_changed_files(service, tmp_path):
    repo = tmp_path / "src"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    run("init", "-q")
    run("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "init")
    (repo / "shop.cpp").write_text("x")
    service.cfg.source_repo = repo
    sel = service.select_affected()
    assert sel["changed_files"] == ["shop.cpp"] and "npc_shop_buy_sell" in sel["selected"]


def test_custom_rules(service):
    service.cfg.selection.rules = {"pet_*.cpp": ["scenario:use_equip_item"]}
    service.cfg.selection.always = []
    assert names(service, ["game/src/pet_system.cpp"]) == ["use_equip_item"]


def test_markdown_summary():
    from qa.store.report import markdown_summary

    md = markdown_summary([{"scenario": "a", "result": "PASSED", "run_id": "QA-1", "summary": "x|y"},
                           {"scenario": "b", "result": "FAILED", "run_id": "QA-2", "summary": "boom"}], "T")
    assert "✅ PASSED: 1" in md and "❌ FAILED: 1" in md and "x\\|y" in md

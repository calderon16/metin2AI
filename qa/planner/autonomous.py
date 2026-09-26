"""Otonom keşif ajanı: LLM planlar, behaviour motoru oynar, oracle doğrular.

    Hedef ("Dükkan sistemini oyuncu gibi kullan, edge-case ara")
      → LLM: tool çağrıları (walk_to, buy_item, check, report_finding ...)
      → ExplorationManager: gerçek oyuncu yolundan yürütme + durum farkı + sunucu hataları
      → LLM: sonuçları değerlendirir, sonraki adıma karar verir
      → finish: bulgu raporu + (isteğe bağlı) üretilmiş regression senaryosu + doğrulama çalıştırması

Güvenlik: LLM yalnızca oyuncu behaviour'larını, gözlem/kontrol araçlarını ve ilk adımdan önce
kısıtlı `/qa` hazırlığını görür. Shell, SQL, dosya erişimi yoktur. Bütçe: adım, tur, token.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from ..config import QaConfig
from ..engine import behaviours as _b  # noqa: F401  (BEHAVIOURS registry'i doldurur)
from ..engine.executor import BEHAVIOURS
from ..oracle.assertions import describe_assertions
from ..scenario.loader import load_scenario
from ..scenario.runner import ScenarioRunner, SetupError
from ..scenario.schema import SETUP_OPS
from ..session import BridgeFactory
from ..store.db import Store
from .explore import ExplorationManager, suggest_checklist
from .budget import BudgetedProvider, BudgetExceeded, LLMBudget
from .llm import LLMError, LLMProvider, Message, ToolCall, ToolResult, ToolSpec

META_TOOLS = {"observe", "check", "qa_setup", "report_finding", "finish"}
# LLM'e gösterilmeyen ince ayar parametreleri (varsayılanları kullanılır) — her istekte tekrar gönderilen
# araç tanımlarını küçültür
HIDDEN_PARAMS = {"timeout_ms", "search_radius", "radius", "potion_below_pct", "potion_vnum", "auto_potion",
                 "required", "tolerance", "range", "use_skill", "match"}
NOISY_EVENTS = {"damage_dealt", "damage_taken"}
# Tek ajanlı keşifte işe yaramayanlar (çok oyunculu aksiyonlar, modelin göremediği ekran görüntüsü) —
# her istekte gönderilen araç listesini kısa tutar
# Ticaret tek ajanla gerçek bir oyuncuya (ör. TESTR) yapılabilir; grup komutları headless'ta henüz yok
EXCLUDED_BEHAVIOURS = {"party_invite", "party_kick", "party_accept", "party_decline", "party_leave",
                       "screenshot", "wait_for_event"}

SYSTEM_PROMPT = """Sen bir Metin2 QA mühendisisin. Gerçek bir QA karakterini (AI_QA_*) oyunda oynatarak
verilen sistemi test ediyor ve hata arıyorsun. Türkçe düşün ve raporla.

Kurallar:
- Her tool çağrısı gerçek oyuncu aksiyonudur (paket → sunucu → DB). Oyunu başka yoldan değiştiremezsin.
- `qa_setup` (yang/item/seviye verme) YALNIZCA ilk oyuncu adımından önce kullanılabilir.
- Her adımdan sonra sonucu incele: `diff` (durum farkı), `new_server_errors`, `new_qa_assert_failures`,
  `failures`. Beklenmeyen her şey bir bulgu adayıdır.
- Kuralları `check` ile doğrula (ör. yang eksiye düşmemeli, item kaybolmamalı, ödül tek sefer verilmeli).
  `check` sonuçları üretilecek regression senaryosuna girer.
- Reddedilmesi GEREKEN bir işlemi denediğinde adıma `expect_error` ver (ör. NOT_ENOUGH_GOLD).
- Kendi hatalı çağrını (yanlış vnum, uzaktaki NPC vb.) bulgu sanma; bulgu = oyunun yanlış davranması.
- Her gerçek bulguyu `report_finding` ile kanıtıyla (adım, beklenen, gerçekleşen) kaydet.
- `finish` çağırmadan önce en az bir gerçek oyuncu aksiyonu yap; yalnız gözlem yeterli değildir.
- Edge-case'lere odaklan: sınır değerler, yetersiz yang, dolu envanter, tekrar eden işlemler,
  yeniden bağlanma (reconnect), ölüp dirilme, pencereyi kapatıp açma.
- Bütçen sınırlı: {max_steps} oyuncu adımı. Bitirirken `finish` çağır ve kısa bir özet yaz.
- Her turda en az bir tool çağır; birden fazla bağımsız çağrı yapabilirsin."""


PLAYER_SYSTEM_PROMPT = """Sen Metin2 oynayan normal bir oyuncusun (karakterin {account}); oynarken oyunu da test
ediyorsun. Türkçe düşün.

Kurallar:
- Her şeyi gerçek bir oyuncu gibi yap. Bir yere gitmek için YÜRÜ (walk_to, go_to_npc, go_to_quest_npc,
  talk_npc). Uzaktaki bir NPC'ye (Silah Satıcısı, Demirci ...) adıyla go_to_npc ile git. Işınlanma, Işınlayıcı
  NPC, /qa komutu ya da hile yok; bunlar zaten engellidir.
- Bir oyuncunun yanına go_to_player ile git; go_to_npc yalnız NPC'ler içindir.
- NPC ile konuşunca adım sonucundaki windows.dialog.options listesini oku ve select_dialog(index=N) ile seç;
  listede olmayan bir seçenek uydurma. Aynı çağrıyı tekrarlayıp duruyorsan başka bir şey dene.
- Ticaret yalnız GM'lerle (ör. TESTR) yapılabilir; başka oyuncuyla ticaret açma.
- Oyuncunun yaptığı her şeyi yap: görev al ve bitir (observe → quests: available/active/ready), canavar kes,
  düşenleri topla (pickup), daha iyi ekipmanı giy (equip_item), iksir iç (use_item), satıcıdan al/sat
  (talk_npc → buy_item/sell_item), demircide + bas (refine_item; önce confirm=false ile ücret ve şansa bak),
  seviye atlayınca durum puanı (stat_up) ve beceri puanı (skill_up) ver, becerileri kullan (use_skill).
- Öncelik: 1) HP düşükse iksir iç ya da uzaklaş 2) biten (ready) görevi teslim et 3) aktif görevin hedefi
  4) yeni görev al 5) seviyene uygun canavar kes 6) envanter dolunca sat; yang birikince ekipman al / + bas.
- Oyunun yanlış davrandığını görürsen (yanlış ödül, kaybolan eşya, sunucu hatası) report_finding ile kanıtıyla
  kaydet. Kendi hatalı çağrın bulgu değildir.
- Bu oturumda en çok {max_steps} adımın var. Bitirirken `finish` ile ne yaptığını ve SIRADAKİ planını yaz;
  bir sonraki oturum oradan devam eder.
- Her turda en az bir tool çağır; birden fazla bağımsız çağrı yapabilirsin."""


def _param_schema(annotation: Any) -> dict[str, Any] | None:
    a = str(annotation).replace(" ", "")
    if a.startswith("dict") or "dict[" in a:
        return None
    if a.startswith("list[int]"):
        return {"type": "array", "items": {"type": "integer"}}
    if a.startswith("list"):
        return {"type": "array", "items": {"type": "string"}}
    for key, typ in (("bool", "boolean"), ("int", "integer"), ("float", "number"), ("str", "string")):
        if a.split("|")[0] == key or a == key:
            return {"type": typ}
    return {"type": "string"}


def behaviour_tools(player: bool = False) -> list[ToolSpec]:
    tools = []
    for name, b in BEHAVIOURS.items():
        if name in EXCLUDED_BEHAVIOURS or name in META_TOOLS:
            continue
        props: dict[str, Any] = {}
        required = []
        for p in b.params:
            if p.name in HIDDEN_PARAMS:
                continue
            sch = _param_schema(p.annotation)
            if sch is None:
                continue
            if p.default is inspect.Parameter.empty:
                required.append(p.name)
            props[p.name] = sch
        if not player:   # oyuncu modunda test beklentisi yok; küçük modeller "nil" gibi değerlerle adımı bozuyordu
            props["expect_error"] = {"type": "string", "description": "beklenen red kodu"}
        doc = (b.doc or name).split("\n")[0].strip()
        tools.append(ToolSpec(name, doc, {"type": "object", "properties": props, "required": required}))
    return tools


def meta_tools() -> list[ToolSpec]:
    assertion_names = ", ".join(a["name"] for a in describe_assertions())
    return [
        ToolSpec("observe", "Karakterin anlık durumu: oyuncu, envanter, görevler, açık pencereler, yakın varlıklar, "
                 "son sistem mesajları.", {"type": "object", "properties": {}}),
        ToolSpec("check", "Oyun kurallarını doğrula. asserts: YAML liste, ör. "
                 "'- gold: {min: 0}\\n- item_count: {vnum: 27001, delta: 3}\\n- server_errors: 0'. "
                 f"Kullanılabilir: {assertion_names}. delta başlangıca (setup sonrası) göredir.",
                 {"type": "object", "properties": {"asserts": {"type": "string"}}, "required": ["asserts"]}),
        ToolSpec("qa_setup", "Test hazırlığı — YALNIZCA ilk oyuncu adımından önce. ops: YAML liste, ör. "
                 f"'- set_gold: 1000\\n- give_item: {{vnum: 27001, count: 5}}'. İşlemler: {', '.join(SETUP_OPS)}.",
                 {"type": "object", "properties": {"ops": {"type": "string"}}, "required": ["ops"]}),
        ToolSpec("report_finding", "Bulunan hatayı kanıtıyla kaydet.",
                 {"type": "object", "properties": {
                     "title": {"type": "string"},
                     "description": {"type": "string"},
                     "severity": {"type": "string", "enum": ["critical", "major", "bug", "minor", "note"]},
                     "step": {"type": "integer", "description": "Hatanın görüldüğü adım numarası"},
                     "expected": {"type": "string"},
                     "actual": {"type": "string"}},
                  "required": ["title", "description", "severity"]}),
        ToolSpec("finish", "Keşfi bitir. summary: ne test edildi, ne bulundu (kısa).",
                 {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}),
    ]


def _compact(obj: Any, limit: int = 1500) -> Any:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) <= limit:
        return obj
    return {"truncated": True, "json": s[:limit] + "…"}


def _prune(d: dict[str, Any]) -> dict[str, Any]:
    """Boş/None alanları at: model için anlam taşımaz, token harcar."""
    return {k: v for k, v in d.items() if v not in (None, [], {}, "")}


def _mini_state(st: dict[str, Any]) -> dict[str, Any]:
    return _prune({k: st.get(k) for k in ("hp", "max_hp", "sp", "gold", "level", "exp", "x", "y", "map", "dead",
                                          "skills")})


def _events_view(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Hasar olaylarını sayıya indir, diğer olayları (ölüm, drop, pencere ...) olduğu gibi bırak."""
    counts: dict[str, int] = {}
    kept = []
    for e in events:
        if e["event"] in NOISY_EVENTS:
            counts[e["event"]] = counts.get(e["event"], 0) + 1
        else:
            kept.append({"event": e["event"], **(e.get("data") or {})})
    return _prune({"events": kept[-8:], "counts": counts})


def _entities_view(rows: list[dict[str, Any]], n: int = 12) -> list[list[Any]]:
    """[vid, tür, vnum, ad, mesafe] — anahtar tekrarı olmadan"""
    return [[r["vid"], r["type"], r["vnum"], r.get("name", ""), r.get("distance")] for r in rows[:n]]


# Görev mektupları yalnız bilgi (sol kenardaki ikonlar); kapatılacak bir pencere değil — modele gösterilmez
HIDDEN_WINDOWS = {"quest_letters"}
_COMMAND_TEXT = re.compile(r"^(MRQ_\w+.*|[A-Za-z_]+(\s+-?\d+)*)$")


def _windows_view(windows: Any) -> dict[str, Any]:
    """Açık pencereler ve içerikleri: diyalog metni + numaralı seçenekler, dükkân ürünleri, yükseltme bilgisi.
    Model seçeneği tahmin etmesin, listeden index ile seçsin."""
    items = windows.items() if isinstance(windows, dict) else [(w.get("name"), w) for w in windows or []]
    out: dict[str, Any] = {}
    for name, w in items:
        if name in HIDDEN_WINDOWS or not isinstance(w, dict):
            continue
        if name == "dialog":
            out[name] = _prune({"text": str(w.get("text") or "")[-400:],
                                "options": [f"{i}: {o}" for i, o in enumerate(w.get("options") or [])],
                                "hint": "select_dialog(index=N) ile seç" if w.get("options") else None})
        elif name == "shop":
            out[name] = _prune({"npc_vid": w.get("npc_vid"),
                                "items[slot:vnum:ad:fiyat]": [f"{i['slot']}:{i['vnum']}:{i.get('name', '')}:{i.get('price', '')}"
                                                              for i in w.get("items") or []]})
        else:
            out[name] = _prune({k: v for k, v in w.items() if k != "name" and not isinstance(v, (dict, list))
                                or k in ("materials", "options")})
    return out


def _messages_view(messages: list[dict[str, Any]], n: int = 5) -> list[str]:
    """Oyuncunun okuduğu sistem mesajları; istemciye giden komutlar (MRQ_*, setblockmode 0 ...) atılır."""
    texts = [str(m.get("text", "")) for m in messages]
    return [t for t in texts if t.strip() and not _COMMAND_TEXT.match(t.strip())][-n:]


def _item_label(i: dict[str, Any]) -> str:
    """"Kırmızı İksir(K) (27001)" — ad yoksa yalnız vnum."""
    name = str(i.get("name") or "")
    return f"{name} ({i['vnum']})" if name and name != str(i["vnum"]) else str(i["vnum"])


def _hunting_view(state: dict[str, Any]) -> list[str]:
    """Bu haritada seviyene yakın canavarlar (rehberden) — "ad (sv N)"; kill_monster(name=...) ile avlanır."""
    from ..engine import npcdir

    lv = state.get("level") or 1
    mobs = [m for m in npcdir.mobs_on_map(state.get("map")) if m.get("level") and abs(m["level"] - lv) <= 6]
    return [f"{m['name']} (sv {m['level']})" for m in sorted(mobs, key=lambda m: m["level"])][:10]


def _observe_view(o: dict[str, Any]) -> dict[str, Any]:
    inv = o.get("inventory") or {}
    return _prune({
        "state": _mini_state(o.get("state") or {}),
        "inventory": [f"{_item_label(i)} x{i['count']} @{i['slot']}" for i in inv.get("items", [])],
        "equipment": {k: _item_label(v) for k, v in (inv.get("equipment") or {}).items()},
        "av_yerleri (kill_monster name=...)": _hunting_view(o.get("state") or {}),
        "quests": o.get("quests"),
        "windows": _windows_view(o.get("windows") or {}),
        "nearby[vid,tür,vnum,ad,mesafe]": _entities_view(o.get("nearby") or []),
        "messages": _messages_view(o.get("messages") or []),
    })


def _check_view(r: dict[str, Any]) -> dict[str, Any]:
    return {"passed": r.get("passed"), "results": [
        _prune({"name": a.get("name"), "passed": a.get("passed"), "expected": a.get("expected"),
                "actual": None if a.get("passed") else a.get("actual")}) for a in r.get("results", [])]}


def _step_view(r: dict[str, Any]) -> dict[str, Any]:
    """Adım sonucunu LLM için özetle (yalnızca karar vermek için gerekenler)."""
    out = _prune({k: r.get(k) for k in ("step", "status", "result", "error", "diff", "new_server_errors",
                                         "new_qa_assert_failures")})
    fails = [_prune({k: f.get(k) for k in ("kind", "name", "expected", "actual", "message")})
             for f in r.get("failures", [])]
    if fails:
        out["failures"] = fails
    out.update(_events_view(r.get("client_events", [])))
    out["state"] = _mini_state(r.get("state") or {})
    wv = _windows_view(r.get("windows") or {})
    if wv:
        out["windows"] = wv
    return out


LOOP_WINDOW = 10        # son kaç oyuncu çağrısına bakılır
LOOP_REPEAT = 3         # aynı çağrı bu kadar tekrar ederse uyarı
LOOP_MAX_WARNINGS = 3   # uyarıya rağmen sürerse oturum biter


def _call_signature(call: ToolCall) -> str:
    args = {k: v for k, v in (call.args or {}).items() if k != "expect_error"}
    return f"{call.name}({json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)})"


@dataclass
class ExploreBudget:
    max_steps: int = 60
    max_turns: int | None = None             # varsayılan: max_steps * 2 + 10
    max_total_tokens: int | None = None
    history_turns: int = 10                  # bağlamda tutulan son tur sayısı
    max_idle_turns: int = 2                  # tool çağırmayan ardışık tur sınırı


@dataclass
class ExploreOutcome:
    run_id: str
    result: str
    summary: str
    stop_reason: str
    agent_summary: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    saved_scenario: str | None = None
    validation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class AutoExplorer:
    def __init__(self, cfg: QaConfig, store: Store, provider: LLMProvider, factory: BridgeFactory | None = None):
        self.cfg, self.store = cfg, store
        # Her çağrı bütçeden geçer: günlük/aylık tavan, hız sınırı ve kullanım kaydı
        self.budget = LLMBudget(store, cfg.explorer)
        self.provider = BudgetedProvider(provider, self.budget)
        self.factory = factory or BridgeFactory(cfg)
        self.explorer = ExplorationManager(cfg, store, self.factory)

    def run(self, goal: str, *, budget: ExploreBudget | None = None, account: str | None = None,
            seed: int | None = None, setup: list[Any] | None = None, save_as_scenario: str | None = None,
            validate: bool = True, player: bool = False, should_stop: Any = None,
            exclude_tools: set[str] | None = None) -> ExploreOutcome:
        """player=True: oyuncu modu — /qa reset ve qa_setup yok (karakter olduğu yerden devam eder), oyuncu
        sistem istemi. should_stop(): her turdan önce sorulur; True ise oturum 'preempted' ile biter."""
        b = budget or ExploreBudget()
        max_turns = b.max_turns or b.max_steps * 2 + 10
        self.budget.check()  # bütçe dolmuşsa oyuna hiç girme
        if player and setup:
            raise ValueError("Oyuncu modunda hazırlık (setup) kullanılamaz")
        start = self.explorer.start(goal, account, seed, setup, reset=not player)
        if not start["ok"]:
            return ExploreOutcome(start["run_id"], "ERROR", start["error"], "start_failed")
        run_id = start["run_id"]
        self.provider.run_id = run_id
        rs = self.explorer.sessions[run_id]["rs"]
        transcript = rs.artifacts.path("llm_transcript.jsonl").open("w", encoding="utf-8")

        tools = behaviour_tools(player) + meta_tools()
        if player:
            tools = [t for t in tools if t.name != "qa_setup"]
        if exclude_tools:
            tools = [t for t in tools if t.name not in exclude_tools]
        tool_names = {t.name for t in tools}
        if player:
            system = PLAYER_SYSTEM_PROMPT.format(max_steps=b.max_steps, account=start.get("account") or account)
            intro = f"HEDEF:\n{goal}\n"
        else:
            system = SYSTEM_PROMPT.format(max_steps=b.max_steps)
            intro = (f"TEST HEDEFİ:\n{goal}\n\nFikir listesi (edge-case'ler):\n"
                     + yaml.safe_dump(suggest_checklist(goal), allow_unicode=True, sort_keys=False))
        first = Message("user", text=(
            intro
            + "\nBaşlangıç durumu:\n"
            + json.dumps(_observe_view({"state": start["state"], "inventory": start["inventory"],
                                        "nearby": start["nearby"]}), ensure_ascii=False)
            + "\n\nPlanını kısaca düşün, sonra tool çağrılarıyla test etmeye başla."))
        history: list[Message] = [first]
        # Tur 0: eğitim verisi (training/export_dataset.py) modelin gördüğü bağlamı birebir kurabilsin
        transcript.write(json.dumps({"turn": 0, "system": system, "user": first.text, "goal": goal,
                                     "provider": self.provider.name, "model": self.provider.model,
                                     "max_steps": b.max_steps, "history_turns": b.history_turns},
                                    ensure_ascii=False) + "\n")
        findings: list[dict[str, Any]] = []
        usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "total_tokens": 0}
        steps = turns = idle = 0
        recent: list[str] = []          # son oyuncu çağrılarının imzaları (döngü tespiti)
        loop_warnings = 0
        stop_reason, agent_summary = "max_turns", ""
        error: str | None = None

        try:
            while turns < max_turns:
                if should_stop is not None and should_stop():
                    stop_reason = "preempted"
                    break
                turns += 1
                try:
                    reply = self.provider.chat(system, history, tools)
                except BudgetExceeded as e:
                    # Planlı durma: o ana kadar yapılanlar raporlanır, run hata sayılmaz
                    stop_reason, agent_summary = "budget_exhausted", agent_summary or str(e)
                    break
                except LLMError as e:
                    error, stop_reason = str(e), "llm_error"
                    break
                for k in usage:
                    usage[k] += reply.usage.get(k, 0)
                history.append(reply.message)
                calls = reply.message.tool_calls
                if not calls:
                    idle += 1
                    if idle > b.max_idle_turns:
                        stop_reason = "no_tool_calls"
                        break
                    history.append(Message("user", text="Devam etmek için bir tool çağır ya da `finish` ile bitir."))
                    continue
                idle = 0
                results: list[ToolResult] = []
                finished = False
                for call in calls:
                    if call.name == "finish":
                        if steps == 0:
                            results.append(ToolResult(call.name, {"ok": False,
                                "error": "En az bir gerçek oyuncu aksiyonu yapmadan keşif bitirilemez."}, call.id))
                            continue
                        agent_summary = str(call.args.get("summary", ""))
                        results.append(ToolResult(call.name, {"ok": True}, call.id))
                        finished = True
                        continue
                    if call.name in tool_names - META_TOOLS and steps >= b.max_steps:
                        results.append(ToolResult(call.name, {"ok": False, "error": "Adım bütçesi doldu; finish çağır."},
                                                  call.id))
                        continue
                    res = self._execute(run_id, call, tool_names, findings)
                    if call.name not in META_TOOLS and res.get("ok", True) is not False:
                        steps += 1
                    results.append(ToolResult(call.name, _compact(res), call.id))
                note = ""
                for call, res in zip(calls, results):
                    if call.name not in META_TOOLS:
                        c = res.content if isinstance(res.content, dict) else {}
                        outcome = {k: c.get(k) for k in ("status", "error", "result")}
                        if isinstance(c.get("windows"), dict) and "dialog" in c["windows"]:
                            outcome["dialog"] = c["windows"]["dialog"].get("text")
                        recent.append(_call_signature(call) + "=>" + json.dumps(outcome, ensure_ascii=False,
                                                                                sort_keys=True, default=str))
                recent = recent[-LOOP_WINDOW:]
                repeated = sorted({sig for sig in recent if recent.count(sig) >= LOOP_REPEAT})
                if repeated and not finished:
                    loop_warnings += 1
                    if loop_warnings > LOOP_MAX_WARNINGS:
                        stop_reason = "stuck_loop"
                        agent_summary = agent_summary or f"Döngüde takıldı: {repeated[0].split('=>')[0]}"
                        transcript.write(json.dumps({"turn": turns, "text": reply.message.text,
                                                     "calls": [{"name": c.name, "args": c.args} for c in calls],
                                                     "results": [{"name": r.name, "content": r.content} for r in results],
                                                     "note": "stuck_loop", "usage": reply.usage},
                                                    ensure_ascii=False, default=str) + "\n")
                        break
                    note = (f"DİKKAT: aynı çağrıyı aynı sonuçla tekrar ediyorsun ({repeated[0].split('=>')[0]}); "
                            "işe yaramıyor. Durumu oku "
                            "(observe), farklı bir yol dene ya da işi bırakıp finish ile nedenini yaz.")
                    recent.clear()
                elif steps >= b.max_steps and not finished:
                    note = f"Adım bütçesi ({b.max_steps}) doldu. Şimdi `finish` çağır."
                elif turns % 10 == 0:
                    note = f"İlerleme: {steps}/{b.max_steps} adım, {len(findings)} bulgu, tur {turns}/{max_turns}."
                history.append(Message("tool", text=note, tool_results=results))
                transcript.write(json.dumps({
                    "turn": turns, "text": reply.message.text,
                    "calls": [{"name": c.name, "args": c.args} for c in calls],
                    "results": [{"name": r.name, "content": r.content} for r in results],
                    "note": note, "usage": reply.usage}, ensure_ascii=False, default=str) + "\n")
                transcript.flush()
                if finished:
                    stop_reason = "finished"
                    break
                if b.max_total_tokens and usage["total_tokens"] >= b.max_total_tokens:
                    stop_reason = "token_budget"
                    break
                if steps >= b.max_steps + 3:  # finish çağırmadan bütçeyi zorlamaya devam ediyorsa
                    stop_reason = "step_budget"
                    break
                history = self._trim(history, b.history_turns)
        finally:
            transcript.close()

        list_cost = round(self.budget.price(usage), 4)
        extra = {"agent_summary": agent_summary, "stop_reason": stop_reason,
                 "llm": {"provider": self.provider.name, "model": self.provider.model, "usage": usage,
                         "turns": turns, "steps": steps, "free_tier": self.cfg.explorer.free_tier,
                         "cost_usd": 0.0 if self.cfg.explorer.free_tier else list_cost,
                         "list_cost_usd": list_cost},
                 "evidence_llm_transcript": "llm_transcript.jsonl"}
        if error:
            rs.fail_infra(f"LLM hatası: {error}")
        # Yürümeyen adımlar (LLM'in hatalı çağrıları) senaryoya girmez; oracle'ın itiraz ettiği adımlar girer
        out = self.explorer.finish(run_id, findings, save_as_scenario, overwrite=True, drop_errored_steps=True,
                                   extra=extra)
        outcome = ExploreOutcome(run_id, out["result"], out["summary"], stop_reason, agent_summary, findings,
                                 steps, turns, usage, out["saved_scenario"])
        if save_as_scenario and out["saved_scenario"] and validate:
            outcome.validation = self._validate(save_as_scenario)
        return outcome

    # ------------------------------------------------------------------ yardımcılar
    def _execute(self, run_id: str, call: ToolCall, tool_names: set[str],
                 findings: list[dict[str, Any]]) -> dict[str, Any]:
        args = dict(call.args or {})
        try:
            if call.name not in tool_names:
                return {"ok": False, "error": f"Bilinmeyen tool: {call.name}"}
            if call.name == "observe":
                return _observe_view(self.explorer.observe(run_id))
            if call.name == "check":
                asserts = yaml.safe_load(args.get("asserts") or "[]")
                if not isinstance(asserts, list):
                    asserts = [asserts]
                return _check_view(self.explorer.check(run_id, asserts))
            if call.name == "qa_setup":
                ops = yaml.safe_load(args.get("ops") or "[]")
                return self.explorer.setup_more(run_id, ops if isinstance(ops, list) else [ops])
            if call.name == "report_finding":
                f = {k: args.get(k) for k in ("title", "description", "severity", "step", "expected", "actual")}
                f["severity"] = f["severity"] or "bug"
                findings.append(f)
                return {"ok": True, "recorded": len(findings)}
            expect_error = args.pop("expect_error", None)
            raw: dict[str, Any] = {call.name: args or None}
            if expect_error:
                raw["expect_error"] = expect_error
            return _step_view(self.explorer.step(run_id, raw))
        except (ValueError, TypeError, KeyError, SetupError, yaml.YAMLError) as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _trim(history: list[Message], keep_turns: int) -> list[Message]:
        # [ilk mesaj] + son keep_turns (assistant, tool) çifti; çiftler bölünmez
        body = history[1:]
        max_len = keep_turns * 2
        if len(body) <= max_len:
            return history
        body = body[len(body) - max_len:]
        while body and body[0].role != "assistant":
            body = body[1:]
        return [history[0]] + body

    def _validate(self, name: str) -> dict[str, Any]:
        """Üretilen senaryoyu aynı seed ile bir kez çalıştır: bulguyu yeniden üretiyor mu / geçiyor mu?"""
        runner = ScenarioRunner(self.cfg, self.store, self.factory)
        sc, text = load_scenario(self.cfg.scenarios_path, name)
        rep = runner.run(sc, text)
        return {"run_id": rep["run_id"], "result": rep["result"], "summary": rep["summary"]}

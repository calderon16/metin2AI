"""Sahip (GM) fısıltıları: anlama, yürütme ve DOĞRU raporlama.

Küçük yerel model serbest metinde hem isteği yanlış anlayabiliyor hem de yapmadığı şeyi "yaptım" diyebiliyor.
Bu yüzden:
  1. Sık istekler (selam, durum, gel, ticaret, ver, kes, dur/devam) kurallarla anlaşılır; model yalnız kalanları
     sabit bir niyet listesinden seçer (serbest cevap yazamaz).
  2. Bilinen niyetler kodla, davranış adımlarıyla yürütülür (gel → go_to_player, ticaret → trade_with ...).
  3. Sahibe giden her rapor adımların GERÇEK sonucundan üretilir; model iş sırasında fısıldayamaz.
  4. Durum soruları (neredesin, seviyen, yangın) oyunun kendi verisiyle cevaplanır.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..engine import npcdir

INTENTS = ("chat", "status", "come", "receive_trade", "give", "hunt", "pause", "resume", "task")


@dataclass
class Intent:
    kind: str
    count: int | None = None
    target: str | None = None          # canavar / eşya adı
    gold: int | None = None
    task: str | None = None            # serbest iş (yalnız kind == "task")
    source: str = "rule"               # rule | llm
    extra: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ anlama (kurallar)
_NUM_WORDS = {"bir": 1, "iki": 2, "uc": 3, "dort": 4, "bes": 5, "alti": 6, "yedi": 7, "sekiz": 8, "dokuz": 9,
              "on": 10, "yirmi": 20, "otuz": 30, "elli": 50, "yuz": 100}


def _num(tok: str | None) -> int | None:
    if not tok:
        return None
    tok = tok.strip()
    if tok.isdigit():
        return int(tok)
    return _NUM_WORDS.get(tok)


def _gold(text: str) -> int | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(k|bin|m|milyon)?\s*yang", text)
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    mult = {"k": 1000, "bin": 1000, "m": 1_000_000, "milyon": 1_000_000}.get(m.group(2) or "", 1)
    return int(n * mult)


def classify_rules(text: str) -> Intent | None:
    t = npcdir.fold(text)
    t = re.sub(r"[!?.,;:]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    words = t.split()
    if not t:
        return Intent("chat")
    if re.search(r"\b(devam et|oynamaya devam|oyna artik|serbestsin)\b", t):
        return Intent("resume")
    if len(words) <= 4 and re.search(r"\b(dur|bekle|birak|oynama|dinlen|mola)\b", t):
        return Intent("pause")
    if re.search(r"\b(yanima|buraya|bana dogru) gel|\bgel (buraya|yanima)\b|^gel\b|\bgel$", t):
        return Intent("come")
    if re.search(r"\bticaret\b.*\b(at|ac|yolla|gonder|baslat|iste)\b|\bsana\b.*\b(verey|verece|gondere|yollay)", t) \
            and not re.search(r"\bbana\b.*\b(ver|gonder|yolla)\b", t):
        return Intent("receive_trade", gold=_gold(t))
    m = re.search(r"\bbana (.+?) (ver|verir misin|gonder|yolla)\b", t)
    if m:
        what = m.group(1)
        gold = _gold(what)
        cm = re.match(r"(\d+|" + "|".join(_NUM_WORDS) + r")\s+(tane |adet )?(.+)", what)
        count, name = (_num(cm.group(1)), cm.group(3)) if cm else (None, what)
        name = re.sub(r"\b(ticaretle|ticaret ile|ile|tane|adet)\b", " ", name)
        name = re.sub(r"(lerini|larini|leri|lari|ini|ıni|unu|nu|ni|i)$", "", name.strip()).strip()
        return Intent("give", gold=gold, count=None if gold else count, target=None if gold else name)
    m = re.search(r"(?:(\d+|" + "|".join(_NUM_WORDS) + r")\s+(?:tane |adet )?)?(.+?)\s+(kes|avla|oldur|kasil|temizle)\b", t)
    if m and not re.search(r"(gorev|quest|oyuncu)", m.group(2)):
        name = re.sub(r"(leri|lari|yi|yu|i|u)$", "", m.group(2).strip())
        return Intent("hunt", count=_num(m.group(1)) or 1, target=name)
    if re.search(r"\b(nerede|nerdesin|nerdesn|seviye|level|kac yang|yangin|ne yapiyor|napiyo|durum|canin|hp\b|"
                 r"envanter|cantanda|gorev var mi|gorevlerin|hangi gorev)", t):
        return Intent("status")
    if len(words) <= 5 and re.search(r"^(selam|slm|sa|as|merhaba|mrb|hey|naber|nasilsin|iyi misin|tesekkur|"
                                     r"tesekkurler|sagol|eyvallah|gunaydin|iyi aksamlar|hosca kal|gorusuruz)\b", t):
        return Intent("chat")
    return None


# ------------------------------------------------------------------ anlama (model — yalnız sabit liste)
CLASSIFY_SYSTEM = """Metin2 oyununda {sender} (GM) karakterine ({account}) fısıltıyla bir istek yazdı. İsteği
aşağıdaki niyetlerden TAM OLARAK BİRİNE ayır. Cevap metni yazma; yalnız JSON ver.

Niyetler:
- chat: selam, sohbet, teşekkür (iş yok)
- status: nerede olduğu, seviyesi, yangı, ne yaptığı soruluyor
- come: {sender}'in yanına gelmesi isteniyor
- receive_trade: {sender} ona bir şey VERECEK (ticaret aç / sana yang vereyim)
- give: o {sender}'e bir şey VERECEK. target = eşya adı, count = adet, gold = yang miktarı
- hunt: canavar kesmesi isteniyor. target = canavar adı, count = adet
- pause: durması/beklemesi isteniyor
- resume: oynamaya devam etmesi isteniyor
- task: yukarıdakilerin dışında oyunda yapılacak bir iş (satıcıdan al, + bas, görev yap, eşya giy ...).
  task = işin kısa ve açık tarifi

Örnekler:
"silah satıcısından kendine silah al" -> {{"intent": "task", "task": "Silah Satıcısından seviyene uygun bir silah satın al ve giy"}}
"demirciye git kılıcını +1 yap" -> {{"intent": "task", "task": "Demirciye git ve kılıcını +1 yükselt"}}
"5 kurt kes" -> {{"intent": "hunt", "target": "Kurt", "count": 5}}
"bana 2 kırmızı iksir ver" -> {{"intent": "give", "target": "Kırmızı İksir", "count": 2}}
"sana 1000 yang vereceğim ticaret aç" -> {{"intent": "receive_trade", "gold": 1000}}

YALNIZCA JSON: {{"intent": "...", "target": null, "count": null, "gold": null, "task": null}}"""


def _json_obj(text: str) -> dict[str, Any] | None:
    t = (text or "").strip()
    if "</think>" in t:
        t = t.rsplit("</think>", 1)[1]
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        obj = json.loads(t[a:b + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def classify_llm(chat: Callable[[str, str], str], text: str, sender: str, account: str) -> Intent:
    """chat(system, user) -> metin. Model listede olmayan bir şey derse iş (task) olarak orijinal metin kullanılır."""
    obj = _json_obj(chat(CLASSIFY_SYSTEM.format(sender=sender, account=account), text)) or {}
    kind = str(obj.get("intent") or "").strip().lower()
    if kind not in INTENTS:
        return Intent("task", task=text, source="llm")
    count = obj.get("count")
    gold = obj.get("gold")
    return Intent(kind, count=int(count) if isinstance(count, (int, float)) and count > 0 else None,
                  target=str(obj["target"]).strip() if obj.get("target") else None,
                  gold=int(gold) if isinstance(gold, (int, float)) and gold > 0 else None,
                  task=str(obj.get("task") or text).strip() if kind == "task" else None, source="llm")


def classify(text: str, chat: Callable[[str, str], str] | None, sender: str, account: str) -> Intent:
    return classify_rules(text) or (classify_llm(chat, text, sender, account) if chat else Intent("task", task=text))


# ------------------------------------------------------------------ doğru raporlama
def status_text(state: dict[str, Any], nearby: list[dict[str, Any]], activity: str, question: str = "",
                inventory: list[dict[str, Any]] | None = None, quests: dict[str, Any] | None = None) -> str:
    """Oyunun kendi verisinden durum cümlesi (model uydurmaz). Soru envanter/görev soruyorsa onları da ekler."""
    q = npcdir.fold(question)
    if re.search(r"(envanter|canta|esya|item|neyin var|ne var)", q):
        items = [f"{i.get('name') or i['vnum']} x{i['count']}" for i in inventory or []]
        return ("Envanterim: " + ", ".join(items[:15]) + ".") if items else "Envanterim boş."
    if re.search(r"(gorev|quest)", q):
        rows = []
        for qid, v in (quests or {}).items():
            st = {"available": "alınabilir", "active": "sürüyor", "ready": "teslim edilecek", "done": "bitti"}.get(
                v.get("state"), v.get("state"))
            if st != "bitti":
                objs = ", ".join(f"{o['label']} {o['cur']}/{o['max']}" for o in v.get("objs") or [])
                rows.append(f"{v.get('title') or qid} ({st}{': ' + objs if objs else ''})")
        return ("Görevlerim: " + "; ".join(rows[:6]) + ".") if rows else "Şu an alınabilir ya da süren görevim yok."
    maps = (npcdir.load() or {}).get("maps") or {}
    mname = (maps.get(str(state.get("map"))) or {}).get("name") or f"harita {state.get('map')}"
    npcs = [e for e in nearby if e.get("type") == "npc" and e.get("name")]
    near = f", {npcs[0]['name']} yakınında" if npcs else ""
    parts = [f"Seviye {state.get('level')}", f"{state.get('gold')} yang", f"HP {state.get('hp')}/{state.get('max_hp')}"]
    if state.get("dead"):
        parts.append("şu an ölüyüm")
    return f"{', '.join(parts)}. {mname} ({state.get('x')}, {state.get('y')}){near}. Şu an: {activity}."


def _item(vnum: Any) -> str:
    return npcdir.item_name(vnum)


def describe_steps(steps: list[dict[str, Any]]) -> tuple[list[str], str | None]:
    """Run adımlarından yalnız GERÇEKTEN olanlar: (yapılanlar, son hata)."""
    done: list[str] = []
    last_error = None
    for s in steps:
        name, args, res = s.get("name"), s.get("args") or {}, s.get("result") or {}
        if s.get("status") != "passed":
            last_error = str(s.get("error") or "")
            continue
        if name == "buy_item":
            done.append(f"{_item(args.get('vnum'))} aldım")
        elif name == "sell_item":
            done.append(f"{_item(args.get('vnum'))} sattım")
        elif name == "equip_item":
            done.append(f"{_item(args.get('vnum') or res.get('vnum') or '')} giydim".strip())
        elif name == "refine_item" and isinstance(res, dict) and res.get("confirmed"):
            done.append(f"{_item(res.get('src_vnum'))} yükseltmesi {'başarılı' if res.get('result') == 'success' else 'başarısız'}")
        elif name == "kill_monster":
            done.append(f"{res.get('kills', 0)} canavar kestim")
        elif name in ("go_to_npc",) and res.get("name"):
            done.append(f"{res['name']} yanına gittim")
        elif name == "go_to_player":
            done.append(f"{args.get('name')} yanına geldim")
        elif name == "use_item":
            done.append(f"{_item(args.get('vnum'))} kullandım" if args.get("vnum") else "eşya kullandım")
        elif name in ("trade_accept", "trade_wait_and_accept") and res.get("completed"):
            done.append("ticaret tamamlandı")
        elif name in ("walk_to", "walk_by", "move_to_entity"):
            done.append("yürüdüm")
        elif name in ("talk_npc", "go_to_quest_npc") and isinstance(res, dict) and res.get("npc"):
            done.append("NPC ile konuştum")
        elif name == "select_dialog" and isinstance(res, dict) and res.get("selected") not in (None, "Kapat"):
            done.append(f"'{res['selected']}' seçtim")
        elif name == "pickup":
            done.append("yerden eşya topladım")
        elif name in ("skill_up", "stat_up"):
            done.append("puan dağıttım")
    # tekrarları sıkıştır
    out: list[str] = []
    for d in done:
        if not out or out[-1] != d:
            out.append(d)
    return out, last_error


def clean_error(err: str | None) -> str:
    """'ACTION_REJECTED: buy_item reddedildi: NOT_ENOUGH_GOLD Yetersiz yang' -> 'Yetersiz yang'"""
    e = str(err or "").strip()
    e = re.sub(r"^[A-Z_]+: ", "", e)
    e = re.sub(r"^\w+ reddedildi: ", "", e)
    e = re.sub(r"^[A-Z_]{3,}\s+", "", e)
    return e[:150]


def task_report(steps: list[dict[str, Any]], stop_reason: str | None) -> str:
    done, last_error = describe_steps(steps)
    if done:
        msg = "Yaptıklarım: " + ", ".join(done[-5:]) + "."
        if last_error:
            msg += f" Sorun: {clean_error(last_error)}."
        return msg
    if last_error:
        return f"Yapamadım: {clean_error(last_error)}."
    if stop_reason == "stuck_loop":
        return "Yapamadım, takıldım; başka türlü tarif eder misin?"
    return "Bir şey yapamadım; biraz daha açık yazar mısın?"

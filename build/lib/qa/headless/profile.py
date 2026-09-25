"""Paket profili: fork'un `packet.h`'ından otomatik çıkarılan paket numaraları ve yapıları.

Her Metin2 fork'unda header numaraları, struct alanları ve boyutları farklıdır. Elle yazmak yerine:

    metin2-qa packets import --packet-h server/common/packet.h \\
        --size-table client/UserInterface/PythonNetworkStream.cpp \\
        --size-table server/game/src/packet_info.cpp \\
        --defines client/UserInterface/Locale_inc.h --define ENABLE_XYZ \\
        -o profiles/metin2re.json

Ayrıştırıcı şunları anlar:
  * `#define AD deger`, `enum { HEADER_X = 1, ... }` (örtük artış, hex, önceki sabitlere referans)
  * `#ifdef/#ifndef/#if defined(X) && !defined(Y)/#elif/#else/#endif` (verilen define kümesiyle)
  * `typedef struct ad { ... } TAd;`, `struct TAd { ... };`, `typedef BYTE TAd;`
  * alanlar: BYTE/WORD/DWORD/char/short/int/long/float/int64/bool, `char ad[N]`, çok boyutlu diziler,
    iç içe struct'lar; paketler `#pragma pack(1)` kabul edilir.
  * boyut tabloları: client `Set(HEADER_GC_X, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCX),
    DYNAMIC_SIZE_PACKET))` ve sunucu `Set(HEADER_CG_X, sizeof(TPacketCGX), ...)` satırları → header → struct.
"""

from __future__ import annotations

import json
import re
import struct as _struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PRIMITIVES: dict[str, tuple[str, int]] = {
    # ad: (struct formatı, boyut)
    "BYTE": ("B", 1), "UINT8": ("B", 1), "uint8_t": ("B", 1), "unsigned char": ("B", 1), "bool": ("B", 1),
    "char": ("b", 1), "int8_t": ("b", 1), "signed char": ("b", 1), "BOOL8": ("B", 1),
    "WORD": ("H", 2), "uint16_t": ("H", 2), "unsigned short": ("H", 2), "USHORT": ("H", 2),
    "short": ("h", 2), "int16_t": ("h", 2), "SHORT": ("h", 2),
    "DWORD": ("I", 4), "uint32_t": ("I", 4), "unsigned int": ("I", 4), "UINT": ("I", 4), "unsigned long": ("I", 4),
    "ULONG": ("I", 4), "BOOL": ("i", 4),
    "int": ("i", 4), "int32_t": ("i", 4), "long": ("i", 4), "INT": ("i", 4), "LONG": ("i", 4),
    "float": ("f", 4), "FLOAT": ("f", 4), "double": ("d", 8),
    "int64_t": ("q", 8), "long long": ("q", 8), "LONGLONG": ("q", 8), "INT64": ("q", 8), "__int64": ("q", 8),
    "uint64_t": ("Q", 8), "unsigned long long": ("Q", 8), "ULONGLONG": ("Q", 8), "DWORD64": ("Q", 8),
    "time_t": ("i", 4),
}

TEXT_ENCODING = "cp1254"  # Türkçe istemciler; profil ile değiştirilebilir


class ProfileError(Exception):
    pass


# ---------------------------------------------------------------------- ön işlemci

def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _eval_cond(expr: str, defines: dict[str, str]) -> bool:
    e = expr.strip()
    e = re.sub(r"defined\s*\(\s*(\w+)\s*\)", lambda m: " 1 " if m.group(1) in defines else " 0 ", e)
    e = re.sub(r"defined\s+(\w+)", lambda m: " 1 " if m.group(1) in defines else " 0 ", e)

    def ident(m: re.Match[str]) -> str:
        v = defines.get(m.group(0))
        if v is None:
            return "0"
        v = v.strip() or "1"
        return v if re.fullmatch(r"-?\d+", v) else "1"

    e = re.sub(r"\b[A-Za-z_]\w*\b", ident, e)
    e = e.replace("&&", " and ").replace("||", " or ")
    e = re.sub(r"!(?!=)", " not ", e)
    try:
        return bool(eval(e, {"__builtins__": {}}, {}))  # yalnızca sayılar ve mantık operatörleri kaldı
    except Exception:
        return False


def preprocess(text: str, defines: dict[str, str]) -> str:
    """Koşullu derleme bloklarını değerlendirir, #define'ları `defines`'a ekler."""
    out: list[str] = []
    stack: list[tuple[bool, bool]] = []  # (bu blok aktif mi, bu zincirde bir dal alındı mı)
    active = True
    lines = _strip_comments(text).replace("\\\n", " ").split("\n")
    for line in lines:
        s = line.strip()
        m = re.match(r"#\s*(\w+)\s*(.*)", s)
        if m:
            d, rest = m.group(1), m.group(2).strip()
            if d in ("ifdef", "ifndef", "if"):
                if d == "ifdef":
                    cond = rest.split()[0] in defines if rest else False
                elif d == "ifndef":
                    cond = rest.split()[0] not in defines if rest else True
                else:
                    cond = _eval_cond(rest, defines)
                stack.append((active, active and cond))
                active = active and cond
                continue
            if d == "elif":
                parent, taken = stack[-1]
                cond = parent and not taken and _eval_cond(rest, defines)
                stack[-1] = (parent, taken or cond)
                active = cond
                continue
            if d == "else":
                parent, taken = stack[-1]
                active = parent and not taken
                stack[-1] = (parent, True)
                continue
            if d == "endif":
                if stack:
                    active = stack.pop()[0]
                continue
            if not active:
                continue
            if d == "define":
                dm = re.match(r"(\w+)(?:\s+(.*))?$", rest)
                if dm and "(" not in dm.group(1):
                    defines[dm.group(1)] = (dm.group(2) or "").strip()
            elif d == "undef":
                defines.pop(rest.split()[0] if rest else "", None)
            continue
        if active:
            out.append(line)
    return "\n".join(out)


def read_defines(paths: list[Path], extra: list[str] | None = None) -> dict[str, str]:
    """Define dosyalarından (Locale_inc.h, CommonDefines.h, service.h) etkin define'ları topla."""
    defines: dict[str, str] = {}
    for d in extra or []:
        k, _, v = d.partition("=")
        defines[k.strip()] = v.strip()
    for p in paths:
        preprocess(p.read_text(encoding="utf-8", errors="replace"), defines)
    return defines


# ---------------------------------------------------------------------- struct modeli

@dataclass
class Field:
    name: str
    type: str                 # primitif adı veya struct adı
    dims: list[int] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "dims": self.dims}


@dataclass
class Struct:
    name: str
    fields: list[Field]
    size: int = 0


class Profile:
    def __init__(self, constants: dict[str, int], structs: dict[str, Struct], typedefs: dict[str, str],
                 packets: dict[str, dict[str, Any]], encoding: str = TEXT_ENCODING, name: str = "profile"):
        self.name = name
        self.constants = constants
        self.structs = structs
        self.typedefs = typedefs
        self.packets = packets          # HEADER_X -> {"struct": ..., "dynamic": bool, "header": int}
        self.encoding = encoding
        for s in self.structs.values():
            s.size = self.sizeof(s.name)
        self._by_value: dict[str, dict[int, str]] = {"GC": {}, "CG": {}}
        for h, info in self.packets.items():
            for d in ("GC", "CG"):
                if f"_{d}_" in h:
                    self._by_value[d][info["header"]] = h

    # -- tipler
    def resolve(self, t: str) -> str:
        seen = set()
        while t in self.typedefs and t not in seen:
            seen.add(t)
            t = self.typedefs[t]
        return t

    def sizeof(self, t: str) -> int:
        t = self.resolve(t)
        if t in PRIMITIVES:
            return PRIMITIVES[t][1]
        if t in self.structs:
            s = self.structs[t]
            total = 0
            for f in s.fields:
                n = 1
                for d in f.dims:
                    n *= d
                total += self.sizeof(f.type) * n
            return total
        raise ProfileError(f"Bilinmeyen tip: {t}")

    def header(self, name: str) -> int:
        if name in self.packets:
            return self.packets[name]["header"]
        if name in self.constants:
            return self.constants[name]
        raise ProfileError(f"Profilde header yok: {name}")

    def has(self, name: str) -> bool:
        return name in self.packets or name in self.constants

    def packet_by_value(self, direction: str, value: int) -> str | None:
        return self._by_value[direction].get(value)

    def struct_for(self, header_name: str) -> str:
        info = self.packets.get(header_name)
        if not info or not info.get("struct"):
            raise ProfileError(f"{header_name} için struct bilinmiyor (boyut tablosunu profile ekleyin)")
        return info["struct"]

    # -- kodlama
    def encode(self, type_name: str, values: dict[str, Any] | None = None) -> bytes:
        return self._enc(self.resolve(type_name), values or {})

    def _enc(self, t: str, v: Any) -> bytes:
        t = self.resolve(t)
        if t in PRIMITIVES:
            fmt, _ = PRIMITIVES[t]
            if v is None:
                v = 0
            if fmt in "fd":
                return _struct.pack("<" + fmt, float(v))
            return _struct.pack("<" + fmt, int(v))
        s = self.structs[t]
        out = b""
        for f in s.fields:
            out += self._enc_field(f, (v or {}).get(f.name))
        return out

    def _enc_field(self, f: Field, v: Any) -> bytes:
        t = self.resolve(f.type)
        if not f.dims:
            return self._enc(t, v)
        if t in ("char", "BYTE", "unsigned char") and len(f.dims) == 1 and (isinstance(v, (str, bytes)) or v is None):
            raw = v.encode(self.encoding, errors="replace") if isinstance(v, str) else (v or b"")
            return raw[:f.dims[0]].ljust(f.dims[0], b"\0")
        n0, rest = f.dims[0], f.dims[1:]
        items = list(v or [])
        out = b""
        for i in range(n0):
            item = items[i] if i < len(items) else None
            out += self._enc_field(Field(f.name, f.type, rest), item) if rest else self._enc(t, item)
        return out

    def decode(self, type_name: str, data: bytes, offset: int = 0) -> tuple[dict[str, Any], int]:
        v, off = self._dec(self.resolve(type_name), data, offset)
        return v, off

    def _dec(self, t: str, data: bytes, off: int) -> tuple[Any, int]:
        t = self.resolve(t)
        if t in PRIMITIVES:
            fmt, size = PRIMITIVES[t]
            if off + size > len(data):
                raise ProfileError(f"{t} için veri yetersiz")
            return _struct.unpack_from("<" + fmt, data, off)[0], off + size
        s = self.structs[t]
        out: dict[str, Any] = {}
        for f in s.fields:
            out[f.name], off = self._dec_field(f, data, off)
        return out, off

    def _dec_field(self, f: Field, data: bytes, off: int) -> tuple[Any, int]:
        t = self.resolve(f.type)
        if not f.dims:
            return self._dec(t, data, off)
        if t == "char" and len(f.dims) == 1:
            raw = data[off:off + f.dims[0]]
            return raw.split(b"\0", 1)[0].decode(self.encoding, errors="replace"), off + f.dims[0]
        n0, rest = f.dims[0], f.dims[1:]
        items = []
        for _ in range(n0):
            if rest:
                item, off = self._dec_field(Field(f.name, f.type, rest), data, off)
            else:
                item, off = self._dec(t, data, off)
            items.append(item)
        return items, off

    # -- kalıcılık
    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name, "encoding": self.encoding, "constants": self.constants, "typedefs": self.typedefs,
            "structs": {k: {"size": s.size, "fields": [f.to_json() for f in s.fields]} for k, s in self.structs.items()},
            "packets": self.packets,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Profile":
        structs = {k: Struct(k, [Field(f["name"], f["type"], list(f.get("dims", []))) for f in v["fields"]])
                   for k, v in data["structs"].items()}
        return cls(data["constants"], structs, data.get("typedefs", {}), data["packets"],
                   data.get("encoding", TEXT_ENCODING), data.get("name", "profile"))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Profile":
        if not path.exists():
            raise ProfileError(f"Paket profili yok: {path} (metin2-qa packets import ile üretin)")
        return cls.from_json(json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------- ayrıştırıcı

_TYPE_WORDS = r"(?:unsigned\s+long\s+long|unsigned\s+char|unsigned\s+short|unsigned\s+int|unsigned\s+long|" \
              r"signed\s+char|long\s+long|const\s+\w+|\w+)"


def _const_value(expr: str, consts: dict[str, int]) -> int | None:
    e = expr.strip().rstrip(",").strip()
    e = re.sub(r"\(\s*(?:BYTE|WORD|DWORD|int|unsigned\s+\w+)\s*\)", "", e)  # (BYTE) gibi dönüşümleri at
    if not e:
        return None
    e = re.sub(r"\b(0x[0-9a-fA-F]+|\d+)[uUlL]+\b", r"\1", e)

    def sub(m: re.Match[str]) -> str:
        w = m.group(0)
        if w in consts:
            return str(consts[w])
        raise KeyError(w)

    try:
        e2 = re.sub(r"\b[A-Za-z_]\w*\b", sub, e)
        if not re.fullmatch(r"[\s0-9xXa-fA-F+\-*/()<>|&~]+", e2):
            return None
        return int(eval(e2, {"__builtins__": {}}, {}))
    except (KeyError, SyntaxError, ZeroDivisionError, ValueError, TypeError):
        return None


def _parse_fields(body: str, consts: dict[str, int]) -> list[Field]:
    fields: list[Field] = []
    for decl in body.split(";"):
        decl = " ".join(decl.split())
        if not decl or decl.startswith(("typedef", "enum", "union", "#")) or "(" in decl:
            continue
        m = re.match(rf"^(?:struct\s+)?({_TYPE_WORDS})\s+(.+)$", decl)
        if not m:
            continue
        typ = re.sub(r"^const\s+", "", m.group(1)).strip()
        typ = " ".join(typ.split())
        for part in m.group(2).split(","):
            part = part.strip()
            if not part or "*" in part:
                continue
            nm = re.match(r"^(\w+)((?:\s*\[[^\]]+\])*)$", part)
            if not nm:
                continue
            dims = []
            for d in re.findall(r"\[([^\]]+)\]", nm.group(2)):
                v = _const_value(d, consts)
                if v is None:
                    raise ProfileError(f"Dizi boyutu çözülemedi: {d} ({nm.group(1)})")
                dims.append(v)
            fields.append(Field(nm.group(1), typ, dims))
    return fields


def parse_headers(texts: list[str], defines: dict[str, str]) -> tuple[dict[str, int], dict[str, Struct], dict[str, str]]:
    consts: dict[str, int] = {}
    structs: dict[str, Struct] = {}
    typedefs: dict[str, str] = {}
    for raw in texts:
        # preprocess #define'ları local'e ekler; sayısal olanlar sabit olur
        local = dict(defines)
        text = preprocess(raw, local)
        for k, v in local.items():
            val = _const_value(v, consts) if v else None
            if val is not None:
                consts[k] = val
        # enum'lar
        for em in re.finditer(r"enum\s*\w*\s*(?::\s*\w+\s*)?\{(.*?)\}", text, re.S):
            cur = -1
            for item in em.group(1).split(","):
                item = item.strip()
                if not item:
                    continue
                if "=" in item:
                    k, v = item.split("=", 1)
                    val = _const_value(v, consts)
                    if val is None:
                        continue
                    cur = val
                    consts[k.strip()] = cur
                else:
                    cur += 1
                    if re.fullmatch(r"\w+", item):
                        consts[item] = cur
        # typedef <tip> <ad>;
        for tm in re.finditer(rf"typedef\s+({_TYPE_WORDS})\s+(\w+)\s*;", text):
            typedefs[tm.group(2)] = " ".join(tm.group(1).split())
        # struct'lar (iç içe struct gövdesi yoktur varsayımı; Metin2 paketlerinde geçerli)
        pat = re.compile(r"(typedef\s+)?struct\s+(\w+)?\s*\{([^{}]*)\}\s*(\w+)?\s*;", re.S)
        for sm in pat.finditer(text):
            tag, body, alias = sm.group(2), sm.group(3), sm.group(4)
            names = [n for n in (alias, tag) if n]
            if not names:
                continue
            flds = _parse_fields(body, consts)
            for n in names:
                structs[n] = Struct(n, flds)
            if alias and tag:
                typedefs.setdefault(tag, alias)
    return consts, structs, typedefs


_SET_RX = re.compile(
    r"Set\s*\(\s*(HEADER_\w+)\s*,\s*(?:\w+::)?(?:TPacketType\s*\()?\s*sizeof\s*\(\s*(\w+)\s*\)\s*"
    r"(?:,\s*(STATIC_SIZE_PACKET|DYNAMIC_SIZE_PACKET|true|false|TRUE|FALSE|\"[^\"]*\"))?")


def parse_size_tables(texts: list[str], defines: dict[str, str]) -> dict[str, dict[str, Any]]:
    """header adı → {struct, dynamic}. Client (TPacketType) ve sunucu (packet_info) biçimlerini tanır."""
    out: dict[str, dict[str, Any]] = {}
    for raw in texts:
        text = preprocess(raw, dict(defines))
        for m in _SET_RX.finditer(text):
            header, struct_name, flag = m.group(1), m.group(2), m.group(3)
            dynamic = flag == "DYNAMIC_SIZE_PACKET"
            prev = out.get(header)
            out[header] = {"struct": struct_name, "dynamic": dynamic or bool(prev and prev.get("dynamic"))}
    return out


def build_profile(header_texts: list[str], size_table_texts: list[str], defines: dict[str, str],
                  name: str = "profile", encoding: str = TEXT_ENCODING) -> Profile:
    consts, structs, typedefs = parse_headers(header_texts, defines)
    table = parse_size_tables(size_table_texts, defines)
    packets: dict[str, dict[str, Any]] = {}
    for h, v in consts.items():
        if not h.startswith("HEADER_"):
            continue
        info = table.get(h, {})
        st = info.get("struct")
        if st is None:
            st = _guess_struct(h, structs)
        packets[h] = {"header": v, "struct": st, "dynamic": bool(info.get("dynamic"))}
    prof = Profile(consts, structs, typedefs, packets, encoding, name)
    for h, info in packets.items():
        if info["struct"] and info["struct"] not in structs and prof.resolve(info["struct"]) not in structs:
            info["struct"] = None
        elif info["struct"] and not info["dynamic"]:
            # Sunucu tablosu (packet_info.cpp) dinamikliği belirtmez; Metin2'de dinamik paketlerin
            # header'dan sonraki alanı WORD size'dır (toplam boyut)
            st = structs[prof.resolve(info["struct"])]
            if len(st.fields) > 1 and st.fields[1].name.lower() in ("size", "wsize", "length") \
                    and prof.resolve(st.fields[1].type) in ("WORD", "uint16_t", "unsigned short", "USHORT"):
                info["dynamic"] = True
    return prof


def _guess_struct(header: str, structs: dict[str, Struct]) -> str | None:
    """Boyut tablosunda yoksa isimden tahmin: HEADER_GC_ITEM_SET → TPacketGCItemSet."""
    m = re.match(r"HEADER_(GC|CG|GG)_(\w+)", header)
    if not m:
        return None
    camel = "".join(p.capitalize() for p in m.group(2).lower().split("_"))
    for cand in (f"TPacket{m.group(1)}{camel}", f"TPacket{camel}"):
        if cand in structs:
            return cand
    return None


def import_profile(packet_headers: list[Path], size_tables: list[Path], define_files: list[Path],
                   defines: list[str], name: str) -> Profile:
    d = read_defines(define_files, defines)
    rd = lambda p: p.read_text(encoding="utf-8", errors="replace")  # noqa: E731
    return build_profile([rd(p) for p in packet_headers], [rd(p) for p in size_tables], d, name)

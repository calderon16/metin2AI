"""Seed'li deterministik rastgelelik.

Gerçek oyuncuya benzemek için gecikme, rota ve seçimlerde varyasyon kullanılır; ama her şey
tek bir seed'den türetilir. Bir hata bulunduğunda aynı seed ile birebir aynı davranış
tekrar oynatılabilir.
"""

from __future__ import annotations

import hashlib
import random
import secrets
from typing import Sequence, TypeVar

T = TypeVar("T")


def new_seed() -> int:
    return secrets.randbelow(1_000_000)


class QaRandom:
    def __init__(self, seed: int):
        self.seed = int(seed)
        self._r = random.Random(self.seed)

    def delay(self, lo: int, hi: int) -> int:
        return self._r.randint(lo, hi)

    def movement_delay(self) -> int:
        return self.delay(80, 300)

    def reaction_delay(self) -> int:
        return self.delay(150, 600)

    def jitter(self, value: float, amount: float) -> float:
        return value + self._r.uniform(-amount, amount)

    def choice(self, seq: Sequence[T]) -> T:
        return self._r.choice(seq)

    def shuffled(self, seq: Sequence[T]) -> list[T]:
        out = list(seq)
        self._r.shuffle(out)
        return out

    def fork(self, name: str) -> "QaRandom":
        """Aynı seed'den bağımsız ama deterministik bir alt akış (ör. her ajan için)."""
        h = hashlib.sha256(f"{self.seed}:{name}".encode()).digest()
        return QaRandom(int.from_bytes(h[:4], "big"))

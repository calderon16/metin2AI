# Crypto++ tables.txt -> qa/headless/_cipher_tables.py (bkz. README.md)
import sys, textwrap
src, dst = sys.argv[1], sys.argv[2]
rows = dict(line.split() for line in open(src) if line.strip())
def words(h): return [int(h[i:i+8], 16) for i in range(0, len(h), 8)]
out = ['"""Blok şifre sabit tabloları (algoritma tanımlarındaki sabitler).',
       '',
       'Sunucunun kullandığı Crypto++ kitaplığından birebir alındı (tests/data/cryptopp_*.txt üreticisi:',
       'tools/cryptopp_vectors/README.md). Elle düzenlemeyin."""',
       '',
       'from __future__ import annotations',
       '',
       '',
       'def _w(h: str) -> tuple[int, ...]:',
       '    return tuple(int(h[i:i + 8], 16) for i in range(0, len(h), 8))',
       '',
       '']
def emit(name, hexstr, conv):
    body = textwrap.wrap(hexstr, 96)
    out.append(f'{name} = {conv}(')
    for b in body:
        out.append(f'    "{b}"')
    out.append(')')
    out.append('')
emit('MARS_SBOX', rows['mars_sbox'], '_w')
for k in range(1, 5):
    emit(f'CAST_S{k}', rows[f'cast_s{k}'], '_w')
for k in range(2):
    emit(f'TWOFISH_Q{k}', rows[f'twofish_q{k}'], 'bytes.fromhex')
for k in range(4):
    emit(f'TWOFISH_MDS{k}', rows[f'twofish_mds{k}'], '_w')
open(dst, 'w', encoding='utf-8', newline='\n').write('\n'.join(out))

#!/bin/sh
# Builds a throw-away vector generator from a COPY of the server cipher; touches nothing else.
set -e
W=/tmp/qavec
SRC=/usr/metin2/src/server/game/src
cd $W
mkdir -p stub/cryptopp
printf '#include <cassert>\n#include <cstring>\n#include <algorithm>\n#include <memory>\n' > stub/stdafx.h
[ -f /usr/metin2/src/extern/include/cryptopp/cryptoppLibLink.h ] || : > stub/cryptopp/cryptoppLibLink.h
cp $SRC/cipher.h cipher.h
# expose internals + deterministic RNG (seeded per construction)
sed -e 's/^ private:/ public:/' -e 's/^private:/public:/' cipher.h > cipher.h.tmp && mv cipher.h.tmp cipher.h
sed -e 's/^ private:/ public:/' -e 's/^ protected:/ public:/' \
    -e 's/AutoSeededRandomPool rnd;/static word32 g_seed = 12345; LC_RNG rnd(g_seed += 7919);/' \
    $SRC/cipher.cpp > vec.body
{ printf '#include <string>\n#include <vector>\n#include <memory>\n#include <algorithm>\n#include <iostream>\n#include <sstream>\n#include <map>\n#include <deque>\n#include <cstring>\n#include <cassert>\n#include <climits>\n#include <exception>\n#include <typeinfo>\n#include <new>\n#define private public\n#define protected public\n#include <cryptopp/rng.h>\n'; cat vec.body; } > vec.cpp
cat harness.inc >> vec.cpp
c++ -std=c++11 -O1 -D_IMPROVED_PACKET_ENCRYPTION_ -I. -Istub -I/usr/metin2/src/extern/include -o vec vec.cpp \
    /usr/metin2/src/extern/lib/libcryptopp.a -lpthread
./vec
wc -l tables.txt algorithms.txt handshakes.txt

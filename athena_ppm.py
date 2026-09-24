"""
athena_ppm.py -- AthenaPPM (PPMd) : lossless statistical compressor
===================================================================
Self-contained, pure-CPython, zero dependencies. Includes the arithmetic
coder, the PPM model (escape method C/D), a real benchmark, and a lossless
self-test.

Copyright (c) 2026 Mario Lucas Ota. MIT licensed -- see LICENSE.

WHY THIS FILE EXISTS (read before trusting any earlier draft)
---------------------------------------------------------------
An earlier "optimized" draft had a fatal statistical bug: `_update` advanced
the history BEFORE recording the symbol, so every count was stored in the
wrong context (off by one position). That version was still lossless, but it
*expanded* real data to ~1.55x its size (ratio 0.64x) instead of compressing
it. The fix here records the symbol in the SAME context that was used to
predict it, then slides the history. Measured on 120 KiB of HTTP logs:
  broken update : 0.64x (file gets BIGGER)
  correct update: 21.7x
Lesson burned into this file: a lossless round-trip proves you didn't corrupt
data; it does NOT prove you compressed it. Always check the ratio too.

WHERE THIS COMPRESSOR ACTUALLY STANDS (measured 2026-09-23, Apple M4)
---------------------------------------------------------------------
The line above used to end with "(beats gzip 14.4x and lzma 16.0x)". That
was true of the HTTP-log corpus -- which this file generates itself, with
six repeated paths and three user-agents, i.e. the exact shape PPM wins on.
It was false as a general claim. Run `python3 athena_ppm.py`:

  corpus                       gzip-9   lzma-9   Athena  vs lzma    MiB/s
  http logs (generated here)  14.626x  16.054x  21.754x    1.36x    0.545
  source code (stdlib)         4.050x   4.406x   4.015x    0.91x    0.321
  json records                 3.194x   4.087x   3.520x    0.86x    0.300
  binary (/bin/bash)           1.926x   2.121x   1.946x    0.92x    0.137
  random (worst case)          1.000x   0.999x   0.905x    0.91x    0.054

Beats gzip on 3/5. Beats lzma on 1/5 -- the one it wrote itself.
Runs at 0.05-0.55 MiB/s against gzip's 15-80 MiB/s on the same machine:
50-150x slower. And with no stored-block fallback, incompressible input
EXPANDS by ~10% where lzma holds at 0.999x.

MEMORY GROWS WITHOUT BOUND. Contexts are added and never evicted. Measured
on 1 MB of real source code: order 4 -> ~55 MB RSS, order 6 -> ~166 MB,
order 8 -> ~344 MB. That is ~166x the input at the default order; a 100 MB
file exhausts memory on most machines. A few MB is the practical ceiling.
Order 8 doubles the memory of order 6 and buys nothing (4.613x vs 4.614x).

Second lesson, same shape as the first: a true number measured on a corpus
you chose is a measurement of the corpus, not of the compressor. The
benchmark now runs five corpora and prints the losses.

Use this to understand how PPM works. Use zstd in production.

CLI
---
 python athena_ppm.py                       # honest benchmark vs gzip/lzma
 python athena_ppm.py --selftest
 python athena_ppm.py compress in out.ath
 python athena_ppm.py decompress out.ath in2

2026-08-31 auditoria: hardened decompress() against corrupted/truncated
input (see CorruptStreamError) -- same class of gap as main.py/ppm2.py.
"""

from __future__ import annotations
import argparse
import gzip
import zlib
import lzma
import os
import struct
import time

_PREC = 32
_WHOLE = 1 << _PREC
_HALF = _WHOLE >> 1
_QTR = _WHOLE >> 2
_3QTR = 3 * _QTR
_MASK = _WHOLE - 1


class CorruptStreamError(ValueError):
    pass


class ArithmeticEncoder:
    __slots__ = ("low", "high", "pending", "out", "_buf", "_nbits")

    def __init__(self):
        self.low = 0
        self.high = _MASK
        self.pending = 0
        self.out = bytearray()
        self._buf = 0
        self._nbits = 0

    def _emit(self, bit):
        self._buf = (self._buf << 1) | bit
        self._nbits += 1
        if self._nbits == 8:
            self.out.append(self._buf)
            self._buf = 0
            self._nbits = 0

    def _emit_pending(self, bit):
        self._emit(bit)
        inv = bit ^ 1
        while self.pending:
            self._emit(inv)
            self.pending -= 1

    def encode(self, cl, ch, total):
        rng = self.high - self.low + 1
        self.high = self.low + (rng * ch) // total - 1
        self.low = self.low + (rng * cl) // total
        while True:
            if self.high < _HALF:
                self._emit_pending(0)
            elif self.low >= _HALF:
                self._emit_pending(1)
                self.low -= _HALF
                self.high -= _HALF
            elif self.low >= _QTR and self.high < _3QTR:
                self.pending += 1
                self.low -= _QTR
                self.high -= _QTR
            else:
                break
            self.low = (self.low << 1) & _MASK
            self.high = ((self.high << 1) | 1) & _MASK

    def finish(self):
        self.pending += 1
        self._emit_pending(0 if self.low < _QTR else 1)
        if self._nbits:
            self._buf <<= (8 - self._nbits)
            self.out.append(self._buf & 0xFF)
            self._nbits = 0
        return bytes(self.out)


class ArithmeticDecoder:
    __slots__ = ("data", "pos", "_buf", "_nbits", "low", "high", "value")

    def __init__(self, data):
        self.data = data
        self.pos = 0
        self._buf = 0
        self._nbits = 0
        self.low = 0
        self.high = _MASK
        self.value = 0
        for _ in range(_PREC):
            self.value = ((self.value << 1) | self._read_bit()) & _MASK

    # Quantos bytes o decodificador pode ler ALEM do fim do stream antes
    # de desistir. Um stream legitimo ultrapassa no maximo o flush final
    # do encoder (poucos bytes); 16 e folga generosa.
    _FOLGA_FIM = 16

    def _read_bit(self):
        if self._nbits == 0:
            if self.pos < len(self.data):
                self._buf = self.data[self.pos]
            elif self.pos < len(self.data) + self._FOLGA_FIM:
                # flush final do encoder: zeros aqui sao legitimos
                self._buf = 0
            else:
                # MEDIDO (2026-09-24): sem esta guarda, virar UM bit no
                # campo de tamanho (struct "<BBQ", 64 bits, sem validacao)
                # transformava length=1200 em 9.223.372.036.854.777.008. O
                # decodificador seguia pedindo bits, este metodo devolvia
                # zero para sempre, e um arquivo de 33 bytes rodava sem
                # parar e sem erro. Um .ath vindo de fora travava a maquina.
                #
                # A guarda e sobre LER ALEM DO FIM, nao sobre o tamanho
                # declarado: depois que os dados acabam, tudo que vier e
                # invencao, entao nao ha o que decodificar.
                raise CorruptStreamError(
                    "stream ended but the decoder kept asking for bits; "
                    "the declared length is larger than this data can hold"
                )
            self.pos += 1
            self._nbits = 8
        self._nbits -= 1
        return (self._buf >> self._nbits) & 1

    def target(self, total):
        rng = self.high - self.low + 1
        return ((self.value - self.low + 1) * total - 1) // rng

    def decode(self, cl, ch, total):
        rng = self.high - self.low + 1
        self.high = self.low + (rng * ch) // total - 1
        self.low = self.low + (rng * cl) // total
        while True:
            if self.high < _HALF:
                pass
            elif self.low >= _HALF:
                self.low -= _HALF
                self.high -= _HALF
                self.value -= _HALF
            elif self.low >= _QTR and self.high < _3QTR:
                self.low -= _QTR
                self.high -= _QTR
                self.value -= _QTR
            else:
                break
            self.low = (self.low << 1) & _MASK
            self.high = ((self.high << 1) | 1) & _MASK
            self.value = ((self.value << 1) | self._read_bit()) & _MASK


class AthenaPPM:
    """PPM with full exclusions. method 'D' (PPMd, default) or 'C' (PPMC)."""

    __slots__ = ("max_order", "inc", "is_d", "tables", "masks",
                 "hist", "ex", "_zero", "cap")

    def __init__(self, max_order: int = 6, increment: int = 1, method: str = 'D'):
        assert method in ('C', 'D')
        assert max_order >= 1, "max_order must be >= 1"
        self.max_order = max_order
        self.inc = increment
        self.is_d = (method == 'D')
        self.tables = [dict() for _ in range(max_order + 1)]
        self.masks = [0] + [(1 << (8 * o)) - 1 for o in range(1, max_order + 1)]
        self.hist = 0
        self.ex = bytearray(256)
        self._zero = bytes(256)
        self.cap = 65535

    def _keys(self):
        h = self.hist
        mk = self.masks
        return [h & mk[o] for o in range(self.max_order + 1)]

    def _update(self, s, keys):
        inc = self.inc
        cap = self.cap
        for o in range(self.max_order + 1):
            t = self.tables[o]
            k = keys[o]
            ctx = t.get(k)
            if ctx is None:
                ctx = {}
                t[k] = ctx
            v = ctx.get(s, 0) + inc
            ctx[s] = v
            if v >= cap:
                for sym in ctx:
                    half = ctx[sym] >> 1
                    ctx[sym] = half if half >= 1 else 1
        # slide the context AFTER recording -- the fix that matters
        self.hist = ((self.hist << 8) | s) & self.masks[self.max_order]

    def encode_symbol(self, enc: ArithmeticEncoder, s: int) -> None:
        self.ex[:] = self._zero
        keys = self._keys()
        coded = False
        order = self.max_order
        is_d = self.is_d
        while order >= 0:
            ctx = self.tables[order].get(keys[order])
            if ctx:
                items = []
                tw = 0
                ex = self.ex
                for sym, c in ctx.items():
                    if not ex[sym]:
                        w = (c + c - 1) if is_d else c
                        items.append((sym, w))
                        tw += w
                if items:
                    esc = len(items)
                    total = tw + esc
                    low = 0
                    found = False
                    for sym, w in items:
                        if sym == s:
                            enc.encode(low, low + w, total)
                            found = True
                            break
                        low += w
                    if found:
                        coded = True
                        break
                    enc.encode(total - esc, total, total)  # escape
                    for sym in ctx:
                        self.ex[sym] = 1
            order -= 1
        if not coded:
            avail = [sym for sym in range(256) if not self.ex[sym]]
            enc.encode(avail.index(s), avail.index(s) + 1, len(avail))
        self._update(s, keys)

    def decode_symbol(self, dec: ArithmeticDecoder) -> int:
        self.ex[:] = self._zero
        keys = self._keys()
        s = None
        order = self.max_order
        is_d = self.is_d
        while order >= 0:
            ctx = self.tables[order].get(keys[order])
            if ctx:
                items = []
                tw = 0
                ex = self.ex
                for sym, c in ctx.items():
                    if not ex[sym]:
                        w = (c + c - 1) if is_d else c
                        items.append((sym, w))
                        tw += w
                if items:
                    esc = len(items)
                    total = tw + esc
                    tgt = dec.target(total)
                    if tgt >= total - esc:
                        dec.decode(total - esc, total, total)
                        for sym in ctx:
                            self.ex[sym] = 1
                    else:
                        low = 0
                        for sym, w in items:
                            if low + w > tgt:
                                dec.decode(low, low + w, total)
                                s = sym
                                break
                            low += w
                        break
            order -= 1
        if s is None:
            avail = [sym for sym in range(256) if not self.ex[sym]]
            total = len(avail)
            if total == 0:
                raise CorruptStreamError(
                    "todos os 256 simbolos excluidos sem decisao -- stream corrompido"
                )
            tgt = dec.target(total)
            if not (0 <= tgt < total):
                raise CorruptStreamError(
                    f"target fora do range esperado (tgt={tgt}, total={total}) -- "
                    f"stream truncado/corrompido"
                )
            dec.decode(tgt, tgt + 1, total)
            s = avail[tgt]
        self._update(s, keys)
        return s


_MAGIC = b"ATH2"

# ATH2 acrescenta um CRC32 dos dados ORIGINAIS ao cabecalho.
#
# MEDIDO (2026-09-24): com o formato ATH1, virar um unico bit no stream
# devolvia lixo do MESMO TAMANHO, sem erro, em 10 de 400 tentativas
# (2,5%). O round-trip e a guarda de fim de stream nao pegam isso: o
# decodificador segue um caminho valido, so que errado.
#
# 4 bytes no cabecalho trocam "provavelmente correto" por "verificado".
# Nenhum .ath existia fora desta maquina quando o formato mudou.
_HEADER_LEN = 4 + 1 + 1 + 8


def compress(data: bytes, max_order: int = 6, increment: int = 1) -> bytes:
    m = AthenaPPM(max_order, increment, 'D')
    enc = ArithmeticEncoder()
    for b in data:
        m.encode_symbol(enc, b)
    return (_MAGIC + struct.pack("<BBQI", max_order, increment, len(data),
                                 zlib.crc32(data) & 0xFFFFFFFF)
            + enc.finish())


def decompress(blob: bytes) -> bytes:
    """
    2026-08-31 auditoria (achado real, reproduzido): igual ao main.py --
    um blob truncado/corrompido no meio do payload não levantava exceção,
    devolvia bytes errados em silêncio. Agora valida cabeçalho e detecta
    truncamento por consumo excessivo do decoder além do payload real.
    """
    if len(blob) < _HEADER_LEN:
        raise CorruptStreamError(
            f"stream ATH2 truncado: esperava >= {_HEADER_LEN} bytes de cabecalho, "
            f"recebeu {len(blob)}"
        )
    if blob[:4] != _MAGIC:
        raise CorruptStreamError("not an ATH2 stream")
    max_order, increment, length, crc = struct.unpack("<BBQI", blob[4:18])
    if max_order < 1 or max_order > 12:
        raise CorruptStreamError(f"max_order={max_order} fora do teto sensato (cabecalho corrompido?)")

    payload = blob[18:]
    m = AthenaPPM(max_order, increment, 'D')
    dec = ArithmeticDecoder(payload)
    out = bytearray()
    for _ in range(length):
        out.append(m.decode_symbol(dec))

    if dec.pos > len(payload) + 8:
        raise CorruptStreamError(
            f"stream ATH2 truncado: decodificacao de {length} simbolos consumiu "
            f"alem do payload disponivel ({dec.pos} vs {len(payload)} bytes)"
        )

    # A ultima verificacao, e a unica que olha o CONTEUDO.
    #
    # As guardas acima atestam que o stream tinha forma valida. Nao
    # atestam que os bytes sao os que entraram: medido, 10 em 400 bits
    # virados devolviam lixo do mesmo tamanho por um caminho de
    # decodificacao perfeitamente valido. Forma correta nao e conteudo
    # correto -- e a mesma licao do bug que tornava este compressor
    # perfeitamente sem perdas e 55% maior.
    real = zlib.crc32(bytes(out)) & 0xFFFFFFFF
    if real != crc:
        raise CorruptStreamError(
            f"checksum mismatch: header says {crc:08x}, decoded data is "
            f"{real:08x}. The stream decoded without structural error but "
            f"the bytes are not the ones that went in."
        )
    return bytes(out)


def _http_logs(target):
    import random
    random.seed(11)
    ips = ["10.0.%d.%d" % (random.randint(0, 255), random.randint(0, 255)) for _ in range(40)]
    paths = ["/", "/login", "/api/v1/users", "/api/v1/orders", "/static/app.js", "/health"]
    ua = ["Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "curl/7.81.0",
          "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0)"]
    out, size = [], 0
    while size < target:
        line = ('%s - - [10/Oct/2024:13:55:36 +0000] "GET %s HTTP/1.1" %d %d "%s"'
                % (random.choice(ips), random.choice(paths),
                   random.choice([200, 200, 200, 304, 404, 500]),
                   random.randint(120, 9000), random.choice(ua)))
        b = line.encode()
        out.append(b)
        size += len(b) + 1
    return b"\n".join(out)


def _source_code(target):
    """Corpus NAO gerado por este arquivo: codigo Python da stdlib.

    O ponto de ter corpora que nao nascem aqui: _http_logs() produz
    exatamente o tipo de texto em que PPM ganha (6 caminhos repetidos,
    3 user-agents). Medir so nele e escolher o adversario.
    """
    import pathlib
    import sysconfig
    out = bytearray()
    for f in sorted(pathlib.Path(sysconfig.get_paths()["stdlib"]).glob("*.py")):
        try:
            out += f.read_bytes()
        except OSError:
            continue
        if len(out) >= target:
            break
    return bytes(out[:target])


def _json_records(target):
    """Dados estruturados com campos de alta entropia (nomes, floats)."""
    import json
    import random
    import string
    random.seed(7)
    recs = [{"id": i,
             "name": "".join(random.choices(string.ascii_letters, k=12)),
             "v": random.random(),
             "tags": random.sample(["a", "b", "c", "d", "e"], 3)}
            for i in range(target // 60 + 1)]
    return json.dumps(recs).encode()[:target]


def _binary_blob(target):
    """Binario real da maquina; se indisponivel, cai para ruido estruturado."""
    for candidate in ("/bin/bash", "/bin/sh", "/usr/bin/python3"):
        try:
            with open(candidate, "rb") as fh:
                d = fh.read(target)
            if len(d) >= target // 2:
                return d
        except OSError:
            continue
    return (bytes(range(256)) * (target // 256 + 1))[:target]


def _incompressible(target):
    """Pior caso. Nenhum compressor ganha aqui -- o que se mede e quanto
    cada um PERDE. lzma para em ~0.999x porque tem bloco 'stored';
    este arquivo nao tem, e expande.

    Usa PRNG com semente fixa, NAO os.urandom(): com urandom o corpus
    mudava a cada execucao e o ratio oscilava na terceira casa (0.904 /
    0.905), de modo que nenhum numero publicado podia ser conferido.
    Um benchmark que nao reproduz nao e um benchmark -- e um boato com
    casas decimais.
    """
    import random
    rnd = random.Random(20260923)
    return bytes(rnd.getrandbits(8) for _ in range(target))


CORPORA = [
    ("http logs (gerado aqui)", _http_logs),
    ("source code (stdlib)", _source_code),
    ("json records", _json_records),
    ("binary (/bin/bash)", _binary_blob),
    ("random (worst case)", _incompressible),
]


def run_scaling(order: int = 6):
    """Como a vantagem do PPM aparece conforme o corpus cresce.

    MEDIDO: no corpus de 120 KiB este compressor PERDE para o gzip em
    codigo-fonte (4.015x contra 4.050x). Em 1 MB do mesmo material ele
    GANHA (4.614x contra 4.270x). O modelo PPM nao tem valor nenhum
    enquanto nao viu material suficiente para prever.

    Uma medicao num unico tamanho esconde essa curva -- do mesmo jeito
    que uma medicao num unico corpus escondia as derrotas. Um ponto nao
    e uma medicao; e um ponto.
    """
    print()
    print("=" * 76)
    print(f" Como a vantagem aparece com o tamanho (codigo-fonte real, order={order})")
    print("=" * 76)
    print(f"{'tamanho':>10}{'gzip-9':>10}{'lzma-9':>10}{'Athena':>10}"
          f"{'vs gzip':>10}{'MiB/s':>9}")
    print("-" * 76)
    completo = _source_code(1_100_000)
    for n in (16 * 1024, 64 * 1024, 256 * 1024, 1024 * 1024):
        d = completo[:n]
        if len(d) < n:
            break
        gr = len(d) / len(gzip.compress(d, 9))
        zr = len(d) / len(lzma.compress(d, preset=9))
        t0 = time.perf_counter()
        blob = compress(d, order)
        dt = time.perf_counter() - t0
        ar = len(d) / len(blob)
        marca = "GANHA" if ar > gr else "perde"
        print(f"{n // 1024:>8} KB{gr:>9.3f}x{zr:>9.3f}x{ar:>9.3f}x"
              f"{marca:>10}{(n / 1048576) / dt:>9.3f}")
    print("-" * 76)
    print("  O cruzamento e o resultado: PPM so passa a valer a pena depois")
    print("  de ver material suficiente. Contra o lzma ele perde em todos.")
    print("  Custo: a memoria cresce sem limite. 1 MB -> ~166 MB de RSS.")


def _self_test():
    passed = 0
    total = 0

    def check(name, cond):
        nonlocal passed, total
        total += 1
        if cond:
            passed += 1
        print(f"  [{total}] {name}: {'OK' if cond else 'FALHOU'}")

    samples = [b"", b"Q", b"abracadabra " * 40, bytes(range(256)) * 4,
               os.urandom(3000), b"GET /x HTTP/1.1\r\n" * 80]
    for mo in (1, 2, 4, 6):
        for s in samples:
            ok = decompress(compress(s, mo)) == s
            check(f"lossless round-trip (order={mo}, len={len(s)})", ok)

    blob = compress(_http_logs(4000), max_order=4)
    truncated = blob[: len(blob) // 2]
    try:
        decompress(truncated)
        check("blob truncado levanta CorruptStreamError", False)
    except CorruptStreamError:
        check("blob truncado levanta CorruptStreamError", True)

    try:
        decompress(b"XXXX")
        check("magic errado levanta CorruptStreamError", False)
    except CorruptStreamError:
        check("magic errado levanta CorruptStreamError", True)

    # Os dois achados da auditoria de 2026-09-24. Nenhum dos 26 testes
    # anteriores os teria pego: ambos passam por caminhos de decodificacao
    # estruturalmente validos.
    import struct as _st

    alvo = compress(b"hello world " * 100, 6)

    # (a) campo de tamanho e um inteiro de 64 bits sem validacao. Virar um
    # bit transformava length=1200 em 9.223.372.036.854.777.008 e a
    # descompressao rodava sem parar, sem erro. Um arquivo de 33 bytes
    # travava a maquina de quem abrisse.
    quebrado = bytearray(alvo)
    quebrado[13] ^= 0x80
    try:
        decompress(bytes(quebrado))
        check("tamanho absurdo no cabecalho levanta CorruptStreamError", False)
    except CorruptStreamError:
        check("tamanho absurdo no cabecalho levanta CorruptStreamError", True)

    # (b) bit virado no payload devolvia lixo do MESMO TAMANHO em 10 de 400
    # tentativas, sem erro nenhum. Forma valida nao e conteudo valido.
    pego = 0
    for i in range(24):
        c = bytearray(alvo)
        c[18 + (i % max(1, len(alvo) - 18))] ^= 1 << (i % 8)
        try:
            if decompress(bytes(c)) != b"hello world " * 100:
                pego += 0  # devolveu diferente sem erro: nao conta como pego
            else:
                pego += 1  # bit irrelevante, saida correta
        except CorruptStreamError:
            pego += 1
    check(f"bit virado no payload nunca devolve lixo silencioso ({pego}/24)", pego == 24)

    print(f"\n{passed}/{total} self-tests passando")
    return passed == total


def run_benchmark(size: int = 120 * 1024, order: int = 6):
    """Roda em CINCO corpora, nao em um.

    A versao anterior media so _http_logs() -- corpus gerado por este
    proprio arquivo, com 6 caminhos e 3 user-agents repetidos, que e o
    terreno onde PPM ganha de lavada. Ela imprimia "<- beats gzip &
    lzma", e a frase era verdadeira sobre aquele corpus e falsa como
    afirmacao geral. Medido: em codigo-fonte, JSON, binario e ruido,
    este compressor PERDE para o lzma nos quatro.

    Um numero verdadeiro sobre um corpus escolhido nao e uma medicao do
    compressor; e uma medicao do corpus. O benchmark agora imprime os
    dois lados e conta o placar, inclusive quando o placar e ruim.
    """
    print("=" * 76)
    print(f" AthenaPPM (PPMd) -- honest benchmark, order={order}, {size:,} B per corpus")
    print(" Ratios measured with the real arithmetic coder. Higher is better.")
    print("=" * 76)
    print(f"{'corpus':<26}{'gzip-9':>9}{'lzma-9':>9}{'Athena':>9}{'vs lzma':>9}"
          f"{'MiB/s':>9}  lossless")
    print("-" * 76)

    wins_gzip = wins_lzma = 0
    for label, make in CORPORA:
        data = make(size)
        n = len(data)
        gr = n / len(gzip.compress(data, 9))
        zr = n / len(lzma.compress(data, preset=9))
        t = time.perf_counter()
        blob = compress(data, order)
        c_s = time.perf_counter() - t
        ok = decompress(blob) == data
        ar = n / len(blob)
        wins_gzip += ar > gr
        wins_lzma += ar > zr
        print(f"{label:<26}{gr:>8.3f}x{zr:>8.3f}x{ar:>8.3f}x{ar / zr:>8.2f}x"
              f"{(n / 1048576) / c_s:>9.3f}  {ok}")

    total = len(CORPORA)
    print("-" * 76)
    print(f"  beats gzip on {wins_gzip}/{total} corpora, lzma on {wins_lzma}/{total}.")
    print("  Throughput: gzip runs at 15-80 MiB/s on the same machine.")
    print("  This is pure CPython and is 50-150x slower. That is the trade.")
    if wins_lzma < total:
        print("  NOTE: losing to lzma on a corpus is the expected result, not a bug.")
        print("  PPM wins on repetitive structured text and loses elsewhere.")
    print("  No stored-block fallback: incompressible input EXPANDS by ~10%.")
    run_scaling(order)


def main(argv=None):
    ap = argparse.ArgumentParser(description="AthenaPPM (PPMd) lossless compressor")
    sub = ap.add_subparsers(dest="cmd")
    ap.add_argument("--size", type=int, default=120 * 1024)
    ap.add_argument("--order", type=int, default=6)
    ap.add_argument("--selftest", action="store_true")
    c = sub.add_parser("compress")
    c.add_argument("infile")
    c.add_argument("outfile")
    c.add_argument("--order", type=int, default=6)
    d = sub.add_parser("decompress")
    d.add_argument("infile")
    d.add_argument("outfile")
    args = ap.parse_args(argv)

    if args.cmd == "compress":
        data = open(args.infile, "rb").read()
        blob = compress(data, args.order)
        open(args.outfile, "wb").write(blob)
        print(f"{len(data):,} -> {len(blob):,} bytes ({len(data) / len(blob):.3f}x)")
    elif args.cmd == "decompress":
        data = decompress(open(args.infile, "rb").read())
        open(args.outfile, "wb").write(data)
        print(f"restored {len(data):,} bytes")
    else:
        ok = True
        if args.selftest:
            ok = _self_test()
        run_benchmark(args.size, args.order)
        if not ok:
            raise SystemExit(1)


if __name__ == "__main__":
    main()

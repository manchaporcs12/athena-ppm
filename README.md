# AthenaPPM

A PPMd-style lossless compressor — arithmetic coder, statistical model,
escape method C/D — in pure CPython. Zero dependencies, single file.

Written to understand how statistical compression actually works. Not
written to replace zstd, and the numbers below say why.

## Where it actually stands

Run `python3 athena_ppm.py` and it measures itself against gzip and lzma
on five corpora, 120 KiB each. Measured 2026-09-23 on an Apple M4,
CPython 3.13:

```
corpus                       gzip-9   lzma-9   Athena  vs lzma    MiB/s  lossless
http logs (generated here)  14.626x  16.054x  21.754x    1.36x    0.545  True
source code (stdlib)         4.050x   4.406x   4.015x    0.91x    0.321  True
json records                 3.194x   4.087x   3.520x    0.86x    0.300  True
binary (/bin/bash)           1.926x   2.121x   1.946x    0.92x    0.137  True
random (worst case)          1.000x   0.999x   0.905x    0.91x    0.054  True

  beats gzip on 3/5 corpora, lzma on 1/5.
```

Read that honestly: **it beats lzma on exactly one corpus, and that is
the one this file generates itself** — templated HTTP logs with six
repeated paths and three user-agents, which is the precise shape PPM
wins on. On real source code, JSON and binaries it loses to lzma every
time.

It is also slow. 0.05–0.55 MiB/s against gzip's 15–80 MiB/s on the same
machine: **50–150x slower**. A 100 MB file takes minutes, not seconds.

Two known limitations, stated rather than hidden.

**No stored-block fallback**, so incompressible input **expands by about
10%**. lzma holds at 0.999x because it falls back to storing raw. This
does not.

**The model grows without bound.** Contexts are added and never evicted,
so memory scales with the variety of the input, not with a budget.
Measured on 1 MB of real source code:

```
order 4    ratio 4.446x    2.9s    peak RSS  ~55 MB
order 6    ratio 4.614x    3.2s    peak RSS ~166 MB
order 8    ratio 4.613x    3.8s    peak RSS ~344 MB
```

That is roughly 166x the input size at the default order. A 100 MB file
would exhaust memory on most machines. Treat a few MB as the practical
ceiling. Note also that order 8 costs twice the memory of order 6 and
buys nothing.

Worth knowing in the other direction: on 1 MB of real source code it
reaches **4.614x against gzip's 4.270x** — it beats gzip there, while on
the 120 KiB sample above it does not. PPM needs data before its model is
worth anything.

Use it to learn how PPM works. Use zstd in production.

## The bug worth reading about

An earlier draft advanced the context history *before* recording the
symbol. Every count landed in the wrong context — off by exactly one
position.

It was still perfectly lossless. Every round-trip test passed: compress,
decompress, bytes identical, green across the board. And it **expanded**
real data to 1.55x its original size.

```
broken update   0.64x   (output larger than input)
correct update 21.75x
```

Encoder and decoder were wrong in the same way, so the data survived
intact. The model just predicted nothing useful.

> A lossless round-trip proves you did not corrupt the data.
> It does not prove you compressed it.

The test suite only checked the first thing.

## The second bug, which was in the benchmark

The header of this file used to end with *"beats gzip 14.4x and lzma
16.0x"*. That sentence was true — measured, reproducible, not invented.
It was measured on the one corpus this file generates for itself.

A true number measured on a corpus you chose is a measurement of the
corpus, not of the compressor.

The benchmark now runs five corpora, prints the losses, and counts the
scoreboard including when the scoreboard is bad.

## Usage

```bash
python3 athena_ppm.py --selftest          # 26 round-trip + corruption tests
python3 athena_ppm.py                     # benchmark vs gzip/lzma, 5 corpora
python3 athena_ppm.py --order 4           # change model order (default 6)

python3 athena_ppm.py compress   input.txt output.ath
python3 athena_ppm.py decompress output.ath restored.txt
```

Corrupted or truncated streams raise `CorruptStreamError` rather than
returning silent garbage. Both cases are covered by the self-test.

## Self-test

```
26/26 self-tests passing
```

Round-trip across model orders 1, 2, 4 and 6 against six payloads,
including the two cases that are usually missing: **zero-length input**
and **single-byte input**. Plus truncated-stream and bad-magic rejection.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Mario Lucas Ota.

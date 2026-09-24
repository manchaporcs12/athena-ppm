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

## Where the crossover is

`python3 athena_ppm.py` also prints how the advantage appears with
input size, on real CPython stdlib source:

```
   size     gzip-9    lzma-9    Athena   vs gzip    MiB/s
   16 KB    2.697x    2.777x    2.663x     loses    0.262
   64 KB    3.975x    4.243x    3.953x     loses    0.303
  256 KB    4.048x    4.555x    4.138x      WINS    0.302
 1024 KB    4.270x    5.080x    4.611x      WINS    0.299
```

On **source code** it loses to gzip below ~100 KB and wins above it.

I first wrote that sentence without the words "on source code", and it
was wrong. On `/usr/share/dict/words` — an alphabetical word list, a
completely different shape of text — there is no crossover at all:

```
   size     gzip-9    lzma-9    Athena   vs gzip
   16 KB    3.278x    3.549x    3.253x     loses
   64 KB    3.132x    3.564x    3.022x     loses
  256 KB    3.146x    3.661x    2.957x     loses
 2435 KB    3.306x    3.912x    2.997x     loses
```

It loses at every size. **The crossover belongs to the corpus, not to
the compressor.** Against lzma it loses on both corpora at every size.

That is the third time in this file that a true measurement got stated
more broadly than it supported — first one corpus, then one size, now
one kind of text. The benchmark now runs both corpora and prints both
curves, so the claim can only be as wide as the measurement.

A point is not a measurement. Neither is a line.

## What made it better, and what did not

Two techniques from the PPM literature were implemented and measured.
One was kept. One was measured and rejected, which is the more useful
half of the story.

**Update exclusion (Shkarin / PPMII) — kept.** The model was updating
*every* order on every symbol, including the short contexts that were
never consulted because a longer one had already coded the symbol. That
fills low orders with statistics they never use to predict, and dilutes
the ones they do.

```
512 KiB, order 6        before      after
  source code           4.580x     4.884x   +6.6%
  dictionary            2.998x     3.135x   +4.6%
  speed                 0.210      0.248 MiB/s   +18%
```

Four lines. It helps everywhere and it is faster, because it touches
fewer tables.

**Exclusion after estimation — measured and rejected.** A documented
PPMD technique: when symbols are excluded by a higher-order context,
don't shrink the escape estimate as if they had never existed. Two
lines: `esc = len(items)` becomes `esc = len(ctx)`.

```
512 KiB, order 6        delta
  source code           +0.66%
  http logs             +0.30%
  dictionary            -0.46%
```

Everything under 1%, and it trades one corpus for another. Taking it
would mean choosing which kind of text this compressor looks good on —
which is the original sin of this file's first benchmark, in a more
sophisticated costume. It also makes the escape weight harder to read,
and readability is the only thing here worth protecting.

Proposed and independently measured by an external reviewer, who
predicted +0.606% on source code and flagged that he could not predict
what the dictionary would do. Measured here: +0.66%, and the dictionary
fell. Both halves of that prediction were right.

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
returning silent garbage.

That sentence was not true until an adversarial pass on 2026-09-24 found
two ways to break it.

**A one-bit flip could hang the decoder forever.** The original length is
stored as an unverified 64-bit integer. Flipping its top bit turned
`length=1200` into `9,223,372,036,854,777,008`, and the bit reader
returned zeros past end-of-data instead of failing — so a **33-byte file
decompressed indefinitely, with no error**. Anyone opening an untrusted
`.ath` was exposed. The fix refuses to read more than 16 bytes past the
end of the stream.

**A one-bit flip could return wrong bytes silently.** Across 400 single-bit
flips, 10 came back the *same length* as the original with different
contents, through a perfectly valid decode path, and 7 came back at a
different length — all without raising. The format had no integrity check
at all. `ATH2` adds a CRC32 of the original data to the header.

```
400 single-bit flips        ATH1    ATH2
  raised an error            379     396
  wrong length, no error       7       0
  wrong bytes, no error       10       0
```

Cost: 4 bytes per file, and the fuzz run went from not finishing in ten
minutes to 0.7 seconds. Both are now self-tests 27 and 28.

**A third one came from an outside audit, and it is the one I missed.**
`increment` is read from the header and was never validated. With
`increment=0` every count stays at zero, the method-D weight becomes
`2*0-1 = -1`, the interval total reaches zero, and the process dies with
a raw `ZeroDivisionError` instead of refusing. One header byte, one
traceback.

My own 400-flip fuzzing never found it because I was flipping bits in
the payload, not in the header fields. Fuzzing only finds what you aim
it at.

Both header bytes are now swept exhaustively in self-test 29: 512
corrupted headers, 0 raw crashes.

This is the same lesson as the compression bug, one layer up: a decode
that follows a structurally valid path is not a decode that returned your
data. Valid form is not valid content.

## Self-test

```
29/29 self-tests passing
```

Round-trip across model orders 1, 2, 4 and 6 against six payloads,
including the two cases that are usually missing: **zero-length input**
and **single-byte input**. Plus truncated-stream and bad-magic rejection.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Mario Lucas Ota.

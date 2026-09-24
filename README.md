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
http logs (generated here)  14.626x  16.054x  25.010x    1.56x    0.394  True
source code (stdlib)         4.050x   4.406x   4.480x    1.02x    0.184  True
json records                 3.194x   4.087x   3.745x    0.92x    0.166  True
binary (/bin/bash)           1.926x   2.121x   2.063x    0.97x    0.068  True
random (worst case)          1.000x   0.999x   0.905x    0.91x    0.025  True

  beats gzip on 4/5 corpora, lzma on 2/5.
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

## Where the crossover is, and why it is not one thing

`python3 athena_ppm.py` prints how the advantage moves with input size,
on two corpora of different shape.

**CPython stdlib source** — it wins at every size measured:

```
   size     gzip-9    lzma-9    Athena   vs gzip
   16 KB    2.697x    2.777x    2.935x      WINS
   64 KB    3.975x    4.243x    4.375x      WINS
  256 KB    4.048x    4.555x    4.572x      WINS
 1024 KB    4.270x    5.080x    4.980x      WINS
```

**`/usr/share/dict/words`**, an alphabetical word list — the curve runs
the *other way*:

```
   size     gzip-9    lzma-9    Athena   vs gzip
   16 KB    3.278x    3.549x    3.456x      WINS
   64 KB    3.132x    3.564x    3.210x      WINS
  256 KB    3.146x    3.661x    3.110x     loses
 1024 KB    3.276x    3.855x    3.152x     loses
 2435 KB    3.306x    3.912x    3.109x     loses
```

It wins small and loses large. On source code it wins throughout. **The
crossover belongs to the corpus, not to the compressor** — and it does
not even point the same direction on both.

An earlier version of this file said "it loses to gzip below ~100 KB and
wins above it". That was measured on source code only, and it was the
third time in this project that a true number was stated more broadly
than it supported: first one corpus, then one size, then one kind of
text. The benchmark now runs both corpora and prints both curves.

A point is not a measurement. Neither is a line.

Against lzma it loses on both corpora at every size.

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

### What came out of the rejections

Three reviewers proposed techniques. All three lost on at least one
corpus, so all three were rejected. Then the table itself said something
none of the individual results did:

```
                          source   logs    dict    json   binary
  deterministic scaling   +1.89%  +1.74%  -1.44%  -1.29%  -1.04%
```

That technique is not good or bad. **It is good on structured text and
bad on everything else.** Everyone who evaluates a technique on its
average throws this away. Measuring the corpora separately makes the
move obvious: don't choose.

`compress()` now runs both models and keeps the smaller output, with one
bit in the header recording which won.

```
corpus          before      after    delta   chose
source code    4.8844x    4.9764x   +1.89%   scaled
http logs     28.1667x   28.6578x   +1.74%   scaled
dictionary     3.1351x    3.1351x    0.00%   plain
json records   3.8327x    3.8327x    0.00%   plain
binary         2.0308x    2.0308x    0.00%   plain
random         0.8779x    0.8779x    0.00%   plain
```

**Zero regressions, by construction** — it cannot be worse than the
better of the two. This is the only proposal in this project that passed
the test that killed the others: *does it win everywhere, or does it
pick a corpus?*

The cost lands in the right place: **compression is 2x slower,
decompression is unchanged** (0.76s vs 0.78s on 256 KiB). The decoder
reads the bit and runs once. Whoever ships the file pays; whoever
downloads it does not.

This is not a new compression technique and I am not going to dress it
up as one. It is "try both, keep the smaller", which is about as old as
compression itself. What was new here was only the reason to try it: the
per-corpus table. Averaging hides the case where a technique is a clean
win half the time.

### Three techniques, measured and rejected

Three reviewers proposed documented PPM techniques. All three were
implemented and measured against the same 512 KiB baseline. All three
were rejected. The numbers are the useful part.

```
                          source   logs    dict    json   binary
  SEE-lite                -4.98%  -5.00%  -3.10%     --      --
  deterministic scaling   +1.89%  +1.74%  -1.44%  -1.29%  -1.04%
    conditioned c>=2      +0.64%  +1.15%  -0.62%  +0.22%  -0.03%
```

**SEE-lite** (secondary escape estimation, PPMZ/Bloom). Instead of
assuming the escape is worth `len(items)`, learn from the file itself
how often similar contexts actually escape — 16 buckets of
`(hits, escapes)`. The reviewer estimated +1–3% and said plainly it was
an estimate, not a measurement. Measured: **−3% to −5% on everything.**
Real PPMZ SEE is far more elaborate than 25 readable lines; this says
nothing about the technique, only about this implementation of it.

**Deterministic scaling** (Teahan & Cleary, 1997). Double a symbol's
weight when its context has exactly one prediction. Two lines. The
reviewer predicted +2.13% on source (measured +1.89%) and predicted
−1.29% on noisy data — measured −1.29% exactly. A conditioned variant
(`c >= 2`) was also predicted and measured: right on source, wrong
direction on the dictionary.

Both predictions were unusually good. Neither technique survives the
same test that killed the first benchmark: **does it win on every
corpus, or does it pick one?** Under 2%, and trading one kind of text
for another, is not an improvement — it is choosing what to look good
at.

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

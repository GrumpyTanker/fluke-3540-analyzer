# Fluke 3540 FC `trend.bin` binary format

This document describes the layout of `trend.bin` files produced by the
**Fluke 3540 FC** three-phase power-quality logger, reverse-engineered by
combining record probing, FLUKELOAD's original field map, and per-record
electrical-relationship verification (`P² + Q² = S²`, `PF = P / S`,
`Wh = P / 3600`, NEMA imbalance, etc.).

The canonical machine-readable form of everything below lives in
[`../spec/field_map.json`](../spec/field_map.json). Both the Python and
JavaScript parsers load from that file at module-init time, so they stay
in lockstep.

## File structure

A `trend.bin` is a flat stream of fixed-size **records**, one per second,
each 744 bytes:

```
record[0]   bytes  0  – 743
record[1]   bytes 744 – 1487
record[2]   bytes 1488 – 2231
...
```

File size is always a multiple of 744. There is no file-level header.

## Record layout

Each 744-byte record:

| Offset | Size | Type | Field |
|---|---|---|---|
| 0  | 4 | bytes        | magic `46 00 E8 02` |
| 4  | 8 | filetime\*   | window start (FILETIME) |
| 12 | 8 | filetime\*   | window end   (FILETIME) |
| 20 | 4 | uint32 LE    | reserved / count |
| 24 | 720 (180 × 4) | float32 LE | measurement payload |

\*FILETIME is stored as **two consecutive little-endian uint32 words**, where
the *first* word in memory is the *high* 32 bits and the *second* is the *low*
32 bits. To reconstruct, read both LE uint32s and combine as
`(high << 32) | low`. The Python parser does:

```python
start_hi, start_lo = struct.unpack_from("<II", chunk, 4)
start_ft = (start_hi << 32) | start_lo
```

and the JS port mirrors:

```javascript
const startHi = BigInt(view.getUint32(offset + 4, true));
const startLo = BigInt(view.getUint32(offset + 8, true));
const startFt = (startHi << 32n) | startLo;
```

### FILETIME → datetime

FILETIME counts 100-nanosecond ticks since `1601-01-01 00:00:00 UTC`.

```
unix_ms = filetime / 10_000 − 11_644_473_600_000
```

The constant `11,644,473,600,000 ms` is the gap between 1601-01-01 and the
Unix epoch (1970-01-01) in milliseconds.

### Float payload

180 IEEE-754 float32 little-endian values, indices 0 – 179. The full mapping
is in `spec/field_map.json`. Highlights:

| Indices | Group | Notes |
|---|---|---|
| 0 – 8   | V_LN per phase | min / max / avg, 3 phases |
| 9 – 17  | V_LL per pair  | ab, bc, ca (min / max / avg) |
| 18 – 26 | I per phase    | min / max / avg |
| 27 – 35 | V_THD %        | per phase, min/max/avg |
| 36 – 44 | I_THD %        | per phase, min/max/avg |
| 45 – 47 | freq           | min / max / avg, Hz |
| 48 – 59 | P (RMS active) | per phase + total, min/max/avg |
| 60 – 71 | S (apparent)   | per phase + total |
| 72 – 83 | Q (reactive)   | per phase + total |
| 84 – 119 | P1 / S1 / Q1  | fundamental only |
| 120 – 131 | PF            | true (`P/S`), per phase + total |
| 132 – 143 | DPF           | displacement (`P1/S1`) |
| 144 – 155 | Wh / VAh / VARh | per-second energy increments |
| 156 – 163 | misc          | partly-identified low-confidence fields |
| 164 – 169 | (all-NaN)     | omitted from CSV output |
| 170 – 179 | rolling/demand | per-phase + aggregate rolling values |

### Confidence levels

`spec/field_map.json` annotates each field with one of:

- **H** — verified by a physical relationship (e.g. `S² = P² + Q²`,
  `PF = P / S`, `Wh ≈ P / 3600`)
- **M** — strong inferential evidence (units, range, correlation pattern)
- **L** — weak inference; likely interpretation but not verified
- **?** — present but role unknown

When in doubt, check the confidence level before trusting a value
downstream.

## CT direction (sign of P / Q / PF / DPF / Wh / VARh)

Power-related quantities flip sign if the iFlex CT probes are installed
backwards. The fix is parameter-only, not destructive: see
[`CT_REVERSAL.md`](CT_REVERSAL.md) for which columns are negated and how
to detect/correct this.

## Sister files in `ES.NNN/`

A complete session directory typically contains:

| File | Purpose | Required by parser? |
|---|---|---|
| `trend.bin`           | the per-second record stream described above | yes |
| `trend-meta.bin`      | metadata about the trend stream | no (not yet parsed) |
| `session-meta.bin`    | session ID, start/stop timestamps | no |
| `configuration.bin`   | instrument config snapshot | no |
| `ES.NNN-config.json`  | asset name, team, instrument firmware | optional (used for Summary metadata) |

The parser only needs `trend.bin`; everything else is for nicer reports.

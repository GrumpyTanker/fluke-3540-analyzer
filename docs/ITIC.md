# ITIC / CBEMA dip classification

Each voltage dip, swell, and outage detected in a session is classified against
the **ITIC (Information Technology Industry Council) curve** — the modern
successor to the CBEMA curve. The ITIC curve describes the voltage-deviation /
duration envelope that IT and process-control equipment is *expected to ride
through* without misoperation or damage.

This matters for the P115RE rectifier work: a PLC that drops on a voltage event
is almost always responding to a dip that fell into the ITIC **prohibited** or
**no-damage (dropout)** region. Classifying every event turns "there was a dip"
into "this dip was deep/long enough that a PLC should be expected to trip."

## The three regions

For a given event we compute two numbers:

- **Residual voltage %** — the voltage *during* the event as a percentage of
  nominal. A dip to 70 % of nominal → `residual_pct = 70`. A full outage → `0`.
  A 30 % swell → `130`.
- **Duration (s)** — how long the event lasted.

The `(residual_pct, duration)` point is then classified:

| Class | Meaning |
|-------|---------|
| `no_interruption` | Inside the ITIC envelope — equipment should keep running. |
| `prohibited` | Above the upper bound (over-voltage region) — may damage equipment. |
| `no_damage` | Below the lower bound (under-voltage / dropout region) — equipment may shut down but should not be damaged. |

A PLC stop is most strongly correlated with `no_damage` (the supply sagged below
the ride-through floor and the controller dropped) or, for surges, `prohibited`.

## The envelope used here

The classifier uses a conservative step approximation of the published ITIC
breakpoints (residual % vs. duration):

**Lower bound (below this = `no_damage` / dropout):**

| Duration | Residual floor |
|----------|----------------|
| < 1 ms | 0 % (any transient tolerated) |
| ≤ 20 ms | 70 % |
| ≤ 0.5 s | 70 % |
| ≤ 10 s | 80 % |
| steady state | 90 % |

**Upper bound (above this = `prohibited` / over-voltage):**

| Duration | Residual ceiling |
|----------|------------------|
| ≤ 1 ms | 500 % |
| ≤ 3 ms | 200 % |
| ≤ 0.5 s | 120 % |
| ≤ 10 s | 120 % |
| steady state | 110 % |

These are intentionally on the conservative side of the official curve so the
classifier errs toward flagging events as out-of-envelope rather than silently
passing a borderline dip.

## Where it shows up

- **`events.json`** — every dip/outage/swell gets an `itic` block:
  ```json
  {
    "kind": "dip", "t_start": "2026-06-02T18:14:03+00:00", ...,
    "itic": { "residual_pct": 62.1, "duration_secs": 4.0, "itic_class": "no_damage" }
  }
  ```
- **Per-bucket `events.json`** under `--split-by` carries the same block.
- **Insights / findings** call out when multiple dips fall in the prohibited or
  no-damage region (e.g. "3 dips fall in the ITIC prohibited region — likely to
  drop a PLC").

## Implementation

`classify_itic(residual_pct, duration_secs)` and `event_itic(event, nominal_ln_v)`
in [`python/src/fluke_3540/analysis.py`](../python/src/fluke_3540/analysis.py).
Pure stdlib; unit-tested in `python/tests/test_analysis_itic.py`.

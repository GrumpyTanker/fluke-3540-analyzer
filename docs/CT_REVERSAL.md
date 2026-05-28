# CT reversal — `--reverse-cts` flag

iFlex (and most clamp-on) current transformers are **direction-sensitive**.
If a probe is clamped onto a conductor with the arrow pointing the wrong way,
the meter will record the current's *sign* inverted — and since the meter
computes real power from instantaneous V × I, every signed power-domain
quantity gets flipped too.

This is one of the most common installation mistakes on a multi-day power
logger run. Spotting it after the fact is easy, and correcting it is
parameter-only — no need to re-run the survey.

## How to spot it

Open the session and look at `P_total_avg_W` (or the equivalent in the web
app's Active Power chart). A backwards CT shows up as:

- **Net negative energy** at a site that you know is consuming power
  (e.g. an EV charger, an HVAC, a panel feeding a building — anything that
  isn't actively exporting to the grid)
- `PF` consistently near **−1** instead of near **+1** during steady load
- The exported-energy column (`wh_rev` in the Summary) far exceeds imports
  on a load that doesn't generate

If exactly one phase looks wrong, only that probe is reversed and the
others are correct — but the global `--reverse-cts` flag flips *all* of
them, so use phase-specific reasoning before reaching for it.

## What `--reverse-cts` does

When the flag is set, the parser negates the values of every field whose
name starts with one of these prefixes:

- `P_`     — RMS active power
- `P1_`    — fundamental active power
- `Q_`     — RMS reactive power
- `Q1_`    — fundamental reactive power
- `PF_`    — true power factor
- `DPF_`   — displacement power factor
- `Wh_`    — active energy
- `VARh_`  — reactive energy

The list lives at `spec/field_map.json` → `reverse_cts_prefixes` and is
shared by both implementations.

**Sign-independent quantities are not touched:**

- `V_LN_*`, `V_LL_*` — voltages
- `I_*` — RMS current (magnitude only)
- `S_*`, `S1_*` — apparent power
- `VAh_*` — apparent energy
- `V_THD_*`, `I_THD_*` — harmonic distortion
- `freq_*` — frequency
- rolling / demand columns

## CLI

```bash
# Flip all three phases (the original v0.1 behavior)
fluke-analyze path/to/ES.NNN -o out/ --reverse-cts

# Flip only phase A — useful when one probe is backwards and the others are fine
fluke-analyze path/to/ES.NNN -o out/ --reverse-cts a

# Flip phases A and C
fluke-analyze path/to/ES.NNN -o out/ --reverse-cts a,c
```

When you flip a subset of phases, the corresponding `*_total_*` columns
(`P_total_avg_W`, `Q_total_avg_VAR`, etc.) are also negated — totals are
sums across phases, so an asymmetric flip changes the total too.

The Summary line at the end of the CLI run prints which phases were
flipped and how many columns were affected.

## Web app

In the Session summary panel after the parse completes, tick any
combination of the **Phase A / Phase B / Phase C** checkboxes under
"Reverse CTs". The app re-parses the cached file buffer in-place
whenever the selection changes; no re-drop needed.

## When NOT to use it

- Sites with bidirectional flow (solar PV, battery storage, V2G chargers)
  where genuinely negative P_total values are expected — `--reverse-cts`
  would just hide real export events.

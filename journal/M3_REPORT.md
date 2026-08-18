# M3 — Chart Renderer · Report

**Date:** 2026-08-18
**Status:** DONE — `make check` green (289 tests), byte-identical renders verified, reproduction from stored candles verified byte-for-byte.

---

## 1. What was built

| Module | Contents |
|---|---|
| `charts/theme.py` | Dark palette as frozen constants: background `#0d1117`, teal/red candles, three distinct EMA hues (none red or green), and a loud `#ff3b30` for the degraded banner. |
| `charts/models.py` | `ChartSpec` (what to draw), `DrawnLevel`, `ChartRenderParams` (the reconstruction record), `ChartImage` (PNG + params). All frozen Pydantic, JSON-safe. |
| `charts/renderer.py` | `render()` for one timeframe, `render_album()` for the analyst's 15m/1h/4h set. Candles, EMA20/50/200, volume panel, S/R lines, watermark. |
| `tools/chart.py` | `python -m sentinel.tools.chart SOLUSDT 1h`, `--all` for the album, `--from-db` to reproduce from stored candles. Writes PNG + params sidecar. |
| Config | `charts:` block in `config.yaml` + `ChartsConfig` — dimensions, DPI, window, panel ratio, EMA periods, level cap, timeframes. |
| Tests | 31 new tests across `tests/charts/`, including pixel-level assertions about what is actually on the canvas. |

## 2. Dimensions and DPI

**1600 × 1000 px at 100 DPI, showing the last 120 closed candles.**

The analyst is `claude-fable-5`, which accepts images up to **2576 px on the long edge** without server-side downscaling (older models cap at 1568 px and downscale anything larger — which blurs exactly the axis numbers and level labels that matter). 1600 px sits comfortably inside that, costs ~2,130 visual tokens per chart (~6.4k for the album), and at DPI 100 `figsize` inches map 1:1 to pixels.

The bigger legibility lever was the **candle window, not the canvas**: 200 candles across the plot area is ~7 px each and bodies merge into a smear; 120 gives ~11 px and a doji stays distinguishable from a full body. The feature engine still uses the full tail — the chart is a view onto it.

## 3. Honest legibility assessment at 1600 × 1000

I rendered live BTCUSDT charts and read them as the analyst would.

**Clearly readable:**
- Individual candle bodies and wicks at ~11 px per candle; direction colour is unambiguous.
- All three EMAs traceable across the full width, distinguishable by colour, with crossovers visible (EMA20 crossing EMA50 is obvious on the 4h).
- All six S/R lines, each labelled with price and touch count (`64,560  12x`).
- Price gridlines and axis labels; time axis labels; the watermark's symbol, timeframe, last-closed timestamp and close.
- Volume bars, direction-coloured, with spikes obvious relative to the baseline.

**Not readable — the analyst must use the JSON for these:**
- **Exact OHLC of any individual candle.** You can read a level off the axis to roughly ±0.08% of the visible range, no better. The chart shows structure; the numbers live in the snapshot.
- **Small-bodied candles in dense stretches.** At 4h with 120 bars, a body under ~0.1% of the range is 1–2 px — visible as a line, but body-vs-wick becomes a guess. Doji identification is reliable on 1h, marginal on 4h.
- **Exact EMA values.** Only their order, slope, and crossings.
- **Precise volume magnitudes.** The volume axis carries three labels; anything between them is eyeballed.

**Two real limitations worth knowing:**
- **Level labels can collide.** Two levels within ~1.5% of each other produce adjacent boxes that nearly touch (seen on the 1h at 64,148 / 63,983). Three or more clustered levels would overlap. Not currently mitigated.
- **The right-edge label boxes slightly overlay the last few candles** — the most recent price action, which is the part that matters most. Minor at the moment; if it bothers you the labels could move into the axis gutter at the cost of plot width.

## 4. Determinism

Same OHLCV → byte-identical PNG. Three things make that true, all tested:
1. `Agg` backend and matplotlib's bundled DejaVu Sans (no display-stack or system-font dependency).
2. PNG metadata stripped — matplotlib stamps `Software` and `Creation Time` into the header by default, which alone breaks byte-equality. A test asserts neither string appears.
3. **No wall-clock anywhere in the drawing path.** The watermark timestamp is the last *closed candle*, derived from data.

`matplotlib==3.11.1` and `mplfinance==0.12.10b0` are **pinned exactly**, per your instruction: a rasterisation change in a future rebuild would break reproducibility of stored signals with no error message.

`--from-db` reproduces the live chart **byte-for-byte** — same sha256 `f7f5519216f6aa43`, same six levels — which is PRD F4's actual requirement demonstrated rather than asserted.

## 5. Bugs found during verification

Three, all caught by looking at output rather than trusting it:

1. **EMA200 was announced but never drawn.** The header key listed EMA200 while the chart had no purple line. A pixel count proved it: EMA200's only pixels were the key text itself (y=39–48), zero in the plot area. Cause: an EMA200 needs 200 candles to produce its *first* value, so a 200-closed-candle tail yields exactly one point — nothing to draw a line between. Two fixes: the renderer now requires ≥2 valid points before claiming an EMA, and the chart timeframes fetch 320 closed candles so EMA200 spans the 120-bar window. **This is a spec deviation** (§6).
2. **Level prices rendered in scientific notation** — `6.458e+04` instead of `64,578`, from `{:,.4g}`. Precision now follows magnitude.
3. **`--from-db` silently drew a partial candle and no levels.** I had assumed stored candles were all closed; M1 actually persists the in-progress bar too. The DB path now applies the same partial-candle rule and rehydrates the *stored* feature block, so a historical chart shows the structure as it was, not today's.

Plus two layout problems visible only on inspection: mplfinance hardcodes its panels to 72% × 70% of the canvas (wide dead margins — wasted pixels the analyst still pays tokens for), and it drew its own legend on top of price action. Panels are now positioned explicitly and the EMA key lives in the header band.

## 6. Deviation from spec

**Chart timeframes now fetch 321 candles (320 closed) instead of 201.** M3 requires EMA200 on the chart; with a 200-closed tail that is arithmetically impossible (one valid point). 320 closed yields 121 valid EMA200 points, enough to span the 120-candle window. Still one API call per timeframe, and the extra rows dedupe away on upsert. `1d` keeps its 100-closed tail, so its EMA200 stays `null` by design. Recorded in [DATA_SOURCES.md §2.1](../docs/specs/DATA_SOURCES.md); revert with one config line if you'd rather have the smaller payload and no EMA200 on charts.

## 7. Decisions

1. **No `chart_renders` table** (your call): params are embedded in the signal record at M5/M6. The tool writes a JSON sidecar in the meantime.
2. **Degraded data is loud** (your call): red bordered box, top-right, naming the degraded fields — the analyst is told to be stricter on degraded data, so it has to see the degradation in the image.
3. **Each chart shows only its own timeframe's levels.** A cross-timeframe overlay is unreadable at this density.
4. **EMAs are computed with `features.indicators.ema`**, never a second implementation, so the line on the chart and the number in the JSON cannot disagree.
5. **Pixel-level tests.** The EMA200 bug was invisible to any assertion about what the code *intended* to draw, so the suite samples rendered pixels and asserts what is actually on the canvas.

## 8. Demo

```bash
python -m sentinel.tools.chart BTCUSDT --all        # 15m + 1h + 4h album
python -m sentinel.tools.chart BTCUSDT 1h --from-db # reproduce from stored candles
```

Verified: three PNGs at 1600×1000, ~101–130 KB each, 120 candles, EMAs [20, 50, 200], 6 levels on 1h/4h; `--from-db` byte-identical to the live render; two consecutive renders identical; DEGRADED banner renders when the snapshot is degraded.

## 9. Notes for M5 (LLM pipeline)

- `render_album()` returns the three images in the order the analyst prompt expects (4h context, 1h primary, 15m timing — reorder the specs if PROMPTS §2 wants that sequence).
- `ChartRenderParams` is JSON-safe and belongs in the signal record alongside the analyst report.
- Base64-encoding for the vision block is M5's job; the renderer returns raw PNG bytes.
- Budget ~2,130 visual tokens per chart, ~6.4k per analysis, when estimating LLM cost.

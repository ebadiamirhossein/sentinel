# Cassettes

Recorded response payloads. The test suite replays these and **never opens a socket**
(`tests/conftest.py` installs an autouse fixture that fails any real connection).

| File | Source | How |
|---|---|---|
| `binance_*.json` | Binance USDT-M futures, public keyless endpoints | ccxt's *parsed* output — that is what the adapter consumes |
| `alternative_fng.json` | https://api.alternative.me/fng/?limit=2 | raw response |
| `coingecko_global.json` | https://api.coingecko.com/api/v3/global | raw response |
| `frankfurter_latest.json` | https://api.frankfurter.dev/v1/latest?base=EUR&symbols=USD | raw response |
| `coindesk_rss.xml` | https://www.coindesk.com/arc/outboundfeeds/rss/ | raw feed, truncated to 60 KB |
| `cryptopanic_posts.json` | CryptoPanic `/api/v1/posts/` | **hand-authored** from the documented response shape — recording needs an API key |

Recorded 2026-08-18, **except `binance_*_DOGEUSDT.*`, recorded 2026-08-21** when it
was added as a third golden symbol (M10b-2). The dates differ on purpose: adding a
symbol must not re-record the others, because every golden in the repo is computed
from these files and refreshing them all would move every fixture at once. Nothing
compares two symbols against each other, so a per-symbol recording date is not a
problem — it is the evidence that the addition was an addition.

Candle series are trimmed to 60 rows to keep the repo small; production tail lengths
live in `config.yaml` and are asserted from the request parameters, not from fixture
length.

## The three golden symbols

`BTCUSDT`, `SOLUSDT` and `DOGEUSDT` are recorded because each sits in a **different
branch** of `charts/renderer._format_price`, which labels every S/R line on a chart:
above 1000, between 1 and 1000, and below 1. Until M10b-2 only BTCUSDT was pinned, so
a change to either other branch would have silently altered the stored charts of six
live watchlist symbols with the whole suite green. See `journal/M10b_2_REPORT.md` §4a.
`tests/golden/test_golden_cycle.py::test_every_formatter_branch_is_covered_by_exactly_one_golden_symbol`
asserts the partition holds, so swapping one for another large-cap fails rather than
quietly covering the same branch twice.

## Refreshing

```bash
python -m sentinel.tools.record_cassettes                     # everything — DESTRUCTIVE
python -m sentinel.tools.record_cassettes --symbols DOGEUSDT --skip-http   # add one
```

Live, read-only, keyless. Re-record when a provider changes its response shape —
then run `make test` and fix whatever the parsers now get wrong.

**The no-argument form refreshes every symbol**, which moves every golden fixture in
the repo at once — a regeneration wearing an addition's clothes. `--symbols` narrows
the run to what you actually mean to touch, and `--skip-http` leaves the shared
sentiment/macro/FX/RSS fixtures alone. Use both when adding a symbol. If you add a
`CRYPTOPANIC_API_KEY` to `.env`, replace the hand-authored CryptoPanic fixture
with a real recording.

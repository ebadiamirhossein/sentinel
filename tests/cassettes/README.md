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

Recorded 2026-08-18. Candle series are trimmed to 60 rows to keep the repo small;
production tail lengths live in `config.yaml` and are asserted from the request
parameters, not from fixture length.

## Refreshing

```bash
python -m sentinel.tools.record_cassettes
```

Live, read-only, keyless. Re-record when a provider changes its response shape —
then run `make test` and fix whatever the parsers now get wrong. If you add a
`CRYPTOPANIC_API_KEY` to `.env`, replace the hand-authored CryptoPanic fixture
with a real recording.

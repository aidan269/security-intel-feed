# security-intel-feed

Small Python script that pulls a daily snapshot of security-relevant chatter
from Reddit and X (Twitter), filters by keyword and engagement, and emits a
single JSON blob you can pipe into anything else.

## What it watches

**Subreddits (top of the last 24h):**
`r/netsec`, `r/websec`, `r/ethdev`, `r/LocalLLaMA`, `r/programming`

**Keyword filter (case-insensitive, must match at least one):**
`AI security`, `LLM exploit`, `smart contract`, `vulnerability`, `CVE`,
`supply chain`

**Minimum upvotes:** 50

**Twitter/X hashtag filter (recent search, last 24h):**
`#aisecurity`, `#llmsecurity`, `#web3security`, `#smartcontractaudit`, `#CVE`

## Setup

No dependencies — Python 3.10+ stdlib only.

```bash
git clone https://github.com/<owner>/security-intel-feed.git
cd security-intel-feed
python3 fetch_security_intel.py > intel.json
```

The Reddit half works without auth. The Twitter half is skipped unless
`X_BEARER_TOKEN` is set:

```bash
export X_BEARER_TOKEN="your-x-api-v2-bearer-token"
python3 fetch_security_intel.py > intel.json
```

Twitter/X v2 `recent search` requires the **Basic** API tier or higher. The
free tier won't work. Get a token from the
[X Developer Portal](https://developer.x.com/) under your project → Keys and
tokens → Bearer Token.

See `.env.example` for the expected variable name.

## Customizing a single run

Two ways to add keywords/hashtags/subreddits without editing the file:

**Interactive (the default when running in a terminal).** The script shows
the current filter and lets you add comma-separated extras for that run:

```text
$ python3 fetch_security_intel.py > intel.json
Interactive mode — extend the defaults below, or press Enter to keep them.

Current keywords:
  AI security, LLM exploit, smart contract, vulnerability, CVE, supply chain
Add more keywords (comma-separated), or press Enter to skip: RCE, zero-day

Current hashtags (# optional):
  #aisecurity, #llmsecurity, #web3security, #smartcontractaudit, #CVE
Add more hashtags (comma-separated), or press Enter to skip:

Fetching...
```

**CLI flags (good for scripts / cron).** All repeatable:

```bash
python3 fetch_security_intel.py \
  -k "RCE" -k "zero-day" \
  -H "#0day" -H supplychainattack \
  -s blueteamsec \
  --min-upvotes 100 \
  --hours 48 \
  --no-prompt > intel.json
```

| Flag | Purpose |
| --- | --- |
| `-k, --keyword` | Add a keyword (repeatable) |
| `-H, --hashtag` | Add an X hashtag, `#` optional (repeatable) |
| `-s, --subreddit` | Add a subreddit, bare name (repeatable) |
| `--min-upvotes N` | Reddit score threshold |
| `--hours N` | Look-back window |
| `--no-prompt` | Skip interactive prompts (use for cron/CI) |

CLI and interactive additions both *extend* the defaults — they don't
replace them. To change defaults permanently, edit the constants at the
top of `fetch_security_intel.py` and commit.

## Output

A single JSON object written to stdout:

```json
{
  "generated_at": "2026-05-13T23:16:09Z",
  "window_hours": 24,
  "min_upvotes": 50,
  "count": 1,
  "items": [
    {
      "source": "reddit:r/programming",
      "title": "...",
      "engagement": 81,
      "url": "https://www.reddit.com/...",
      "note": "matched keywords: supply chain; comments=18"
    }
  ]
}
```

Items are sorted by engagement (Reddit score, or X likes+RTs+replies+quotes).

## Tuning

All the filters live as constants at the top of `fetch_security_intel.py`:
`SUBREDDITS`, `KEYWORDS`, `TWITTER_HASHTAGS`, `MIN_UPVOTES`, `WINDOW_HOURS`.
Edit and re-run.

## Notes

- Reddit calls are anonymous and rate-limited; the script sleeps 1s between
  subreddits to stay polite.
- Don't commit your bearer token. `.gitignore` already excludes `.env` and
  generated `intel.json` files.

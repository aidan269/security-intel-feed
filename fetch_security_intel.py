"""
Fetches top posts from selected subreddits and tweets matching security
hashtags, filters by keywords/engagement, and emits structured JSON.

Reddit: uses the public .json endpoints (no auth, rate-limited).
Twitter/X: requires a bearer token (paid API tier). Set X_BEARER_TOKEN
in the environment; otherwise the Twitter section is skipped.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


SUBREDDITS = ["netsec", "websec", "ethdev", "LocalLLaMA", "programming"]

KEYWORDS = [
    "AI security",
    "LLM exploit",
    "smart contract",
    "vulnerability",
    "CVE",
    "supply chain",
]

TWITTER_HASHTAGS = [
    "#aisecurity",
    "#llmsecurity",
    "#web3security",
    "#smartcontractaudit",
    "#CVE",
]

MIN_UPVOTES = 50
WINDOW_HOURS = 24
USER_AGENT = "security-intel-script/1.0 (by /u/anonymous)"

# Pre-compile a single case-insensitive regex for the keyword list.
KEYWORD_RE = re.compile(
    "|".join(re.escape(k) for k in KEYWORDS),
    flags=re.IGNORECASE,
)


@dataclass
class Item:
    source: str
    title: str
    engagement: int
    url: str
    note: str


def http_get_json(url: str, headers: dict | None = None) -> dict:
    req = Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def matched_keywords(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in KEYWORD_RE.finditer(text)})


def fetch_reddit(subreddit: str, cutoff: datetime) -> Iterable[Item]:
    # `t=day` keeps the listing to the last 24h; we still re-check timestamps
    # to be exact about the cutoff.
    url = f"https://www.reddit.com/r/{subreddit}/top.json?{urlencode({'t': 'day', 'limit': 100})}"
    try:
        payload = http_get_json(url)
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"[reddit:{subreddit}] fetch failed: {e}", file=sys.stderr)
        return

    for child in payload.get("data", {}).get("children", []):
        post = child.get("data", {})
        created = datetime.fromtimestamp(post.get("created_utc", 0), tz=timezone.utc)
        if created < cutoff:
            continue

        score = post.get("score", 0)
        if score <= MIN_UPVOTES:
            continue

        haystack = " ".join(
            [
                post.get("title", "") or "",
                post.get("selftext", "") or "",
                post.get("link_flair_text", "") or "",
            ]
        )
        hits = matched_keywords(haystack)
        if not hits:
            continue

        permalink = post.get("permalink", "")
        url = f"https://www.reddit.com{permalink}" if permalink else post.get("url", "")
        title = post.get("title", "").strip()
        snippet = (post.get("selftext", "") or "").strip().replace("\n", " ")
        if snippet:
            title = f"{title} — {snippet[:160]}"

        yield Item(
            source=f"reddit:r/{subreddit}",
            title=title[:280],
            engagement=score,
            url=url,
            note=f"matched keywords: {', '.join(hits)}; comments={post.get('num_comments', 0)}",
        )

    # Reddit asks for ~1 req/sec from anonymous clients.
    time.sleep(1)


def fetch_twitter(cutoff: datetime) -> Iterable[Item]:
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        print(
            "[twitter] X_BEARER_TOKEN not set — skipping. "
            "Twitter/X requires a paid API tier; export X_BEARER_TOKEN to enable.",
            file=sys.stderr,
        )
        return

    # Single OR query across all hashtags; exclude retweets to reduce dupes.
    query = "(" + " OR ".join(TWITTER_HASHTAGS) + ") -is:retweet lang:en"
    params = {
        "query": query,
        "start_time": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "max_results": 100,
        "tweet.fields": "public_metrics,created_at,author_id,entities",
        "expansions": "author_id",
        "user.fields": "username",
    }
    url = "https://api.twitter.com/2/tweets/search/recent?" + urlencode(params)

    try:
        payload = http_get_json(url, headers={"Authorization": f"Bearer {token}"})
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"[twitter] fetch failed: {e}", file=sys.stderr)
        return

    users = {u["id"]: u["username"] for u in payload.get("includes", {}).get("users", [])}

    for tweet in payload.get("data", []):
        metrics = tweet.get("public_metrics", {})
        engagement = (
            metrics.get("like_count", 0)
            + metrics.get("retweet_count", 0)
            + metrics.get("reply_count", 0)
            + metrics.get("quote_count", 0)
        )
        text = tweet.get("text", "")

        # Surface keyword matches if any; hashtag match alone still qualifies.
        hits = matched_keywords(text)
        tags = [
            "#" + tag["tag"].lower()
            for tag in tweet.get("entities", {}).get("hashtags", [])
        ]
        matched_tags = [t for t in TWITTER_HASHTAGS if t.lower() in tags]

        author = users.get(tweet.get("author_id", ""), "i")
        tweet_url = f"https://twitter.com/{author}/status/{tweet['id']}"

        note_bits = []
        if matched_tags:
            note_bits.append("hashtags: " + ", ".join(matched_tags))
        if hits:
            note_bits.append("keywords: " + ", ".join(hits))
        note_bits.append(
            f"likes={metrics.get('like_count', 0)} rts={metrics.get('retweet_count', 0)}"
        )

        yield Item(
            source="twitter",
            title=text.replace("\n", " ")[:280],
            engagement=engagement,
            url=tweet_url,
            note="; ".join(note_bits),
        )


def main() -> int:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=WINDOW_HOURS)
    results: list[Item] = []

    for sub in SUBREDDITS:
        results.extend(fetch_reddit(sub, cutoff))

    results.extend(fetch_twitter(cutoff))

    results.sort(key=lambda i: i.engagement, reverse=True)

    output = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "window_hours": WINDOW_HOURS,
        "min_upvotes": MIN_UPVOTES,
        "subreddits": SUBREDDITS,
        "keywords": KEYWORDS,
        "twitter_hashtags": TWITTER_HASHTAGS,
        "count": len(results),
        "items": [asdict(i) for i in results],
    }
    json.dump(output, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

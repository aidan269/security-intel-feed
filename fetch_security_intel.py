"""
Fetches top posts from selected subreddits and tweets matching security
hashtags, filters by keywords/engagement, and emits structured JSON.

Reddit: uses the public .json endpoints (no auth, rate-limited).
Twitter/X: requires a bearer token (paid API tier). Set X_BEARER_TOKEN
in the environment; otherwise the Twitter section is skipped.

Customizing a single run (without editing this file):

  # Interactive prompt (default when run in a terminal)
  python3 fetch_security_intel.py

  # Add keywords/hashtags/subreddits via flags (repeatable)
  python3 fetch_security_intel.py -k "zero-day" -k "RCE" -H "#0day" -s blueteamsec

  # Widen the window or raise the bar
  python3 fetch_security_intel.py --hours 48 --min-upvotes 100

  # Skip the prompt (useful for cron/CI)
  python3 fetch_security_intel.py --no-prompt > intel.json
"""

from __future__ import annotations

import argparse
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


# ---- Defaults (also editable directly) -------------------------------------

DEFAULT_SUBREDDITS = ["netsec", "websec", "ethdev", "LocalLLaMA", "programming"]

DEFAULT_KEYWORDS = [
    "AI security",
    "LLM exploit",
    "smart contract",
    "vulnerability",
    "CVE",
    "supply chain",
]

DEFAULT_HASHTAGS = [
    "#aisecurity",
    "#llmsecurity",
    "#web3security",
    "#smartcontractaudit",
    "#CVE",
]

DEFAULT_MIN_UPVOTES = 50
DEFAULT_WINDOW_HOURS = 24

USER_AGENT = "security-intel-script/1.0 (by /u/anonymous)"


# ---- Data model ------------------------------------------------------------


@dataclass
class Item:
    source: str
    title: str
    engagement: int
    url: str
    note: str


# ---- HTTP / parsing helpers ------------------------------------------------


def http_get_json(url: str, headers: dict | None = None) -> dict:
    req = Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_keyword_re(keywords: list[str]) -> re.Pattern[str] | None:
    if not keywords:
        return None
    return re.compile(
        "|".join(re.escape(k) for k in keywords),
        flags=re.IGNORECASE,
    )


def matched_keywords(text: str, keyword_re: re.Pattern[str] | None) -> list[str]:
    if keyword_re is None:
        return []
    return sorted({m.group(0).lower() for m in keyword_re.finditer(text)})


def normalize_hashtags(tags: Iterable[str]) -> list[str]:
    out: list[str] = []
    for t in tags:
        t = t.strip()
        if not t:
            continue
        if not t.startswith("#"):
            t = "#" + t
        out.append(t)
    return out


def split_csv(line: str) -> list[str]:
    return [s.strip() for s in line.split(",") if s.strip()]


def read_terms_file(path: str) -> list[str]:
    """One term per line, comma-separated, or any mix. Blank lines skipped."""
    terms: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            terms.extend(split_csv(line))
    return terms


# ---- Fetchers --------------------------------------------------------------


def fetch_reddit(
    subreddit: str,
    cutoff: datetime,
    keyword_re: re.Pattern[str] | None,
    min_upvotes: int,
) -> Iterable[Item]:
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
        if score <= min_upvotes:
            continue

        haystack = " ".join(
            [
                post.get("title", "") or "",
                post.get("selftext", "") or "",
                post.get("link_flair_text", "") or "",
            ]
        )
        hits = matched_keywords(haystack, keyword_re)
        if not hits:
            continue

        permalink = post.get("permalink", "")
        link = f"https://www.reddit.com{permalink}" if permalink else post.get("url", "")
        title = post.get("title", "").strip()
        snippet = (post.get("selftext", "") or "").strip().replace("\n", " ")
        if snippet:
            title = f"{title} — {snippet[:160]}"

        yield Item(
            source=f"reddit:r/{subreddit}",
            title=title[:280],
            engagement=score,
            url=link,
            note=f"matched keywords: {', '.join(hits)}; comments={post.get('num_comments', 0)}",
        )

    time.sleep(1)  # be polite to Reddit's anonymous endpoint


def fetch_twitter(
    cutoff: datetime,
    hashtags: list[str],
    keyword_re: re.Pattern[str] | None,
) -> Iterable[Item]:
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        print(
            "[twitter] X_BEARER_TOKEN not set — skipping. "
            "Twitter/X requires a paid API tier; export X_BEARER_TOKEN to enable.",
            file=sys.stderr,
        )
        return
    if not hashtags:
        print("[twitter] no hashtags configured — skipping.", file=sys.stderr)
        return

    query = "(" + " OR ".join(hashtags) + ") -is:retweet lang:en"
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

        hits = matched_keywords(text, keyword_re)
        tags = [
            "#" + tag["tag"].lower()
            for tag in tweet.get("entities", {}).get("hashtags", [])
        ]
        matched_tags = [t for t in hashtags if t.lower() in tags]

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


# ---- CLI -------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Pull security-relevant posts from Reddit + X.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-k", "--keyword", action="append", default=[], metavar="KW",
        help="Add a keyword to the filter (repeatable). Adds to defaults.",
    )
    p.add_argument(
        "--keywords-file", metavar="PATH",
        help=(
            "Read additional keywords from a file. One per line, or "
            "comma-separated, or a mix. Blank lines and '#' comments ignored."
        ),
    )
    p.add_argument(
        "--hashtags-file", metavar="PATH",
        help="Same format as --keywords-file, but for Twitter hashtags.",
    )
    p.add_argument(
        "-H", "--hashtag", action="append", default=[], metavar="TAG",
        help="Add a Twitter hashtag (repeatable). '#' is optional.",
    )
    p.add_argument(
        "-s", "--subreddit", action="append", default=[], metavar="SUB",
        help="Add a subreddit (repeatable). Bare name, no 'r/'.",
    )
    p.add_argument(
        "--min-upvotes", type=int, default=DEFAULT_MIN_UPVOTES,
        help=f"Reddit score threshold (default: {DEFAULT_MIN_UPVOTES}).",
    )
    p.add_argument(
        "--hours", type=int, default=DEFAULT_WINDOW_HOURS,
        help=f"Look-back window in hours (default: {DEFAULT_WINDOW_HOURS}).",
    )
    p.add_argument(
        "--no-prompt", action="store_true",
        help="Don't prompt interactively, even when stdin is a terminal.",
    )
    return p.parse_args(argv)


def prompt_extras(label: str, current: list[str]) -> list[str]:
    sys.stderr.write(f"\nCurrent {label}:\n  {', '.join(current) if current else '(none)'}\n")
    sys.stderr.write(f"Add more {label} (comma-separated), or press Enter to skip: ")
    sys.stderr.flush()
    try:
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        return []
    return split_csv(line or "")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    interactive = sys.stdin.isatty() and not args.no_prompt

    keywords = list(DEFAULT_KEYWORDS) + list(args.keyword)
    hashtags_raw = list(DEFAULT_HASHTAGS) + list(args.hashtag)
    subreddits = list(DEFAULT_SUBREDDITS) + list(args.subreddit)

    if args.keywords_file:
        keywords.extend(read_terms_file(args.keywords_file))
    if args.hashtags_file:
        hashtags_raw.extend(read_terms_file(args.hashtags_file))

    hashtags = normalize_hashtags(hashtags_raw)

    if interactive:
        sys.stderr.write(
            "Interactive mode — extend the defaults below, or press Enter to keep them.\n"
            "(Run with --no-prompt to skip these prompts.)\n"
        )
        keywords.extend(prompt_extras("keywords", keywords))
        hashtags.extend(normalize_hashtags(prompt_extras("hashtags (# optional)", hashtags)))
        sys.stderr.write("\nFetching...\n\n")

    # Dedupe while preserving order.
    keywords = list(dict.fromkeys(keywords))
    hashtags = list(dict.fromkeys(hashtags))
    subreddits = list(dict.fromkeys(subreddits))

    keyword_re = build_keyword_re(keywords)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=args.hours)
    results: list[Item] = []

    for sub in subreddits:
        results.extend(fetch_reddit(sub, cutoff, keyword_re, args.min_upvotes))

    results.extend(fetch_twitter(cutoff, hashtags, keyword_re))
    results.sort(key=lambda i: i.engagement, reverse=True)

    output = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "window_hours": args.hours,
        "min_upvotes": args.min_upvotes,
        "subreddits": subreddits,
        "keywords": keywords,
        "twitter_hashtags": hashtags,
        "count": len(results),
        "items": [asdict(i) for i in results],
    }
    json.dump(output, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

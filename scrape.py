#!/usr/bin/env python3
"""Fetch LinkedIn post data through Apify.

Usage:
    python scrape.py post <url> [--json [FILE]]
    python scrape.py comments <url> [--json [FILE]]

Reads APIFY_TOKEN from .env (next to this file) or the environment.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

API_BASE = "https://api.apify.com/v2"

# Input: {"urls": [<post url>]}
POST_ACTOR = "supreme_coder~linkedin-post"

# Input: {"postIds": [<post url or id>], "limit": <=100, "sortOrder": ...}
COMMENTS_ACTOR = "apimaestro~linkedin-post-comments-replies-engagements-scraper-no-cookies"
COMMENTS_LIMIT = 100  # actor schema maximum
COMMENTS_SORT = "most relevant"  # or "most recent" (actor default); relevant surfaces reply threads

# run-sync waits up to 300 s on Apify's side; give the HTTP call a little more.
HTTP_TIMEOUT = 330


class ScrapeError(Exception):
    """An error with a message fit to show the user as-is."""


# ---- Apify ---------------------------------------------------------------


def get_token() -> str:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    token = (os.getenv("APIFY_TOKEN") or "").strip()
    if not token:
        raise ScrapeError(
            "APIFY_TOKEN is missing. Open .env and set APIFY_TOKEN=<your token> "
            "(find it at https://console.apify.com/settings/integrations)."
        )
    return token


def run_actor(actor: str, actor_input: dict[str, Any], token: str) -> list[Any]:
    url = f"{API_BASE}/acts/{actor}/run-sync-get-dataset-items"
    try:
        r = requests.post(
            url,
            json=actor_input,
            # Header rather than ?token= so the token never lands in URLs or logs.
            headers={"Authorization": f"Bearer {token}"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.Timeout:
        raise ScrapeError(f"Timed out waiting for actor {actor} after {HTTP_TIMEOUT}s.")
    except requests.RequestException as e:
        raise ScrapeError(f"Could not reach Apify: {e}")

    if r.status_code >= 400:
        detail = _error_detail(r)
        if r.status_code == 401:
            raise ScrapeError(f"Apify rejected the token (401). Check APIFY_TOKEN in .env. {detail}")
        if r.status_code == 402:
            raise ScrapeError(f"Apify account is out of credit or lacks access (402). {detail}")
        if r.status_code == 404:
            raise ScrapeError(f"Actor {actor} not found (404). {detail}")
        if r.status_code == 408:
            raise ScrapeError(f"Actor {actor} did not finish within Apify's 300s sync limit (408). {detail}")
        raise ScrapeError(f"Actor {actor} failed (HTTP {r.status_code}). {detail}")

    try:
        data = r.json()
    except ValueError:
        raise ScrapeError(f"Actor {actor} returned a non-JSON response: {r.text[:300]}")
    if isinstance(data, dict) and "error" in data:
        raise ScrapeError(f"Actor {actor} failed: {_format_error(data['error'])}")
    if not isinstance(data, list):
        raise ScrapeError(f"Unexpected response from actor {actor}: {str(data)[:300]}")
    return data


def _error_detail(r: requests.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:300]
    if isinstance(body, dict) and "error" in body:
        return _format_error(body["error"])
    return str(body)[:300]


def _format_error(err: Any) -> str:
    if isinstance(err, dict):
        return " - ".join(str(err[k]) for k in ("type", "message") if err.get(k))
    return str(err)


# ---- Input validation ----------------------------------------------------


def validate_post_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not (
        host == "linkedin.com" or host.endswith(".linkedin.com")
    ):
        raise ScrapeError(f"Not a LinkedIn URL: {url!r}")
    path = parsed.path
    if not (
        path.startswith("/posts/")
        or path.startswith("/feed/update/")
        or path.startswith("/pulse/")
    ):
        raise ScrapeError(
            f"Not a LinkedIn post URL: {url!r}\n"
            "Expected https://www.linkedin.com/posts/... or "
            "https://www.linkedin.com/feed/update/urn:li:activity:..."
        )
    return url


# ---- Output helpers ------------------------------------------------------


def first(d: Any, *keys: str) -> Any:
    """First non-empty value among dotted key paths, e.g. first(d, "author.name", "authorName")."""
    for key in keys:
        cur = d
        for part in key.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, "", [], {}):
            return cur
    return None


def fmt_date(value: Any) -> str:
    if isinstance(value, dict):
        value = first(value, "date", "relative", "timestamp")
    if isinstance(value, (int, float)):  # epoch ms
        return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M")
    return str(value) if value else "unknown"


def save_json(data: Any, path: str | None, command: str) -> None:
    if not path:
        path = f"{command}_{datetime.now():%Y%m%d-%H%M%S}.json"
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved raw result to {path}")


def indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in (text or "").splitlines() or [""])


# ---- Commands ------------------------------------------------------------


def cmd_post(url: str, json_path: str | None, as_json: bool) -> None:
    token = get_token()
    items = run_actor(POST_ACTOR, {"urls": [url]}, token)
    posts = [it for it in items if isinstance(it, dict)]
    if not posts:
        raise ScrapeError(
            "The actor returned no data for this post. It may be private, deleted, "
            "or login-walled, or the actor may be failing to scrape it."
        )
    if as_json:
        save_json(items, json_path, "post")
        return

    p = posts[0]
    text = first(p, "text", "commentary", "content")
    author = first(p, "authorName", "author.name", "authorFullName")
    if not text and not author:
        raise ScrapeError("The actor returned a record with no post text or author (post may be unavailable).")

    print(f"Author:    {author or 'unknown'}")
    headline = first(p, "authorHeadline", "author.headline", "authorTitle")
    if headline:
        print(f"Headline:  {headline}")
    print(f"Date:      {fmt_date(first(p, 'postedAtISO', 'postedAt', 'postedAtTimestamp', 'date'))}")
    print(f"Reactions: {first(p, 'numLikes', 'numReactions', 'stats.total_reactions') or 0}")
    print(f"Comments:  {first(p, 'numComments', 'stats.comments') or 0}")
    shares = first(p, "numShares", "stats.shares")
    if shares is not None:
        print(f"Shares:    {shares}")
    print("-" * 60)
    print(text or "(no text)")


def cmd_comments(url: str, json_path: str | None, as_json: bool) -> None:
    token = get_token()
    items = run_actor(
        COMMENTS_ACTOR,
        {"postIds": [url], "limit": COMMENTS_LIMIT, "sortOrder": COMMENTS_SORT},
        token,
    )
    # The actor appends a {"summary": {...}} record (alone, when there are no comments).
    comments = [it for it in items if isinstance(it, dict) and "summary" not in it]
    if as_json:
        if not items:
            raise ScrapeError("The actor returned no data for this post.")
        save_json(items, json_path, "comments")
        return
    if not comments:
        raise ScrapeError(
            "No comments found. The post may have no comments, or be private, deleted, or login-walled."
        )

    total_replies = 0
    for i, c in enumerate(comments, 1):
        print(f"[{i}] {_comment_line(c)}")
        print(indent(first(c, "text", "comment", "commentary") or "(no text)", "    "))
        replies = c.get("replies") or []
        total_replies += len(replies)
        for r in replies:
            print(f"    ↳ {_comment_line(r)}")
            print(indent(first(r, "text", "comment", "commentary") or "(no text)", "      "))
        print()
    print(f"{len(comments)} comments, {total_replies} replies (sorted by {COMMENTS_SORT}, max {COMMENTS_LIMIT}).")


def _comment_line(c: dict[str, Any]) -> str:
    name = first(c, "author.name", "authorName", "author.full_name", "commenter.name") or "unknown"
    headline = first(c, "author.headline", "authorHeadline", "commenter.headline")
    date = first(c, "posted_at", "postedAt", "created_at", "date")
    reactions = first(c, "stats.total_reactions", "numReactions", "reactions_count")
    parts = [name]
    if headline:
        parts.append(f"({headline})")
    if date:
        parts.append(f"· {fmt_date(date)}")
    if reactions:
        parts.append(f"· {reactions} reactions")
    return " ".join(parts)


# ---- CLI -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch LinkedIn post data via Apify.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("post", "print post text, author, date and reaction/comment counts"),
        ("comments", "print comments with their replies nested"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("url", help="LinkedIn post URL")
        p.add_argument(
            "--json",
            nargs="?",
            const="",
            default=None,
            metavar="FILE",
            help="save the raw actor result to FILE (default: <command>_<timestamp>.json) instead of printing",
        )
    args = parser.parse_args(argv)

    try:
        url = validate_post_url(args.url)
        handler = cmd_post if args.command == "post" else cmd_comments
        handler(url, args.json or None, args.json is not None)
    except ScrapeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

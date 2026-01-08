#!/usr/bin/env python3
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List

import requests
import feedparser

CONFIG_FILE = Path("config.json")
STATE_FILE = Path("state.json")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def load_json(path: Path, default: Any) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def make_entry_key(entry: Dict[str, Any]) -> str:
    """
    Robust gegen Feeds ohne GUID:
    Wir bauen aus (id/guid/link/title/published) einen Hash.
    """
    parts = [
        str(entry.get("id", "")),
        str(entry.get("guid", "")),
        str(entry.get("link", "")),
        str(entry.get("title", "")),
        str(entry.get("published", "")),
        str(entry.get("updated", "")),
    ]
    raw = "||".join(parts).strip()
    if not raw:
        raw = json.dumps(entry, sort_keys=True, ensure_ascii=False)[:500]
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def passes_filters(title: str, summary: str, include_any: List[str], exclude_any: List[str]) -> bool:
    hay = f"{norm(title)} {norm(summary)}"

    # Excludes: wenn eines drin ist -> raus
    for kw in exclude_any or []:
        kw_n = norm(kw)
        if kw_n and kw_n in hay:
            return False

    # Includes: wenn leer -> alles ok; sonst muss mind. eins passen
    include_any = include_any or []
    if not include_any:
        return True

    return any((kw_n := norm(kw)) and kw_n in hay for kw in include_any)


def telegram_send(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt (GitHub Secrets prüfen).")

    url = TELEGRAM_API.format(token=token, method="sendMessage")
    resp = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    resp.raise_for_status()


def build_message(feed_name: str, title: str, link: str, published: str) -> str:
    bits = [f"📰 {feed_name}", title.strip()]
    if published:
        bits.append(f"🕒 {published}")
    if link:
        bits.append(link.strip())
    return "\n".join(bits).strip()


def format_published(entry: Dict[str, Any]) -> str:
    # feedparser liefert manchmal published_parsed / updated_parsed
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6]).strftime("%d.%m.%Y %H:%M")
            except Exception:
                pass
    # Fallback: string
    return str(entry.get("published", entry.get("updated", "")) or "").strip()


def main() -> int:
    config = load_json(CONFIG_FILE, default={"feeds": []})
    state = load_json(STATE_FILE, default={"seen": {}})  # seen[url] = [hashes]

    feeds = config.get("feeds", [])
    if not feeds:
        print("Keine Feeds in config.json gefunden.")
        return 1

    seen_map: Dict[str, List[str]] = state.get("seen", {})
    changed = False
    sent = 0

    for f in feeds:
        name = f.get("name", "Feed")
        url = f.get("url", "").strip()
        include_any = f.get("include_any", [])
        exclude_any = f.get("exclude_any", [])
        max_items = int(f.get("max_items", 30))

        if not url:
            continue

        parsed = feedparser.parse(url)
        if getattr(parsed, "bozo", 0):
            # Feed kaputt/temporär problematisch -> überspringen
            print(f"Warnung: Feed-Parsing-Problem bei {url}")
            continue

        entries = getattr(parsed, "entries", [])[:max_items]
        seen_hashes = set(seen_map.get(url, []))
        new_hashes: List[str] = []

        for e in entries:
            e_dict = dict(e)
            key = make_entry_key(e_dict)
            if key in seen_hashes:
                continue

            title = str(e_dict.get("title", "") or "").strip()
            summary = str(e_dict.get("summary", e_dict.get("description", "")) or "").strip()
            link = str(e_dict.get("link", "") or "").strip()
            published = format_published(e_dict)

            # Filter anwenden
            if passes_filters(title, summary, include_any, exclude_any):
                msg = build_message(name, title or "(ohne Titel)", link, published)
                telegram_send(msg)
                sent += 1

            # Egal ob gefiltert oder gesendet: als "gesehen" markieren (sonst nervt es ständig)
            new_hashes.append(key)
            seen_hashes.add(key)

        if new_hashes:
            # Speicher begrenzen
            trimmed = list(seen_hashes)[-400:]
            seen_map[url] = trimmed
            changed = True

    if changed:
        state["seen"] = seen_map
        save_json(STATE_FILE, state)

    print(f"Done. Sent {sent} messages.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise

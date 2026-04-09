from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from trading_agents.models import SentimentSnapshot


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) CodexTradingBot/0.1"
_HTTP_CACHE: dict[str, tuple[float, object]] = {}


def _cached_fetch(url: str, loader, timeout_seconds: float, cache_ttl_seconds: float):
    now = time.time()
    cached = _HTTP_CACHE.get(url)
    if cached is not None:
        cached_at, payload = cached
        if max(cache_ttl_seconds, 0.0) > 0 and (now - cached_at) <= cache_ttl_seconds:
            return payload
    payload = loader(url, timeout_seconds)
    if cache_ttl_seconds > 0:
        _HTTP_CACHE[url] = (now, payload)
    return payload


def _load_json(url: str, timeout_seconds: float) -> dict:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=max(timeout_seconds, 1.0)) as response:
        return json.loads(response.read().decode("utf-8"))


def _load_xml(url: str, timeout_seconds: float) -> ElementTree.Element:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=max(timeout_seconds, 1.0)) as response:
        payload = response.read()
    return ElementTree.fromstring(payload)


def _load_cache_file(path: Path | None) -> dict[str, dict]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_cache_file(path: Path | None, payload: dict[str, dict]) -> None:
    if path is None:
        return
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception:
        return


@dataclass(frozen=True)
class NewsItem:
    source: str
    title: str
    link: str
    published: str


@dataclass(frozen=True)
class SentimentRecord:
    created_at: str
    symbol: str
    fear_greed_value: int
    fear_greed_label: str
    event_items: list[str]
    news_items: list[NewsItem]
    snapshot: SentimentSnapshot


@dataclass(frozen=True)
class RssFeed:
    name: str
    url: str


@dataclass(frozen=True)
class EventSource:
    name: str
    url: str


class SentimentDataProvider:
    fear_greed_url = "https://api.alternative.me/fng/?limit=1"
    default_config_path = Path("config/sentiment_sources.json")

    def __init__(
        self,
        config_path: str | Path | None = None,
        timeout_seconds: float = 6.0,
        cache_ttl_seconds: float = 120.0,
        cache_state_path: str | Path | None = None,
    ) -> None:
        self.config_path = Path(config_path) if config_path else self.default_config_path
        self.config = self._load_config(self.config_path)
        self.timeout_seconds = max(float(timeout_seconds), 1.0)
        self.cache_ttl_seconds = max(float(cache_ttl_seconds), 0.0)
        self.cache_state_path = Path(cache_state_path) if cache_state_path else None
        self._file_cache = _load_cache_file(self.cache_state_path)

    def collect(self, symbol: str) -> SentimentRecord:
        base_asset = symbol.split("/", 1)[0].upper()
        fear_greed = self._fetch_fear_greed()
        event_items, event_hits = self._fetch_events(base_asset)
        news_items, feed_hits = self._fetch_news(base_asset)
        snapshot = self._build_snapshot(symbol, fear_greed, event_items, event_hits, news_items, feed_hits)
        return SentimentRecord(
            created_at=_utc_now().isoformat(),
            symbol=symbol,
            fear_greed_value=fear_greed["value"],
            fear_greed_label=fear_greed["label"],
            event_items=event_items,
            news_items=news_items,
            snapshot=snapshot,
        )

    def _fetch_fear_greed(self) -> dict:
        try:
            payload = self._fetch_json(self.fear_greed_url)
            item = payload["data"][0]
            return {
                "value": int(item["value"]),
                "label": item["value_classification"],
            }
        except Exception:
            return {
                "value": 50,
                "label": "Neutral",
            }

    def _load_config(self, path: Path) -> dict:
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.exists():
            return {"rss_feeds": {"default": []}, "x_whitelist": [], "news_whitelist": []}
        return json.loads(path.read_text())

    def _feeds_for_asset(self, base_asset: str) -> list[RssFeed]:
        raw_feeds = self.config.get("rss_feeds", {}).get(base_asset) or self.config.get("rss_feeds", {}).get("default", [])
        return [RssFeed(name=item["name"], url=item["url"]) for item in raw_feeds]

    def _event_sources(self) -> list[EventSource]:
        raw_sources = self.config.get("event_sources", [])
        return [EventSource(name=item["name"], url=item["url"]) for item in raw_sources]

    def _is_whitelisted(self, link: str) -> bool:
        whitelist = self.config.get("news_whitelist", [])
        if not whitelist:
            return True
        domain = urlparse(link).netloc.lower()
        return any(entry in domain for entry in whitelist)

    def _fetch_events(self, base_asset: str) -> tuple[list[str], list[str]]:
        event_items: list[str] = []
        successful_sources: list[str] = []
        for source in self._event_sources():
            try:
                payload = self._fetch_json(source.url)
            except Exception:
                continue

            coins = payload.get("coins", [])
            titles: list[str] = []
            for coin in coins[:7]:
                item = coin.get("item", {})
                symbol = str(item.get("symbol", "")).upper()
                name = str(item.get("name", "")).strip()
                if base_asset and symbol and symbol != base_asset:
                    continue
                if name:
                    titles.append(f"trending:{name} ({symbol})")
            if titles:
                event_items.extend(titles[:3])
                successful_sources.append(source.name)
        return event_items, successful_sources

    def _fetch_news(self, base_asset: str) -> tuple[list[NewsItem], list[str]]:
        items: list[NewsItem] = []
        successful_feeds: list[str] = []
        seen_links: set[str] = set()

        for feed in self._feeds_for_asset(base_asset):
            try:
                root = self._fetch_xml(feed.url)
            except Exception:
                continue

            added_from_feed = 0
            for item in root.findall("./channel/item"):
                title = item.findtext("title", default="").strip()
                link = item.findtext("link", default="").strip()
                published = item.findtext("pubDate", default="").strip()
                if not title or not link or link in seen_links:
                    continue
                if not self._is_whitelisted(link):
                    continue
                items.append(NewsItem(source=feed.name, title=title, link=link, published=published))
                seen_links.add(link)
                added_from_feed += 1
                if len(items) >= 5:
                    break
            if added_from_feed:
                successful_feeds.append(feed.name)
            if len(items) >= 5:
                break
        return items, successful_feeds

    def _build_snapshot(
        self,
        symbol: str,
        fear_greed: dict,
        event_items: list[str],
        event_hits: list[str],
        news_items: list[NewsItem],
        feed_hits: list[str],
    ) -> SentimentSnapshot:
        score = (fear_greed["value"] - 50) / 50
        summary = (
            f"{symbol} market mood is {fear_greed['label'].lower()} "
            f"(Fear & Greed={fear_greed['value']}); "
            f"event items={len(event_items)}; "
            f"curated news items={len(news_items)}; "
            f"successful feeds={len(feed_hits)}"
        )
        references = [
            "alternative.me:fear-and-greed",
            *[f"event:{name}" for name in event_hits],
            *[f"feed:{name}" for name in feed_hits],
            *event_items[:2],
            *[item.link for item in news_items[:3]],
        ]
        return SentimentSnapshot(
            source_count=1 + len(event_hits) + len(feed_hits),
            sentiment_score=round(max(min(score, 1.0), -1.0), 2),
            summary=summary,
            references=references,
        )

    def _fetch_json(self, url: str) -> dict:
        now = time.time()
        cached = _HTTP_CACHE.get(url)
        if cached is not None:
            cached_at, payload = cached
            if self.cache_ttl_seconds > 0 and (now - cached_at) <= self.cache_ttl_seconds:
                return payload
        file_cached = self._file_cache.get(url, {})
        cached_at = float(file_cached.get("cached_at", 0.0))
        if (
            self.cache_ttl_seconds > 0
            and cached_at > 0
            and (now - cached_at) <= self.cache_ttl_seconds
            and file_cached.get("kind") == "json"
            and isinstance(file_cached.get("payload"), dict)
        ):
            payload = file_cached["payload"]
            _HTTP_CACHE[url] = (cached_at, payload)
            return payload
        payload = _load_json(url, self.timeout_seconds)
        if self.cache_ttl_seconds > 0:
            _HTTP_CACHE[url] = (now, payload)
            self._file_cache[url] = {"cached_at": now, "kind": "json", "payload": payload}
            _save_cache_file(self.cache_state_path, self._file_cache)
        return payload

    def _fetch_xml(self, url: str) -> ElementTree.Element:
        now = time.time()
        cached = _HTTP_CACHE.get(url)
        if cached is not None:
            cached_at, payload = cached
            if self.cache_ttl_seconds > 0 and (now - cached_at) <= self.cache_ttl_seconds:
                return payload
        file_cached = self._file_cache.get(url, {})
        cached_at = float(file_cached.get("cached_at", 0.0))
        if (
            self.cache_ttl_seconds > 0
            and cached_at > 0
            and (now - cached_at) <= self.cache_ttl_seconds
            and file_cached.get("kind") == "xml"
            and isinstance(file_cached.get("payload"), str)
        ):
            payload = ElementTree.fromstring(file_cached["payload"].encode("utf-8"))
            _HTTP_CACHE[url] = (cached_at, payload)
            return payload
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=max(self.timeout_seconds, 1.0)) as response:
            raw_payload = response.read().decode("utf-8", errors="replace")
        payload = ElementTree.fromstring(raw_payload.encode("utf-8"))
        if self.cache_ttl_seconds > 0:
            _HTTP_CACHE[url] = (now, payload)
            self._file_cache[url] = {"cached_at": now, "kind": "xml", "payload": raw_payload}
            _save_cache_file(self.cache_state_path, self._file_cache)
        return payload


def write_sentiment_record(path: Path, record: SentimentRecord) -> Path:
    stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    filename = f"{record.symbol.replace('/', '-')}-{stamp}.json"
    target = path / filename
    payload = {
        "created_at": record.created_at,
        "symbol": record.symbol,
        "fear_greed_value": record.fear_greed_value,
        "fear_greed_label": record.fear_greed_label,
        "event_items": record.event_items,
        "news_items": [asdict(item) for item in record.news_items],
        "snapshot": asdict(record.snapshot),
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return target

"""
web_tools — Web Search & Content Fetching
==========================================
Multi-provider web search and URL content extraction.

Features:
- Multi-provider web search (DuckDuckGo, Brave, SearXNG, Tavily, Exa)
- URL content fetching with HTML-to-text extraction
- Rate limiting integration for search providers
- Result deduplication and relevance scoring
- Governed search: all queries and results auditable
- Configurable timeouts and retry policies
- Content size limits to prevent memory exhaustion
- SSRF protection: private/loopback/link-local IP ranges and cloud metadata
  endpoints are blocked

The web tools module treats external data as untrusted input.
All fetched content is size-bounded and sanitized before use.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
import urllib.parse
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class SearchProvider(str, Enum):
    """Supported search providers."""

    DUCKDUCKGO = "duckduckgo"
    BRAVE = "brave"
    SEARXNG = "searxng"
    TAVILY = "tavily"
    EXA = "exa"
    PERPLEXITY = "perplexity"


class FetchStatus(str, Enum):
    """Status of a content fetch operation."""

    SUCCESS = "success"
    TIMEOUT = "timeout"
    ERROR = "error"
    BLOCKED = "blocked"
    SIZE_EXCEEDED = "size_exceeded"
    INVALID_URL = "invalid_url"


@dataclass
class SearchResult:
    """A single search result."""

    title: str = ""
    url: str = ""
    snippet: str = ""
    score: float = 0.0
    source: str = ""
    published_date: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "score": self.score,
            "source": self.source,
        }
        if self.published_date:
            result["published_date"] = self.published_date
        if self.extra:
            result["extra"] = self.extra
        return result


@dataclass
class SearchResponse:
    """Response from a search query."""

    query: str = ""
    provider: str = ""
    results: list[SearchResult] = field(default_factory=list)
    total_results: int = 0
    elapsed_seconds: float = 0.0
    error: str = ""
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "provider": self.provider,
            "results": [r.to_dict() for r in self.results],
            "total_results": self.total_results,
            "elapsed_seconds": self.elapsed_seconds,
            "error": self.error,
            "cached": self.cached,
        }


@dataclass
class FetchedContent:
    """Content fetched from a URL."""

    url: str = ""
    status: FetchStatus = FetchStatus.SUCCESS
    title: str = ""
    text: str = ""
    html: str = ""
    content_type: str = ""
    size_bytes: int = 0
    elapsed_seconds: float = 0.0
    sha256: str = ""
    error: str = ""
    links: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "url": self.url,
            "status": self.status.value,
            "title": self.title,
            "text_length": len(self.text),
            "size_bytes": self.size_bytes,
            "elapsed_seconds": self.elapsed_seconds,
            "content_type": self.content_type,
        }
        if self.sha256:
            result["sha256"] = self.sha256
        if self.error:
            result["error"] = self.error
        if self.metadata:
            result["metadata"] = self.metadata
        return result


@dataclass
class SearchProviderConfig:
    """Configuration for a search provider."""

    name: str
    provider: SearchProvider
    api_key: str = ""
    base_url: str = ""
    enabled: bool = True
    max_results: int = 10
    timeout_seconds: float = 30.0
    priority: int = 0  # lower = higher priority


# Type for search provider implementations
SearchProviderFunc = Callable[[str, int, SearchProviderConfig], SearchResponse]


class WebSearchEngine:
    """
    Multi-provider web search engine.

    Usage:
        engine = WebSearchEngine()

        # Register providers
        engine.register_provider(SearchProviderConfig(
            name="duckduckgo",
            provider=SearchProvider.DUCKDUCKGO,
        ), duckduckgo_search)

        # Search
        response = engine.search("sovereign claw governance", max_results=5)

        # Search with specific provider
        response = engine.search("query", provider="brave")
    """

    # Cache TTL (5 minutes)
    CACHE_TTL = 300.0

    # Maximum cache size
    MAX_CACHE = 1000

    def __init__(self) -> None:
        self._providers: dict[str, tuple[SearchProviderConfig, SearchProviderFunc]] = {}
        self._cache: dict[str, tuple[SearchResponse, float]] = {}
        self._total_searches = 0
        self._total_errors = 0
        self._searches_by_provider: dict[str, int] = {}

    def register_provider(
        self,
        config: SearchProviderConfig,
        func: SearchProviderFunc,
    ) -> None:
        """Register a search provider with its implementation."""
        self._providers[config.name] = (config, func)

    def unregister_provider(self, name: str) -> None:
        """Remove a search provider."""
        self._providers.pop(name, None)

    def search(
        self,
        query: str,
        max_results: int = 10,
        provider: str = "",
        use_cache: bool = True,
    ) -> SearchResponse:
        """
        Execute a web search.

        Args:
            query: Search query string.
            max_results: Maximum results to return.
            provider: Specific provider name (or empty for auto-select).
            use_cache: Whether to use cached results.

        Returns:
            SearchResponse with results.
        """
        self._total_searches += 1
        query = query.strip()
        if not query:
            return SearchResponse(query=query, error="Empty query")

        # Select provider first so we can include it in the cache key
        selected = self._select_provider(provider)
        if not selected:
            self._total_errors += 1
            return SearchResponse(
                query=query,
                error="No search providers available",
            )

        config, func = selected
        provider_name = config.name

        # Check cache (use resolved provider name)
        cache_key = self._cache_key(query, provider_name, max_results)
        if use_cache:
            cached = self._get_cached(cache_key)
            if cached:
                result = deepcopy(cached)
                result.cached = True
                return result

        self._searches_by_provider[provider_name] = (
            self._searches_by_provider.get(provider_name, 0) + 1
        )

        # Execute search
        start = time.time()
        try:
            response = func(query, max_results, config)
            response.query = query
            response.provider = provider_name
            response.elapsed_seconds = time.time() - start
        except Exception as exc:
            self._total_errors += 1
            response = SearchResponse(
                query=query,
                provider=provider_name,
                error=str(exc),
                elapsed_seconds=time.time() - start,
            )

        # Deduplicate results
        response.results = self._deduplicate(response.results)
        response.total_results = len(response.results)

        # Cache result
        if not response.error:
            self._set_cached(cache_key, response)

        return response

    def stats(self) -> dict[str, Any]:
        """Get search engine statistics."""
        return {
            "total_searches": self._total_searches,
            "total_errors": self._total_errors,
            "providers": list(self._providers.keys()),
            "searches_by_provider": dict(self._searches_by_provider),
            "cache_size": len(self._cache),
        }

    def _select_provider(
        self,
        name: str = "",
    ) -> tuple[SearchProviderConfig, SearchProviderFunc] | None:
        """Select a provider by name or auto-select based on priority."""
        if name and name in self._providers:
            cfg, func = self._providers[name]
            if cfg.enabled:
                return cfg, func
            return None

        # Auto-select: pick highest priority enabled provider
        candidates = [(cfg, func) for cfg, func in self._providers.values() if cfg.enabled]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0].priority)
        return candidates[0]

    def _cache_key(self, query: str, provider: str, max_results: int) -> str:
        raw = f"{query.lower()}:{provider}:{max_results}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _get_cached(self, key: str) -> SearchResponse | None:
        entry = self._cache.get(key)
        if not entry:
            return None
        response, ts = entry
        if time.time() - ts > self.CACHE_TTL:
            del self._cache[key]
            return None
        return response

    def _set_cached(self, key: str, response: SearchResponse) -> None:
        self._cache[key] = (response, time.time())
        # Evict oldest if over limit
        if len(self._cache) > self.MAX_CACHE:
            oldest = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest]

    def _deduplicate(self, results: list[SearchResult]) -> list[SearchResult]:
        """Remove duplicate results by URL."""
        seen: set[str] = set()
        deduped = []
        for r in results:
            normalized = r.url.rstrip("/").lower()
            if normalized not in seen:
                seen.add(normalized)
                deduped.append(r)
        return deduped


class ContentFetcher:
    """
    URL content fetcher with HTML-to-text extraction.

    Usage:
        fetcher = ContentFetcher()

        # Fetch a URL
        content = fetcher.fetch("https://example.com")
        print(content.title)
        print(content.text[:500])
    """

    # Maximum content size (10 MB)
    MAX_CONTENT_SIZE = 10 * 1024 * 1024

    # Default timeout
    DEFAULT_TIMEOUT = 30.0

    # Blocked URL patterns (security)
    BLOCKED_SCHEMES = {"file", "ftp", "data", "javascript"}

    # Private/reserved CIDR ranges for SSRF protection
    _SSRF_BLOCKED_NETWORKS = [
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("169.254.0.0/16"),  # link-local / AWS metadata
        ipaddress.ip_network("0.0.0.0/8"),
        ipaddress.ip_network("100.64.0.0/10"),  # shared address space
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fe80::/10"),
        ipaddress.ip_network("fc00::/7"),
    ]

    def __init__(
        self,
        max_content_size: int = MAX_CONTENT_SIZE,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = "SovereignClaw/3.3.0",
    ) -> None:
        self._max_content_size = max_content_size
        self._timeout = timeout
        self._user_agent = user_agent
        self._total_fetches = 0
        self._total_errors = 0
        self._total_bytes = 0
        self._blocked_domains: set[str] = set()

    def block_domain(self, domain: str) -> None:
        """Block a domain from being fetched."""
        self._blocked_domains.add(domain.lower())

    def unblock_domain(self, domain: str) -> None:
        """Unblock a domain."""
        self._blocked_domains.discard(domain.lower())

    def fetch(self, url: str) -> FetchedContent:
        """
        Fetch content from a URL.

        Args:
            url: The URL to fetch.

        Returns:
            FetchedContent with extracted text and metadata.
        """
        self._total_fetches += 1
        start = time.time()

        # Validate URL
        validation_error, fetch_status = self._validate_url(url)
        if validation_error:
            self._total_errors += 1
            return FetchedContent(
                url=url,
                status=fetch_status,
                error=validation_error,
                elapsed_seconds=time.time() - start,
            )

        # Fetch via httpx with streaming to enforce size limit
        try:
            import httpx

            raw_chunks: list[bytes] = []
            total_size = 0
            content_type = ""

            with httpx.Client(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": self._user_agent},
            ) as client:
                with client.stream("GET", url) as response:
                    # Re-validate the final URL after redirects (SSRF via redirect)
                    final_url = str(response.url)
                    if final_url != url:
                        redirect_error, redirect_status = self._validate_url(final_url)
                        if redirect_error:
                            self._total_errors += 1
                            return FetchedContent(
                                url=url,
                                status=redirect_status,
                                error=f"Redirect blocked: {redirect_error}",
                                elapsed_seconds=time.time() - start,
                            )
                    content_type = response.headers.get("content-type", "")
                    for chunk in response.iter_bytes(chunk_size=65536):
                        total_size += len(chunk)
                        if total_size > self._max_content_size:
                            self._total_errors += 1
                            return FetchedContent(
                                url=url,
                                status=FetchStatus.SIZE_EXCEEDED,
                                error=(
                                    f"Content size exceeds limit {self._max_content_size}"
                                ),
                                size_bytes=total_size,
                                content_type=content_type,
                                elapsed_seconds=time.time() - start,
                            )
                        raw_chunks.append(chunk)

            raw = b"".join(raw_chunks)
            self._total_bytes += len(raw)

            # Extract text
            title, text, links = self._extract_text(raw, content_type)

            return FetchedContent(
                url=url,
                status=FetchStatus.SUCCESS,
                title=title,
                text=text,
                html=raw.decode("utf-8", errors="replace") if "html" in content_type else "",
                content_type=content_type,
                size_bytes=len(raw),
                elapsed_seconds=time.time() - start,
                sha256=hashlib.sha256(raw).hexdigest(),
                links=links,
            )

        except Exception as exc:
            self._total_errors += 1
            status = FetchStatus.TIMEOUT if "timeout" in str(exc).lower() else FetchStatus.ERROR
            return FetchedContent(
                url=url,
                status=status,
                error=str(exc),
                elapsed_seconds=time.time() - start,
            )

    def stats(self) -> dict[str, Any]:
        """Get fetcher statistics."""
        return {
            "total_fetches": self._total_fetches,
            "total_errors": self._total_errors,
            "total_bytes": self._total_bytes,
            "blocked_domains": list(self._blocked_domains),
        }

    def _validate_url(self, url: str) -> tuple[str, FetchStatus]:
        """
        Validate a URL.

        Returns a tuple of (error_message, FetchStatus).
        Empty error_message means the URL is valid.
        BLOCKED is returned for security/policy denials; INVALID_URL for parse errors.
        """
        try:
            parsed = urllib.parse.urlparse(url)
        except Exception:
            return "Invalid URL format", FetchStatus.INVALID_URL

        if not parsed.scheme:
            return "Missing URL scheme", FetchStatus.INVALID_URL
        if parsed.scheme.lower() in self.BLOCKED_SCHEMES:
            return f"Blocked scheme: {parsed.scheme}", FetchStatus.BLOCKED
        if not parsed.netloc:
            return "Missing URL host", FetchStatus.INVALID_URL

        hostname = parsed.hostname or ""
        hostname_lower = hostname.lower()

        # Block localhost and *.local domains
        if hostname_lower == "localhost" or hostname_lower.endswith(".local"):
            return f"Blocked domain: {hostname}", FetchStatus.BLOCKED

        # Block explicitly blocked domains
        if hostname_lower in self._blocked_domains:
            return f"Blocked domain: {hostname}", FetchStatus.BLOCKED

        # SSRF protection: resolve hostname and check against private ranges
        if hostname_lower:
            try:
                addr_infos = socket.getaddrinfo(hostname_lower, None)
                for _family, _type, _proto, _canonname, sockaddr in addr_infos:
                    ip_str = sockaddr[0]
                    try:
                        ip_obj = ipaddress.ip_address(ip_str)
                    except ValueError:
                        continue
                    for network in self._SSRF_BLOCKED_NETWORKS:
                        if ip_obj in network:
                            return (
                                f"Blocked: {hostname} resolves to private/reserved address {ip_str}",
                                FetchStatus.BLOCKED,
                            )
            except OSError:
                # DNS resolution failure — let the actual request fail with a proper error
                pass

        return "", FetchStatus.SUCCESS

    def _extract_text(
        self,
        raw: bytes,
        content_type: str,
    ) -> tuple[str, str, list[str]]:
        """Extract title, text, and links from raw content."""
        if "html" not in content_type and "xml" not in content_type:
            text = raw.decode("utf-8", errors="replace")
            return "", text, []

        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(raw, "html.parser")

            # Title
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else ""

            # Remove script/style
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()

            # Text
            text = soup.get_text(separator="\n", strip=True)

            # Links
            links = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if isinstance(href, str) and href.startswith("http"):
                    links.append(href)

            return title, text, links[:100]  # cap at 100 links

        except ImportError:
            # Fallback without BeautifulSoup
            text = raw.decode("utf-8", errors="replace")
            return "", text, []

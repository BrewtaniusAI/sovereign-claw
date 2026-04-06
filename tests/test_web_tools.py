"""Tests for sovereign_claw.web_tools."""

from __future__ import annotations

from sovereign_claw.web_tools import (
    ContentFetcher,
    FetchedContent,
    FetchStatus,
    SearchProvider,
    SearchProviderConfig,
    SearchResponse,
    SearchResult,
    WebSearchEngine,
)


# ── SearchResult ─────────────────────────────────────────────────────────────


class TestSearchResult:
    def test_creation(self) -> None:
        r = SearchResult(
            title="Test",
            url="https://example.com",
            snippet="A test result",
        )
        assert r.title == "Test"
        assert r.url == "https://example.com"

    def test_to_dict(self) -> None:
        r = SearchResult(
            title="Test",
            url="https://example.com",
            snippet="Snippet",
            source="duckduckgo",
        )
        d = r.to_dict()
        assert d["title"] == "Test"
        assert d["source"] == "duckduckgo"

    def test_score_default(self) -> None:
        r = SearchResult(title="T", url="https://t.com", snippet="s")
        assert r.score == 0.0


# ── SearchResponse ───────────────────────────────────────────────────────────


class TestSearchResponse:
    def test_creation(self) -> None:
        resp = SearchResponse(
            query="test query",
            results=[
                SearchResult(title="R1", url="https://r1.com", snippet="r1"),
            ],
            provider="brave",
        )
        assert resp.query == "test query"
        assert len(resp.results) == 1

    def test_to_dict(self) -> None:
        resp = SearchResponse(
            query="test",
            results=[],
            provider="tavily",
        )
        d = resp.to_dict()
        assert d["query"] == "test"
        assert d["total_results"] == 0

    def test_cached_default(self) -> None:
        resp = SearchResponse(query="q")
        assert resp.cached is False


# ── FetchedContent ───────────────────────────────────────────────────────────


class TestFetchedContent:
    def test_success(self) -> None:
        fc = FetchedContent(
            url="https://example.com",
            status=FetchStatus.SUCCESS,
            title="Example",
            text="Hello world",
            content_type="text/html",
        )
        assert fc.status == FetchStatus.SUCCESS
        assert fc.title == "Example"

    def test_error(self) -> None:
        fc = FetchedContent(
            url="https://blocked.com",
            status=FetchStatus.BLOCKED,
            error="Domain blocked",
        )
        assert fc.status == FetchStatus.BLOCKED

    def test_to_dict(self) -> None:
        fc = FetchedContent(
            url="https://example.com",
            status=FetchStatus.SUCCESS,
            text="content",
        )
        d = fc.to_dict()
        assert d["url"] == "https://example.com"
        assert d["status"] == "success"

    def test_size_exceeded(self) -> None:
        fc = FetchedContent(
            url="https://big.com",
            status=FetchStatus.SIZE_EXCEEDED,
            error="Too large",
        )
        assert fc.status == FetchStatus.SIZE_EXCEEDED


# ── WebSearchEngine ──────────────────────────────────────────────────────────


class TestWebSearchEngine:
    def _make_engine(self) -> WebSearchEngine:
        engine = WebSearchEngine()

        def mock_search(
            query: str, max_results: int, config: SearchProviderConfig
        ) -> SearchResponse:
            return SearchResponse(
                query=query,
                provider=config.name,
                results=[
                    SearchResult(
                        title=f"Result for: {query}",
                        url=f"https://example.com/{query.replace(' ', '-')}",
                        snippet=f"Snippet about {query}",
                        source=config.name,
                    )
                ],
                total_results=1,
            )

        config = SearchProviderConfig(
            name="duckduckgo",
            provider=SearchProvider.DUCKDUCKGO,
            enabled=True,
            priority=1,
        )
        engine.register_provider(config, mock_search)
        return engine

    def test_register_provider(self) -> None:
        engine = self._make_engine()
        stats = engine.stats()
        assert len(stats["providers"]) >= 1

    def test_search_basic(self) -> None:
        engine = self._make_engine()
        resp = engine.search("test query")
        assert resp.query == "test query"
        assert len(resp.results) >= 1
        assert "test query" in resp.results[0].title

    def test_search_caching(self) -> None:
        engine = self._make_engine()
        resp1 = engine.search("cached query")
        resp2 = engine.search("cached query", use_cache=True)
        assert resp1.query == resp2.query
        assert resp2.cached

    def test_search_no_cache(self) -> None:
        engine = self._make_engine()
        engine.search("query1")
        resp = engine.search("query1", use_cache=False)
        assert not resp.cached

    def test_search_max_results(self) -> None:
        engine = self._make_engine()
        resp = engine.search("test", max_results=5)
        assert len(resp.results) <= 5

    def test_search_specific_provider(self) -> None:
        engine = self._make_engine()
        resp = engine.search("test", provider="duckduckgo")
        assert resp.provider == "duckduckgo"

    def test_search_no_provider(self) -> None:
        engine = WebSearchEngine()
        resp = engine.search("test")
        assert resp.error != ""
        assert "No search providers" in resp.error

    def test_search_empty_query(self) -> None:
        engine = self._make_engine()
        resp = engine.search("")
        assert resp.error != ""
        assert "Empty query" in resp.error

    def test_deduplication(self) -> None:
        engine = WebSearchEngine()

        def dup_search(
            query: str, max_results: int, config: SearchProviderConfig
        ) -> SearchResponse:
            return SearchResponse(
                query=query,
                provider=config.name,
                results=[
                    SearchResult(title="Same", url="https://same.com", snippet="s"),
                    SearchResult(title="Same2", url="https://same.com", snippet="s"),
                ],
                total_results=2,
            )

        config = SearchProviderConfig(
            name="brave",
            provider=SearchProvider.BRAVE,
            enabled=True,
            priority=1,
        )
        engine.register_provider(config, dup_search)
        resp = engine.search("dedup test")
        urls = [r.url for r in resp.results]
        assert len(urls) == len(set(urls))

    def test_stats(self) -> None:
        engine = self._make_engine()
        engine.search("q1")
        engine.search("q2")
        stats = engine.stats()
        assert stats["total_searches"] == 2

    def test_unregister_provider(self) -> None:
        engine = self._make_engine()
        engine.unregister_provider("duckduckgo")
        stats = engine.stats()
        assert "duckduckgo" not in stats["providers"]

    def test_search_error_handling(self) -> None:
        engine = WebSearchEngine()

        def broken_search(
            query: str, max_results: int, config: SearchProviderConfig
        ) -> SearchResponse:
            raise RuntimeError("Provider down")

        config = SearchProviderConfig(
            name="broken",
            provider=SearchProvider.BRAVE,
            enabled=True,
            priority=1,
        )
        engine.register_provider(config, broken_search)
        resp = engine.search("test")
        assert resp.error != ""


# ── ContentFetcher ───────────────────────────────────────────────────────────


class TestContentFetcher:
    def test_creation(self) -> None:
        fetcher = ContentFetcher()
        stats = fetcher.stats()
        assert stats["total_fetches"] == 0

    def test_block_domain(self) -> None:
        fetcher = ContentFetcher()
        fetcher.block_domain("evil.com")
        result = fetcher.fetch("https://evil.com/page")
        assert result.status == FetchStatus.BLOCKED
        assert "Blocked domain" in result.error

    def test_unblock_domain(self) -> None:
        fetcher = ContentFetcher()
        fetcher.block_domain("temp.com")
        fetcher.unblock_domain("temp.com")
        # After unblocking, domain is no longer in blocked list
        stats = fetcher.stats()
        assert "temp.com" not in stats.get("blocked_domains", [])

    def test_blocked_scheme_file(self) -> None:
        fetcher = ContentFetcher()
        result = fetcher.fetch("file:///etc/passwd")
        assert result.status == FetchStatus.BLOCKED
        assert "Blocked scheme" in result.error

    def test_invalid_url_empty(self) -> None:
        fetcher = ContentFetcher()
        result = fetcher.fetch("")
        assert result.status == FetchStatus.INVALID_URL

    def test_javascript_scheme_blocked(self) -> None:
        fetcher = ContentFetcher()
        result = fetcher.fetch("javascript:alert(1)")
        assert result.status == FetchStatus.BLOCKED

    def test_ftp_scheme_blocked(self) -> None:
        fetcher = ContentFetcher()
        result = fetcher.fetch("ftp://files.example.com/data")
        assert result.status == FetchStatus.BLOCKED

    def test_missing_scheme(self) -> None:
        fetcher = ContentFetcher()
        result = fetcher.fetch("example.com")
        assert result.status == FetchStatus.INVALID_URL
        assert "Missing" in result.error

    def test_stats_increments(self) -> None:
        fetcher = ContentFetcher()
        fetcher.fetch("https://nonexistent.invalid.test")
        stats = fetcher.stats()
        assert stats["total_fetches"] >= 1

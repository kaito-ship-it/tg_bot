from app.dedup import normalize_source_url


def test_normalize_source_url_removes_tracking_noise() -> None:
    url = (
        "https://Example.com/news//42/?utm_source=telegram&utm_campaign=test"
        "&b=2&a=1#section"
    )

    assert normalize_source_url(url) == "https://example.com/news/42?a=1&b=2"


def test_normalize_source_url_keeps_meaningful_query_parameters() -> None:
    assert normalize_source_url("https://example.com/news?id=42&lang=ru") == (
        "https://example.com/news?id=42&lang=ru"
    )
    assert normalize_source_url("https://example.com/news?id=43&lang=ru") != (
        normalize_source_url("https://example.com/news?id=42&lang=ru")
    )


def test_normalize_source_url_rejects_non_web_links() -> None:
    assert normalize_source_url(None) is None
    assert normalize_source_url("not a url") is None
    assert normalize_source_url("file:///tmp/article.html") is None

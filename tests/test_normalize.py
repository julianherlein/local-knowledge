import pytest

from kb.normalize import InvalidURL, extract_hashtags, extract_urls, normalize


@pytest.mark.parametrize(
    "url, canonical, stype",
    [
        # X: host variants, handle independence, extra path segments, tracking params
        ("https://twitter.com/jack/status/20", "https://x.com/i/status/20", "x"),
        ("https://mobile.twitter.com/jack/status/20?s=20&t=abc", "https://x.com/i/status/20", "x"),
        ("https://x.com/someone_else/status/20/photo/1", "https://x.com/i/status/20", "x"),
        ("https://x.com/i/web/status/20", "https://x.com/i/status/20", "x"),
        ("https://www.x.com/a/statuses/20", "https://x.com/i/status/20", "x"),
        ("x.com/a/status/20", "https://x.com/i/status/20", "x"),
        ("https://x.com/karpathy", "https://x.com/karpathy", "web"),
        # YouTube: all id forms collapse; t and si dropped
        ("https://youtu.be/dQw4w9WgXcQ?si=xyz&t=42", "https://youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1m2s&list=PL1",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
            "youtube",
        ),
        ("https://youtube.com/shorts/dQw4w9WgXcQ?feature=share", "https://youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("https://www.youtube.com/@AndrejKarpathy", "https://youtube.com/@AndrejKarpathy", "web"),
        # Web: utm_*, ref, fragments, www, trailing slash, param order, scheme
        (
            "http://www.Example.com/post/?utm_source=x&b=2&a=1&ref=hn#section",
            "https://example.com/post?a=1&b=2",
            "web",
        ),
        ("https://example.com/", "https://example.com/", "web"),
        ("https://example.com", "https://example.com/", "web"),
        ("https://blog.example.com/a/b/", "https://blog.example.com/a/b", "web"),
        ("https://example.com/p?id=7&utm_medium=email", "https://example.com/p?id=7", "web"),
        ("telegram://123/456", "telegram://123/456", "web"),
    ],
)
def test_normalize(url, canonical, stype):
    n = normalize(url)
    assert n.canonical_url == canonical
    assert n.source_type == stype


def test_normalize_is_idempotent():
    for url in ["https://youtu.be/dQw4w9WgXcQ", "https://twitter.com/a/status/1", "http://www.example.com/x/?utm_x=1"]:
        once = normalize(url).canonical_url
        assert normalize(once).canonical_url == once


@pytest.mark.parametrize("bad", ["", "not a url", "ftp://example.com/x", "https://", "https://localhost/x"])
def test_invalid(bad):
    with pytest.raises(InvalidURL):
        normalize(bad)


def test_trailing_punctuation_is_stripped():
    assert normalize("https://example.com/a).").canonical_url == "https://example.com/a"


def test_extract_urls_dedups_by_canonical_form():
    text = "see https://youtu.be/dQw4w9WgXcQ and https://www.youtube.com/watch?v=dQw4w9WgXcQ, also (https://example.com/x)."
    assert extract_urls(text) == ["https://youtu.be/dQw4w9WgXcQ", "https://example.com/x"]


def test_extract_hashtags_ignores_url_fragments():
    assert extract_hashtags("#AI read this https://example.com/#frag #sd #ai") == ["ai", "sd"]
    assert extract_hashtags("C# is not a tag, issue#4 neither") == []


def test_balanced_parentheses_are_kept():
    """Critic M2: a trailing ')' that belongs to the URL must survive."""
    url = "https://en.wikipedia.org/wiki/Mercury_(planet)"
    assert normalize(url).canonical_url == url
    assert extract_urls(f"see {url}.") == [url]
    assert extract_urls("(see https://example.com/a)") == ["https://example.com/a"]
    assert normalize("https://example.com/a_(b)).").canonical_url == "https://example.com/a_(b)"


def test_non_default_port_is_a_different_server():
    assert normalize("http://example.com:8080/doc").canonical_url == "http://example.com:8080/doc"
    assert normalize("https://example.com:443/doc").canonical_url == "https://example.com/doc"
    assert normalize("http://example.com:80/doc").canonical_url == "https://example.com/doc"
    with pytest.raises(InvalidURL):
        normalize("https://example.com:99999/doc")

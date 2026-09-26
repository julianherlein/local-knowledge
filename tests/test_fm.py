from datetime import date, datetime, timedelta, timezone

import pytest
import yaml

from kb import fm


def test_dump_is_flat_ordered_and_obsidian_friendly():
    meta = {
        "id": 142,
        "title": "Why we moved off Airflow: a story",
        "source_type": "web",
        "url": "https://example.com/a?b=1",
        "published": date(2026, 9, 20),
        "captured": datetime(2026, 9, 26, 10, 14, tzinfo=timezone(timedelta(hours=-3))),
        "domains": ["data-engineering", "system-design"],
        "outlinks": [],
        "truncated": True,
        "source": "[[raw/web/2026-09-26-why]]",
    }
    out = fm.dump(meta)
    lines = out.splitlines()
    assert lines[0] == lines[-1] == "---"
    assert [ln.split(":", 1)[0] for ln in lines[1:-1]] == list(meta)
    assert "published: 2026-09-20" in lines  # unquoted -> Obsidian types it as a date
    assert "captured: 2026-09-26T10:14:00-03:00" in lines
    assert "domains: [data-engineering, system-design]" in lines
    assert "outlinks: []" in lines
    parsed = yaml.safe_load(out.strip("-\n"))
    assert parsed["title"] == meta["title"]
    assert parsed["url"] == meta["url"]
    assert parsed["published"] == date(2026, 9, 20)
    assert parsed["source"] == "[[raw/web/2026-09-26-why]]"
    assert parsed["truncated"] is True


@pytest.mark.parametrize(
    "value",
    [
        "yes",
        "No",
        "null",
        "123",
        "1.5",
        "2026-01-01",
        "a: b",
        "x #y",
        " padded",
        "#tag",
        "[x]",
        "{a}",
        "it's",
        'say "hi"',
        "",
        "ñandú",
        "0x1F",
        "-",
        "@me",
    ],
)
def test_strings_round_trip_as_strings(value):
    parsed = yaml.safe_load(fm.dump({"k": value}).strip("-\n"))
    assert parsed["k"] == value


def test_nested_mapping_rejected():
    with pytest.raises(TypeError):
        fm.dump({"a": {"b": 1}})


def test_replace_front_matter_preserves_body_bytes():
    body = "\n# Title\n\nLine with trailing spaces   \n\n---\nnot front matter\n\r\nend"
    text = fm.dump({"domains": ["unsorted"], "tags": ["raw", "unsorted"], "id": 3}) + body
    new = fm.replace_front_matter(text, {"domains": ["tennis"], "tags": ["raw", "tennis"]})
    meta, new_body = fm.split(new)
    assert new_body == body
    assert meta["domains"] == ["tennis"]
    assert list(meta) == ["domains", "tags", "id"]


def test_split_without_front_matter():
    assert fm.split("# just a note\n") == ({}, "# just a note\n")


@pytest.mark.parametrize(
    "value",
    [
        "1.",
        "0b101",
        "1.5_0",
        "1.e+5",
        "0o17",
        ".5",
        "a\x92b",
        "a\x85b",
        "x" + chr(0x2028) + "y",
        "del\x7f",
        "bom" + chr(0xFEFF),
        "tab\tin",
    ],
)
def test_yaml11_lookalikes_and_control_chars_round_trip(value):
    """Critic M1: these used to load back as numbers or crash yaml.safe_load."""
    out = fm.dump({"k": value})
    assert yaml.safe_load(out.strip("-\n"))["k"] == value
    assert "\x85" not in out and chr(0x2028) not in out and "\x92" not in out


def test_lone_surrogate_is_replaced_not_crashing():
    out = fm.dump({"k": "a\ud800b"})
    assert yaml.safe_load(out.strip("-\n"))["k"] == "a�b"
    out.encode("utf-8")

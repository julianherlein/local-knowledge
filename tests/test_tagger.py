import pytest
from kb_llm import FakeLLMClient, LLMError

from kb.models import FetchedItem
from kb.tagger import UNSORTED, classifier_schema, hashtag_domains, tag

ITEM = FetchedItem(
    source_type="web", url="https://example.com", title="Airflow vs Dagster", body="orchestration " * 3000
)


def llm(domains, confidence, language="en"):
    return FakeLLMClient(
        lambda req: {"domains": domains, "confidence": confidence, "language": language, "reason": "r"}
    )


def test_classifier_multi_label(settings):
    r = tag(settings, llm(["data-engineering", "system-design"], 0.9), ITEM, [])
    assert r.domains == ["data-engineering", "system-design"] and r.method == "classifier"
    assert r.language == "en"


def test_low_confidence_is_unsorted(settings):
    r = tag(settings, llm(["tennis"], 0.4), ITEM, [])
    assert r.domains == [UNSORTED] and r.method == "fallback" and r.confidence == 0.4


def test_threshold_is_inclusive(settings):
    assert tag(settings, llm(["tennis"], 0.6), ITEM, []).domains == ["tennis"]


def test_hashtag_sets_primary_classifier_adds_secondary(settings):
    r = tag(settings, llm(["data-engineering", "ai-llms"], 0.8), ITEM, ["sd"])
    assert r.domains == ["system-design", "data-engineering"] and r.method == "hashtag"


def test_hashtag_wins_even_when_classifier_unsure(settings):
    r = tag(settings, llm(["tennis"], 0.2), ITEM, ["#ai"])
    assert r.domains == ["ai-llms"] and r.method == "hashtag"


def test_two_hashtags_fill_both_slots(settings):
    r = tag(settings, llm(["tennis"], 0.99), ITEM, ["de", "sysdesign"])
    assert r.domains == ["data-engineering", "system-design"]


def test_classifier_error_with_hashtag_degrades(settings):
    r = tag(settings, FakeLLMClient(lambda req: LLMError("down")), ITEM, ["tennis"])
    assert r.domains == ["tennis"] and r.method == "hashtag"


def test_classifier_error_without_hashtag_raises(settings):
    with pytest.raises(LLMError):
        tag(settings, FakeLLMClient(lambda req: LLMError("down")), ITEM, [])


def test_unknown_domains_from_model_are_dropped(settings):
    r = tag(settings, llm(["cooking", "tennis", "tennis"], 0.9), ITEM, [])
    assert r.domains == ["tennis"]


def test_prompt_is_bounded_and_schema_enumerates_domains(settings):
    fake = llm(["tennis"], 0.9)
    tag(settings, fake, ITEM, [])
    req = fake.calls[0]
    assert len(req.prompt) < settings.llm.classifier_excerpt_tokens * 4 + 3000
    assert req.json_schema == classifier_schema(settings.domain_names)
    assert set(req.json_schema["properties"]["domains"]["items"]["enum"]) == set(settings.domain_names)


def test_hashtag_map_accepts_full_names_and_aliases(settings):
    assert hashtag_domains(settings, ["#Tennis", "data-engineering", "nope"]) == ["tennis", "data-engineering"]

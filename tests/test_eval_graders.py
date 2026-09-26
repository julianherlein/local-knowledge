"""The eval graders are deterministic code, so they get gate tests like everything else."""

from evals.cases import CLASSIFY, SUMMARY
from evals.run import grade_summary, norm_num, numbers


def test_norm_num_equates_separators():
    assert norm_num("18.000") == norm_num("18,000") == norm_num("18000") == "18000"
    assert norm_num("36%") == "36%"
    assert norm_num("1.5") == "1.5"


def test_numbers_ignores_timestamps():
    assert numbers("at [04:30] latency went from 800 ms to 120 ms") == {"800", "120"}


def test_grader_catches_invented_and_missing_numbers_and_fake_timestamps():
    case = next(c for c in SUMMARY if c.name == "video-postgres")
    good = "PgBouncer cut 4,000 connections to 300 [02:10]. Citus: 32 shards, 800 ms to 120 ms [04:30]. 64 cores, 512 GB, 85%, 50 ms."
    g = grade_summary(case, good, 4)
    assert all(g["checks"].values()), g
    bad = good.replace("32 shards", "48 shards") + " [09:99]"
    g = grade_summary(case, bad, 4)
    assert not g["checks"]["numbers_kept"] and g["invented_numbers"] == ["48"]
    assert g["fake_timestamps"] == ["09:99"]


def test_grader_flags_untranslated_spanish():
    case = next(c for c in SUMMARY if c.name == "spanish-article")
    g = grade_summary(
        case,
        "El equipo migró los pipelines de Airflow a Dagster con una reducción del 36% para los 140 y 12 y 5.000",
        3,
    )
    assert not g["checks"]["english"]


def test_case_sets_are_well_formed():
    from kb.config import DEFAULT_DOMAINS

    names = {d["name"] for d in DEFAULT_DOMAINS} | {"unsorted"}
    assert all(c.primary in names and set(c.also_ok) <= names for c in CLASSIFY)
    assert len({c.name for c in CLASSIFY}) == len(CLASSIFY)
    assert sum(c.primary == "unsorted" for c in CLASSIFY) >= 2

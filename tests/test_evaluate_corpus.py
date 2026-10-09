"""Reference-corpus contract and cluster-evaluation regression tests."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from scripts.evaluate_corpus import Corpus, Predictions, evaluate, load_corpus

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "corpus"


def fixture_corpus() -> Corpus:
    return load_corpus(FIXTURE_DIR / "synthetic-v1.json")


def fixture_predictions() -> Predictions:
    return Predictions.model_validate_json(
        (FIXTURE_DIR / "synthetic-perfect-predictions.json").read_text(encoding="utf-8")
    )


def test_perfect_predictions_report_precision_and_recall_by_split() -> None:
    result = evaluate(fixture_corpus(), fixture_predictions())

    assert set(result) == {"development", "blind_acceptance"}
    for metrics in result.values():
        assert metrics["precision"] == 1.0
        assert metrics["recall"] == 1.0
        assert metrics["false_joins"] == []


def test_false_join_is_reported_separately_and_flags_explicit_non_merge() -> None:
    corpus = fixture_corpus()
    predictions = fixture_predictions()
    assignments = {**predictions.assignments, "a3": "cluster-1"}

    result = evaluate(corpus, predictions.model_copy(update={"assignments": assignments}))
    development = result["development"]

    assert development["precision"] == 1 / 3
    assert development["recall"] == 1 / 2
    assert {tuple(item["articles"]) for item in development["false_joins"]} == {
        ("a1", "a3"),
        ("a2", "a3"),
    }
    assert (
        next(item for item in development["false_joins"] if item["articles"] == ["a1", "a3"])[
            "explicit_non_merge"
        ]
        is True
    )


def test_uncertain_pair_is_excluded_from_scoring() -> None:
    data = fixture_corpus().model_dump(mode="json")
    data["uncertain_pairs"] = [
        {"left_article_id": "a2", "right_article_id": "a4", "reason": "Ambiguous follow-up."}
    ]
    corpus = Corpus.model_validate(data)
    predictions = fixture_predictions()
    assignments = {**predictions.assignments, "a4": "cluster-1"}

    result = evaluate(corpus, predictions.model_copy(update={"assignments": assignments}))

    assert result["development"]["false_joins"] == [
        {"articles": ["a1", "a4"], "explicit_non_merge": False}
    ]


def test_non_merge_cannot_contradict_story_label() -> None:
    data = fixture_corpus().model_dump(mode="json")
    data["non_merges"].append(
        {"left_article_id": "a1", "right_article_id": "a2", "reason": "Contradictory label."}
    )

    with pytest.raises(ValidationError, match="conflicts with story membership"):
        Corpus.model_validate(data)


def test_full_text_and_private_url_cannot_enter_manifest() -> None:
    data = fixture_corpus().model_dump(mode="json")
    data["articles"][0]["body"] = "Article body must remain outside Git."
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Corpus.model_validate(data)

    del data["articles"][0]["body"]
    data["articles"][0]["url"] = "http://192.0.2.10/internal"
    with pytest.raises(ValidationError, match="domain, not an IP"):
        Corpus.model_validate(data)


def test_signal_needs_supporting_article_and_two_label_passes() -> None:
    data = fixture_corpus().model_dump(mode="json")
    data["stories"][0]["signals"][0]["source_article_id"] = "b3"
    with pytest.raises(ValidationError, match="signal evidence article must belong to story"):
        Corpus.model_validate(data)

    data = fixture_corpus().model_dump(mode="json")
    data["stories"][0]["label_passes"] = ["first"]
    with pytest.raises(ValidationError, match="both label passes are required"):
        Corpus.model_validate(data)


def test_predictions_must_cover_exact_corpus() -> None:
    predictions = fixture_predictions()
    assignments = {k: v for k, v in predictions.assignments.items() if k != "b3"}
    with pytest.raises(ValueError, match="exactly once"):
        evaluate(fixture_corpus(), predictions.model_copy(update={"assignments": assignments}))


def test_public_reference_requires_frozen_split_and_window() -> None:
    data = fixture_corpus().model_dump(mode="json")
    data["selection"]["kind"] = "public_reference"
    with pytest.raises(ValidationError, match="must be frozen"):
        Corpus.model_validate(data)

    data["selection"]["frozen"] = True
    with pytest.raises(ValidationError, match="25-40 stories"):
        Corpus.model_validate(data)

    data["selection"]["kind"] = "synthetic"
    data["articles"][0]["published_at"] = "2025-12-31T10:00:00Z"
    with pytest.raises(ValidationError, match="outside selection window"):
        Corpus.model_validate(data)


def test_fixture_is_synthetic_and_json_round_trips() -> None:
    corpus = fixture_corpus()
    assert corpus.selection.kind == "synthetic"
    assert corpus.selection.frozen is False
    assert json.loads(corpus.model_dump_json())["schema_version"] == 1

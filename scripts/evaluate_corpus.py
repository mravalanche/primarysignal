"""Validate a versioned corpus and score predicted story memberships."""

from __future__ import annotations

import argparse
import ipaddress
import json
from collections import Counter
from datetime import date, datetime
from itertools import combinations
from pathlib import Path
from typing import Literal, TypedDict, get_args
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

Topic = Literal[
    "Vulnerabilities & Exploitation",
    "Threat Activity & Incidents",
    "Security Engineering",
    "Policy & Strategy",
    "Research & Tools",
]
StoryType = Literal[
    "News", "Research", "Advisory", "Incident", "Analysis", "Opinion", "Tool Release"
]
Split = Literal["development", "blind_acceptance"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Selection(StrictModel):
    kind: Literal["synthetic", "public_reference"]
    window_start: date
    window_end: date
    random_seed: int = Field(ge=0)
    frozen: bool

    @model_validator(mode="after")
    def window_order(self) -> Selection:
        if self.window_end < self.window_start:
            raise ValueError("selection window ends before it starts")
        return self


class Article(StrictModel):
    id: str = Field(min_length=1)
    split: Split
    url: str
    publisher: str = Field(min_length=1)
    ownership_group: str = Field(min_length=1)
    source_category: str = Field(min_length=1)
    published_at: datetime
    title: str = Field(min_length=1)

    @model_validator(mode="after")
    def public_url(self) -> Article:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("article URL must be public HTTP(S)")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("article URL cannot contain credentials or a fragment")
        host = parsed.hostname
        if host == "localhost" or host.endswith(".local") or "." not in host:
            raise ValueError("article URL host must be a public domain")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("article URL host must be a domain, not an IP address")
        if self.published_at.utcoffset() is None:
            raise ValueError("article publication time needs a timezone")
        return self


class Entity(StrictModel):
    kind: Literal["cve", "organisation", "product", "actor", "technology", "sector"]
    name: str = Field(min_length=1)


class Signal(StrictModel):
    name: Literal[
        "Primary Source",
        "Official Advisory",
        "Active Exploitation",
        "Exploit Available",
        "Actionable",
        "Confirmed Incident",
        "Developing",
        "Widely Reported",
        "Deep Read",
    ]
    source_article_id: str
    evidence_ref: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    decision_route: Literal["automatic", "editor"]
    uncertain: bool


class UkRelevance(StrictModel):
    applies: bool
    reason: str = Field(min_length=1)


class MaterialUpdate(StrictModel):
    article_id: str
    change: str = Field(min_length=1)


class Story(StrictModel):
    id: str = Field(min_length=1)
    split: Split
    article_ids: list[str] = Field(min_length=1)
    representative_article_id: str
    primary_source_article_id: str
    primary_source_reason: str = Field(min_length=1)
    primary_topic: Topic
    secondary_topics: list[Topic] = Field(max_length=2)
    story_type: StoryType
    entities: list[Entity]
    signals: list[Signal]
    uk_relevance: UkRelevance
    material_updates: list[MaterialUpdate]
    uncertainty: str | None
    label_passes: list[Literal["first", "second"]]
    adjudication_note: str = Field(min_length=1)

    @model_validator(mode="after")
    def internal_refs(self) -> Story:
        members = set(self.article_ids)
        if len(members) != len(self.article_ids):
            raise ValueError("duplicate article in story")
        if (
            self.representative_article_id not in members
            or self.primary_source_article_id not in members
        ):
            raise ValueError("representative and primary source must belong to story")
        if self.primary_topic in self.secondary_topics or len(set(self.secondary_topics)) != len(
            self.secondary_topics
        ):
            raise ValueError("secondary topics must be distinct from primary and each other")
        if set(self.label_passes) != {"first", "second"} or len(self.label_passes) != 2:
            raise ValueError("both label passes are required")
        for signal in self.signals:
            if signal.source_article_id not in members:
                raise ValueError("signal evidence article must belong to story")
        for update in self.material_updates:
            if update.article_id not in members:
                raise ValueError("material update article must belong to story")
        return self


class Pair(StrictModel):
    left_article_id: str
    right_article_id: str
    reason: str = Field(min_length=1)


class Corpus(StrictModel):
    schema_version: Literal[1]
    corpus_id: str = Field(min_length=1)
    revision_note: str = Field(min_length=1)
    selection: Selection
    articles: list[Article] = Field(min_length=1)
    stories: list[Story] = Field(min_length=1)
    non_merges: list[Pair]
    uncertain_pairs: list[Pair]

    @model_validator(mode="after")
    def references(self) -> Corpus:
        if self.selection.kind == "public_reference" and not self.selection.frozen:
            raise ValueError("public reference split must be frozen before evaluation")
        articles = {article.id: article for article in self.articles}
        if len(articles) != len(self.articles):
            raise ValueError("duplicate article ID")
        if len({article.url for article in self.articles}) != len(self.articles):
            raise ValueError("duplicate article URL")
        if len({story.id for story in self.stories}) != len(self.stories):
            raise ValueError("duplicate story ID")
        for article in self.articles:
            if (
                not self.selection.window_start
                <= article.published_at.date()
                <= self.selection.window_end
            ):
                raise ValueError(f"article {article.id} falls outside selection window")
        membership: dict[str, str] = {}
        for story in self.stories:
            for article_id in story.article_ids:
                if article_id not in articles:
                    raise ValueError(f"unknown article {article_id}")
                if articles[article_id].split != story.split:
                    raise ValueError(f"story {story.id} crosses the frozen split")
                if article_id in membership:
                    raise ValueError(f"article {article_id} belongs to multiple stories")
                membership[article_id] = story.id
        if membership.keys() != articles.keys():
            raise ValueError(f"unlabelled articles: {sorted(articles.keys() - membership.keys())}")
        seen_pairs: set[frozenset[str]] = set()
        for pair in [*self.non_merges, *self.uncertain_pairs]:
            key = frozenset((pair.left_article_id, pair.right_article_id))
            if pair.left_article_id == pair.right_article_id or any(
                article_id not in articles for article_id in key
            ):
                raise ValueError("pair must name two distinct known articles")
            if articles[pair.left_article_id].split != articles[pair.right_article_id].split:
                raise ValueError("pair cannot cross the frozen split")
            if key in seen_pairs:
                raise ValueError("pair is repeated or both uncertain and non-merge")
            seen_pairs.add(key)
            if membership[pair.left_article_id] == membership[pair.right_article_id]:
                raise ValueError("non-merge or uncertain pair conflicts with story membership")
        if self.selection.kind == "public_reference":
            if not 25 <= len(self.stories) <= 40:
                raise ValueError("public reference needs 25-40 stories")
            topics = Counter(story.primary_topic for story in self.stories)
            if any(topics[topic] < 5 for topic in get_args(Topic)):
                raise ValueError("public reference needs five stories in each primary topic")
            if sum(len(story.article_ids) == 1 for story in self.stories) < 5:
                raise ValueError("public reference needs five single-source stories")
            publisher_counts = Counter(article.publisher for article in self.articles)
            if any(count / len(self.articles) > 0.15 for count in publisher_counts.values()):
                raise ValueError("publisher exceeds 15% of public reference articles")
        return self


class Predictions(StrictModel):
    schema_version: Literal[1]
    corpus_id: str
    assignments: dict[str, str]


class FalseJoin(TypedDict):
    articles: list[str]
    explicit_non_merge: bool


class Metrics(TypedDict):
    precision: float | None
    recall: float | None
    true_pairs: int
    false_joins: list[FalseJoin]
    missed_pairs: int


def load_corpus(path: Path) -> Corpus:
    return Corpus.model_validate_json(path.read_text(encoding="utf-8"))


def evaluate(corpus: Corpus, predictions: Predictions) -> dict[str, Metrics]:
    if predictions.corpus_id != corpus.corpus_id:
        raise ValueError("prediction corpus_id does not match manifest")
    article_ids = {article.id for article in corpus.articles}
    if predictions.assignments.keys() != article_ids:
        raise ValueError("predictions must assign each corpus article exactly once")
    if any(not cluster_id for cluster_id in predictions.assignments.values()):
        raise ValueError("predicted cluster IDs cannot be empty")
    truth = {article_id: story.id for story in corpus.stories for article_id in story.article_ids}
    uncertain = {
        frozenset((pair.left_article_id, pair.right_article_id)) for pair in corpus.uncertain_pairs
    }
    explicit_non_merges = {
        frozenset((pair.left_article_id, pair.right_article_id)) for pair in corpus.non_merges
    }
    results: dict[str, Metrics] = {}
    for split in ("development", "blind_acceptance"):
        members = sorted(article.id for article in corpus.articles if article.split == split)
        tp = fp = fn = 0
        false_joins: list[FalseJoin] = []
        for left, right in combinations(members, 2):
            pair = frozenset((left, right))
            if pair in uncertain:
                continue
            same_predicted = predictions.assignments[left] == predictions.assignments[right]
            same_expected = truth[left] == truth[right]
            if same_predicted and same_expected:
                tp += 1
            elif same_predicted:
                fp += 1
                false_joins.append(
                    {"articles": [left, right], "explicit_non_merge": pair in explicit_non_merges}
                )
            elif same_expected:
                fn += 1
        results[split] = {
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "true_pairs": tp,
            "false_joins": false_joins,
            "missed_pairs": fn,
        }
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("predictions", type=Path)
    args = parser.parse_args()
    corpus = load_corpus(args.corpus)
    predictions = Predictions.model_validate_json(args.predictions.read_text(encoding="utf-8"))
    print(json.dumps(evaluate(corpus, predictions), indent=2))


if __name__ == "__main__":
    main()

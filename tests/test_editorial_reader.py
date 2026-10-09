"""Validation for the private editorial metadata reader."""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from primary_signal.publication.editorial_reader import EditorialReader


def test_reader_rejects_unbounded_inputs_before_database_access() -> None:
    engine = MagicMock()
    reader = EditorialReader(engine, expected_role="editorial_test")
    with pytest.raises(ValueError, match="limit"):
        reader.list_stories(limit=51)
    with pytest.raises(ValueError, match="timezone"):
        reader.list_stories(before=(datetime(2026, 10, 9), uuid.uuid7()))
    with pytest.raises(ValueError, match="story slug"):
        reader.get_story("../private")
    engine.connect.assert_not_called()


def test_reader_requires_expected_login_name() -> None:
    with pytest.raises(ValueError, match="expected_role"):
        EditorialReader(MagicMock(), expected_role="")
    assert datetime.now(UTC).tzinfo is not None

"""Unit tests for domain model edge cases (error paths and utility methods)."""

from unittest.mock import MagicMock, patch

import pytest

from ere.models.resolver import ClusterId, MentionId
from ere.models.resolver.cluster import CandidateCluster, ResolutionResult
from ere.models.resolver.similarity import MentionLink

# ============================================================================
# MentionLink
# ============================================================================


def test_mention_link_rejects_same_left_and_right_id():
    m = MentionId(value="x")
    with pytest.raises(ValueError, match="left_id and right_id must differ"):
        MentionLink(left_id=m, right_id=m, score=0.9)


def test_mention_link_other_returns_right_when_from_is_left():
    left = MentionId(value="a")
    right = MentionId(value="b")
    link = MentionLink(left_id=left, right_id=right, score=0.5)
    assert link.other(left) == right


def test_mention_link_other_returns_left_when_from_is_right():
    left = MentionId(value="a")
    right = MentionId(value="b")
    link = MentionLink(left_id=left, right_id=right, score=0.5)
    assert link.other(right) == left


def test_mention_link_other_raises_when_id_not_in_link():
    left = MentionId(value="a")
    right = MentionId(value="b")
    unknown = MentionId(value="z")
    link = MentionLink(left_id=left, right_id=right, score=0.5)
    with pytest.raises(ValueError):
        link.other(unknown)


# ============================================================================
# ResolutionResult / CandidateCluster
# ============================================================================


def test_resolution_result_rejects_empty_candidates():
    with pytest.raises(ValueError, match="must be non-empty"):
        ResolutionResult(candidates=())


def test_candidate_cluster_as_tuple_returns_id_and_score():
    c = CandidateCluster(cluster_id=ClusterId(value="c1"), score=0.75)
    assert c.as_tuple() == ("c1", 0.75)


def test_resolution_result_as_tuples_returns_list():
    candidates = (
        CandidateCluster(cluster_id=ClusterId(value="c1"), score=0.9),
        CandidateCluster(cluster_id=ClusterId(value="c2"), score=0.6),
    )
    result = ResolutionResult(candidates=candidates)
    assert result.as_tuples() == [("c1", 0.9), ("c2", 0.6)]


# ============================================================================
# app.main() failure paths
# ============================================================================


def test_main_exits_when_redis_connection_fails(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ere"])
    with (
        patch("redis.Redis") as mock_redis_cls,
        patch("ere.entrypoints.app.configure_logging"),
    ):
        mock_redis_cls.return_value.ping.side_effect = ConnectionError("no redis")
        with pytest.raises(SystemExit) as exc:
            from ere.entrypoints.app import main

            main()
    assert exc.value.code == 1


def test_main_exits_when_service_build_fails(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ere"])
    with (
        patch("redis.Redis") as mock_redis_cls,
        patch("ere.entrypoints.app.configure_logging"),
        patch(
            "ere.entrypoints.app.build_entity_resolver",
            side_effect=RuntimeError("build fail"),
        ),
    ):
        mock_redis_cls.return_value.ping.return_value = True
        with pytest.raises(SystemExit) as exc:
            from ere.entrypoints.app import main

            main()
    assert exc.value.code == 1


def test_main_runs_loop_until_keyboard_interrupt(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ere"])
    mock_resolver = MagicMock()
    mock_resolver._mention_repo._con = MagicMock()

    with (
        patch("redis.Redis") as mock_redis_cls,
        patch("ere.entrypoints.app.configure_logging"),
        patch("ere.entrypoints.app.build_entity_resolver", return_value=mock_resolver),
        patch("ere.entrypoints.app.build_rdf_mapper", return_value=MagicMock()),
        patch(
            "ere.entrypoints.app.build_entity_resolution_service",
            return_value=MagicMock(),
        ),
        patch("ere.entrypoints.app.RedisQueueWorker") as mock_worker_cls,
    ):
        mock_redis_cls.return_value.ping.return_value = True
        mock_worker_cls.return_value.process_bite.side_effect = KeyboardInterrupt()
        from ere.entrypoints.app import main

        main()  # must return cleanly (KeyboardInterrupt caught internally)

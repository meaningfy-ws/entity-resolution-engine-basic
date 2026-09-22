"""Unit tests: batch settings read from the environment at start-up (entrypoints / composition root).

Spec: batched-resolution — "Bites are sized by processing capacity, not by arrival" (invalid batch setting).
"""

import pytest

from ere.entrypoints.bootstrap import BatchEnvVar, resolve_batch_settings
from ere.services.resolver_config import BatchSettings


def test_defaults_when_no_environment():
    assert resolve_batch_settings({}) == BatchSettings(
        target_seconds=2.0, max_mentions=500, max_bytes=50_000_000, linger_ms=250
    )


def test_values_read_from_environment():
    env = {
        BatchEnvVar.TARGET_SECONDS: "1.5",
        BatchEnvVar.MAX_MENTIONS: "200",
        BatchEnvVar.MAX_BYTES: "1000000",
        BatchEnvVar.LINGER_MS: "100",
    }

    assert resolve_batch_settings(env) == BatchSettings(
        target_seconds=1.5, max_mentions=200, max_bytes=1_000_000, linger_ms=100
    )


@pytest.mark.parametrize(
    "variable",
    [BatchEnvVar.TARGET_SECONDS, BatchEnvVar.MAX_MENTIONS, BatchEnvVar.MAX_BYTES],
)
@pytest.mark.parametrize("value", ["0", "-1", "many"])
def test_invalid_values_are_rejected_naming_the_variable(variable, value):
    with pytest.raises(ValueError, match=variable.value):
        resolve_batch_settings({variable: value})


@pytest.mark.parametrize("value", ["inf", "-inf", "nan"])
@pytest.mark.parametrize("variable", list(BatchEnvVar))
def test_non_finite_values_are_rejected_naming_the_variable(variable, value):
    with pytest.raises(ValueError, match=variable.value):
        resolve_batch_settings({variable: value})


@pytest.mark.parametrize("value", ["-1", "many"])
def test_invalid_linger_is_rejected_naming_the_variable(value):
    with pytest.raises(ValueError, match=BatchEnvVar.LINGER_MS.value):
        resolve_batch_settings({BatchEnvVar.LINGER_MS: value})


def test_linger_can_be_switched_off():
    assert resolve_batch_settings({BatchEnvVar.LINGER_MS: "0"}).linger_ms == 0

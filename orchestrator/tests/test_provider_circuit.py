"""Pure policy tests for AgDR-026 provider circuits."""

from __future__ import annotations

from dataclasses import fields

import pytest

from orchestrator.provider_circuit import (
    CIRCUIT_FAILURE_CLASSES,
    CircuitState,
    ProviderCircuit,
)
from orchestrator.types import FailureClass


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, milliseconds: int) -> None:
        self.now += milliseconds / 1000


@pytest.mark.parametrize(
    "failure_class",
    [
        FailureClass.PROVIDER_AUTHENTICATION,
        FailureClass.PROVIDER_PLAN_LIMIT,
        FailureClass.PROVIDER_CREDITS_EXHAUSTED,
    ],
)
def test_latched_provider_failures_open_without_automatic_probe(
    failure_class: FailureClass,
) -> None:
    clock = Clock()
    circuit = ProviderCircuit("codex", cooldown_ms=1000, clock=clock)

    transition = circuit.record_failure(failure_class)
    clock.advance_ms(10_000)

    assert transition is not None
    assert transition.state is CircuitState.OPEN_LATCHED
    assert transition.failure_class is failure_class
    assert not circuit.acquire_dispatch().allowed


@pytest.mark.parametrize(
    "failure_class",
    [
        FailureClass.PROVIDER_RATE_LIMIT,
        FailureClass.PROVIDER_UNAVAILABLE,
    ],
)
def test_transient_provider_failures_allow_one_probe_after_cooldown(
    failure_class: FailureClass,
) -> None:
    clock = Clock()
    circuit = ProviderCircuit("codex", cooldown_ms=1000, clock=clock)

    opened = circuit.record_failure(failure_class)
    assert opened is not None
    assert opened.state is CircuitState.OPEN_COOLDOWN
    assert opened.cooldown_ms == 1000
    assert not circuit.acquire_dispatch().allowed
    assert circuit.cooldown_remaining_ms == 1000

    clock.advance_ms(999)
    assert not circuit.acquire_dispatch().allowed
    clock.advance_ms(1)

    probe = circuit.acquire_dispatch()
    assert probe.allowed
    assert probe.probe_token is not None
    assert probe.transition is not None
    assert probe.transition.state is CircuitState.HALF_OPEN
    assert probe.transition.failure_class is None
    assert "failure_class" not in probe.transition.log_fields()
    assert not circuit.acquire_dispatch().allowed


@pytest.mark.parametrize(
    "failure_class",
    sorted(set(FailureClass) - CIRCUIT_FAILURE_CLASSES, key=str),
)
def test_non_provider_failures_do_not_open_closed_circuit(
    failure_class: FailureClass,
) -> None:
    circuit = ProviderCircuit("codex")

    assert circuit.record_failure(failure_class) is None
    assert circuit.state is CircuitState.CLOSED
    assert circuit.acquire_dispatch().allowed


def test_success_closes_latched_circuit_from_already_running_worker() -> None:
    circuit = ProviderCircuit("codex")
    circuit.record_failure(FailureClass.PROVIDER_PLAN_LIMIT)

    transition = circuit.record_success()

    assert transition is not None
    assert transition.previous_state is CircuitState.OPEN_LATCHED
    assert transition.state is CircuitState.CLOSED
    assert circuit.failure_class is None
    assert circuit.acquire_dispatch().allowed


def test_triggering_half_open_failure_reopens_with_new_policy() -> None:
    clock = Clock()
    circuit = ProviderCircuit("codex", cooldown_ms=1000, clock=clock)
    circuit.record_failure(FailureClass.PROVIDER_RATE_LIMIT)
    clock.advance_ms(1000)
    probe = circuit.acquire_dispatch()

    transition = circuit.record_failure(
        FailureClass.PROVIDER_AUTHENTICATION,
        probe_token=probe.probe_token,
    )

    assert transition is not None
    assert transition.previous_state is CircuitState.HALF_OPEN
    assert transition.state is CircuitState.OPEN_LATCHED
    assert not circuit.acquire_dispatch().allowed


def test_non_triggering_probe_failure_closes_circuit() -> None:
    clock = Clock()
    circuit = ProviderCircuit("codex", cooldown_ms=1000, clock=clock)
    circuit.record_failure(FailureClass.PROVIDER_UNAVAILABLE)
    clock.advance_ms(1000)
    probe = circuit.acquire_dispatch()

    transition = circuit.record_failure(
        FailureClass.WORKER_FAILURE,
        probe_token=probe.probe_token,
    )

    assert transition is not None
    assert transition.state is CircuitState.CLOSED
    assert circuit.acquire_dispatch().allowed


def test_stale_probe_token_cannot_close_newer_outage_generation() -> None:
    clock = Clock()
    circuit = ProviderCircuit("codex", cooldown_ms=1000, clock=clock)
    circuit.record_failure(FailureClass.PROVIDER_RATE_LIMIT)
    clock.advance_ms(1000)
    stale_probe = circuit.acquire_dispatch()
    circuit.record_failure(
        FailureClass.PROVIDER_UNAVAILABLE,
        probe_token=stale_probe.probe_token,
    )

    transition = circuit.record_failure(
        FailureClass.WORKER_FAILURE,
        probe_token=stale_probe.probe_token,
    )

    assert transition is None
    assert circuit.state is CircuitState.OPEN_COOLDOWN


def test_abandoned_probe_restarts_cooldown() -> None:
    clock = Clock()
    circuit = ProviderCircuit("codex", cooldown_ms=1000, clock=clock)
    circuit.record_failure(FailureClass.PROVIDER_RATE_LIMIT)
    clock.advance_ms(1000)
    probe = circuit.acquire_dispatch()

    transition = circuit.abandon_probe(probe.probe_token)

    assert transition is not None
    assert transition.state is CircuitState.OPEN_COOLDOWN
    assert transition.cooldown_ms == 1000
    assert not circuit.acquire_dispatch().allowed


def test_concurrent_transient_failures_do_not_extend_open_cooldown() -> None:
    clock = Clock()
    circuit = ProviderCircuit("codex", cooldown_ms=1000, clock=clock)
    first = circuit.record_failure(FailureClass.PROVIDER_RATE_LIMIT)
    clock.advance_ms(900)

    second = circuit.record_failure(FailureClass.PROVIDER_UNAVAILABLE)
    clock.advance_ms(100)

    assert first is not None
    assert second is None
    assert circuit.acquire_dispatch().allowed


def test_concurrent_latched_failure_escalates_open_cooldown() -> None:
    circuit = ProviderCircuit("codex")
    circuit.record_failure(FailureClass.PROVIDER_RATE_LIMIT)

    transition = circuit.record_failure(FailureClass.PROVIDER_PLAN_LIMIT)

    assert transition is not None
    assert transition.state is CircuitState.OPEN_LATCHED
    assert transition.failure_class is FailureClass.PROVIDER_PLAN_LIMIT


def test_transition_log_fields_are_closed_and_sanitized() -> None:
    circuit = ProviderCircuit("codex", cooldown_ms=1234)

    transition = circuit.record_failure(FailureClass.PROVIDER_RATE_LIMIT)

    assert transition is not None
    assert transition.log_fields() == {
        "provider_id": "codex",
        "circuit_state": "open_cooldown",
        "circuit_generation": 1,
        "failure_class": "provider_rate_limit",
        "cooldown_ms": 1234,
    }
    assert {field.name for field in fields(transition)} == {
        "provider_id",
        "previous_state",
        "state",
        "generation",
        "failure_class",
        "cooldown_ms",
    }


@pytest.mark.parametrize("provider_id", ["", None])
def test_provider_id_must_not_be_empty(provider_id: str | None) -> None:
    with pytest.raises(ValueError, match="provider_id"):
        ProviderCircuit(provider_id)  # type: ignore[arg-type]


@pytest.mark.parametrize("cooldown_ms", [True, False, 0, -1, 1.5, "1000"])
def test_cooldown_must_be_positive_integer(cooldown_ms: object) -> None:
    with pytest.raises(ValueError, match="cooldown_ms"):
        ProviderCircuit("codex", cooldown_ms=cooldown_ms)  # type: ignore[arg-type]

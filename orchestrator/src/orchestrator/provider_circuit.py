"""Provider circuit policy with no scheduler or tracker dependencies."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .types import FailureClass


PROVIDER_CIRCUIT_COOLDOWN_MS = 300_000

LATCHED_FAILURE_CLASSES = frozenset(
    {
        FailureClass.PROVIDER_AUTHENTICATION,
        FailureClass.PROVIDER_PLAN_LIMIT,
        FailureClass.PROVIDER_CREDITS_EXHAUSTED,
    }
)
TRANSIENT_FAILURE_CLASSES = frozenset(
    {
        FailureClass.PROVIDER_RATE_LIMIT,
        FailureClass.PROVIDER_UNAVAILABLE,
    }
)
CIRCUIT_FAILURE_CLASSES = LATCHED_FAILURE_CLASSES | TRANSIENT_FAILURE_CLASSES


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN_LATCHED = "open_latched"
    OPEN_COOLDOWN = "open_cooldown"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitTransition:
    provider_id: str
    previous_state: CircuitState
    state: CircuitState
    generation: int
    failure_class: FailureClass | None = None
    cooldown_ms: int | None = None

    def log_fields(self) -> dict[str, str | int]:
        fields: dict[str, str | int] = {
            "provider_id": self.provider_id,
            "circuit_state": self.state.value,
            "circuit_generation": self.generation,
        }
        if self.failure_class is not None:
            fields["failure_class"] = self.failure_class.value
        if self.cooldown_ms is not None:
            fields["cooldown_ms"] = self.cooldown_ms
        return fields


@dataclass(frozen=True)
class DispatchPermit:
    allowed: bool
    probe_token: int | None = None
    transition: CircuitTransition | None = None


class ProviderCircuit:
    """Single-event-loop circuit policy for one execution provider."""

    def __init__(
        self,
        provider_id: str,
        *,
        cooldown_ms: int = PROVIDER_CIRCUIT_COOLDOWN_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not provider_id:
            raise ValueError("provider_id must not be empty")
        if (
            isinstance(cooldown_ms, bool)
            or not isinstance(cooldown_ms, int)
            or cooldown_ms <= 0
        ):
            raise ValueError("cooldown_ms must be a positive integer")
        self.provider_id = provider_id
        self.cooldown_ms = cooldown_ms
        self._clock = clock
        self.state = CircuitState.CLOSED
        self.failure_class: FailureClass | None = None
        self._cooldown_until: float | None = None
        self._generation = 0
        self._probe_token: int | None = None

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def cooldown_remaining_ms(self) -> int | None:
        if self.state is not CircuitState.OPEN_COOLDOWN:
            return None
        assert self._cooldown_until is not None
        return max(0, round((self._cooldown_until - self._clock()) * 1000))

    @staticmethod
    def is_circuit_failure(failure_class: FailureClass) -> bool:
        return failure_class in CIRCUIT_FAILURE_CLASSES

    def acquire_dispatch(self) -> DispatchPermit:
        if self.state is CircuitState.CLOSED:
            return DispatchPermit(allowed=True)
        if self.state is CircuitState.OPEN_LATCHED:
            return DispatchPermit(allowed=False)
        if self.state is CircuitState.OPEN_COOLDOWN:
            assert self._cooldown_until is not None
            if self._clock() < self._cooldown_until:
                return DispatchPermit(allowed=False)
            transition = self._set_state(
                CircuitState.HALF_OPEN,
                failure_class=self.failure_class,
                expose_failure_class=False,
            )
            self._probe_token = transition.generation
            return DispatchPermit(
                allowed=True,
                probe_token=self._probe_token,
                transition=transition,
            )
        return DispatchPermit(allowed=False)

    def record_success(self) -> CircuitTransition | None:
        if self.state is CircuitState.CLOSED:
            return None
        return self._close()

    def record_failure(
        self,
        failure_class: FailureClass,
        *,
        probe_token: int | None = None,
    ) -> CircuitTransition | None:
        if failure_class in LATCHED_FAILURE_CLASSES:
            if self.state is CircuitState.OPEN_LATCHED:
                return None
            return self._open_latched(failure_class)

        if failure_class in TRANSIENT_FAILURE_CLASSES:
            if self.state in {
                CircuitState.OPEN_LATCHED,
                CircuitState.OPEN_COOLDOWN,
            }:
                return None
            return self._open_cooldown(failure_class)

        if self._is_current_probe(probe_token):
            return self._close()
        return None

    def abandon_probe(self, probe_token: int | None) -> CircuitTransition | None:
        """Re-arm cooldown when the half-open worker exits without an outcome."""
        if not self._is_current_probe(probe_token):
            return None
        assert self.failure_class in TRANSIENT_FAILURE_CLASSES
        return self._open_cooldown(self.failure_class)

    def _is_current_probe(self, probe_token: int | None) -> bool:
        return (
            probe_token is not None
            and self.state is CircuitState.HALF_OPEN
            and probe_token == self._probe_token
        )

    def _open_latched(self, failure_class: FailureClass) -> CircuitTransition:
        return self._set_state(
            CircuitState.OPEN_LATCHED,
            failure_class=failure_class,
        )

    def _open_cooldown(self, failure_class: FailureClass) -> CircuitTransition:
        self._cooldown_until = self._clock() + self.cooldown_ms / 1000
        return self._set_state(
            CircuitState.OPEN_COOLDOWN,
            failure_class=failure_class,
            cooldown_ms=self.cooldown_ms,
        )

    def _close(self) -> CircuitTransition:
        return self._set_state(CircuitState.CLOSED)

    def _set_state(
        self,
        state: CircuitState,
        *,
        failure_class: FailureClass | None = None,
        cooldown_ms: int | None = None,
        expose_failure_class: bool = True,
    ) -> CircuitTransition:
        previous_state = self.state
        self._generation += 1
        self.state = state
        self.failure_class = failure_class
        if state is not CircuitState.OPEN_COOLDOWN:
            self._cooldown_until = None
        if state is not CircuitState.HALF_OPEN:
            self._probe_token = None
        return CircuitTransition(
            provider_id=self.provider_id,
            previous_state=previous_state,
            state=state,
            generation=self._generation,
            failure_class=failure_class if expose_failure_class else None,
            cooldown_ms=cooldown_ms,
        )

"""Aggregated, path-qualified results for every validation layer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Stable check names and path-qualified validation failures.

    ``errors`` contains display-ready strings so callers can print a report
    without knowing an internal issue type.  ``checks`` records every layer
    that ran, including checks which produced no errors.  The aliases are
    intentionally small conveniences for command boundaries and tests.
    """

    errors: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether all executed checks passed."""

        return not self.errors

    @property
    def valid(self) -> bool:
        """Compatibility alias for :attr:`ok`."""

        return self.ok

    @property
    def passed(self) -> bool:
        """Compatibility alias for :attr:`ok`."""

        return self.ok

    @property
    def failures(self) -> tuple[str, ...]:
        """Compatibility alias for :attr:`errors`."""

        return self.errors

    @property
    def check_names(self) -> tuple[str, ...]:
        """Compatibility alias for :attr:`checks`."""

        return self.checks

    def __bool__(self) -> bool:
        return self.ok


class Collector:
    def __init__(self) -> None:
        self.checks: list[str] = []
        self.errors: list[str] = []

    def begin(self, name: str) -> None:
        if name not in self.checks:
            self.checks.append(name)

    def add(self, check: str, path: Path | str | None, message: str) -> None:
        self.begin(check)
        path_text = str(path) if path is not None else ""
        if path_text:
            self.errors.append(f"{check}: {path_text}: {message}")
        else:
            self.errors.append(f"{check}: {message}")

    def run(
        self,
        check: str,
        path: Path | str | None,
        operation: Callable[[], object],
    ) -> object | None:
        self.begin(check)
        try:
            return operation()
        except Exception as exc:  # validation must aggregate independent layers
            self.add(check, path, str(exc))
            return None

    def report(self) -> ValidationReport:
        return ValidationReport(
            errors=tuple(self.errors),
            checks=tuple(self.checks),
        )

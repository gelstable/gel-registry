"""Registry-owned product and upstream source policy."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from .constants import CLI_PLATFORMS
from .contracts.common import MODEL_CONFIG, StrictString
from .digest import canonical_json

_PRODUCT_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"/[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)
_KNOWN_ADAPTERS = frozenset({"gel-cli"})
_CANONICAL_ENCODINGS = ("identity", "zstd")


class PolicyError(ValueError):
    """Raised when a source policy cannot be loaded or is not canonical."""


class ProductPolicy(BaseModel):
    """Policy governing one product's upstream release source."""

    model_config = MODEL_CONFIG

    product: StrictString
    repository: StrictString
    adapter: StrictString
    channel: Literal["stable"]
    tag_pattern: StrictString
    platforms: tuple[StrictString, ...]
    encodings: tuple[Literal["identity", "zstd"], ...]

    @field_validator("product")
    @classmethod
    def validate_product_id(cls, value: str) -> str:
        if _PRODUCT_ID.fullmatch(value) is None:
            raise ValueError("product must be a lowercase hyphenated identifier")
        return value

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if _REPOSITORY.fullmatch(value) is None:
            raise ValueError("repository must use owner/repository syntax")
        return value

    @field_validator("adapter")
    @classmethod
    def validate_adapter(cls, value: str) -> str:
        if value not in _KNOWN_ADAPTERS:
            raise ValueError(f"unknown policy adapter {value!r}")
        return value

    @field_validator("tag_pattern")
    @classmethod
    def validate_tag_pattern(cls, value: str) -> str:
        if not value.startswith("^") or not value.endswith("$"):
            raise ValueError("tag_pattern must be anchored with ^ and $")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(
                f"tag_pattern is not a valid regular expression: {exc}"
            ) from exc
        return value

    @field_validator("platforms", "encodings")
    @classmethod
    def validate_unique_nonempty_sequence(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not value:
            raise ValueError("policy sequences must not be empty")
        if len(value) != len(set(value)):
            raise ValueError("policy sequences must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_canonical_order(self) -> ProductPolicy:
        if self.encodings != _CANONICAL_ENCODINGS:
            raise ValueError("encodings must use canonical identity, zstd ordering")
        if self.adapter == "gel-cli" and self.platforms != CLI_PLATFORMS:
            raise ValueError("gel-cli platforms must use the CLI platform ordering")
        return self


class SourcePolicy(BaseModel):
    """Canonical policy for every product promoted by the registry."""

    model_config = MODEL_CONFIG

    schema_version: Literal[1] = 1
    products: tuple[ProductPolicy, ...]

    @field_validator("products")
    @classmethod
    def validate_products(
        cls, value: tuple[ProductPolicy, ...]
    ) -> tuple[ProductPolicy, ...]:
        if not value:
            raise ValueError("source policy must contain at least one product")
        products = tuple(item.product for item in value)
        if len(products) != len(set(products)):
            raise ValueError("source policy contains duplicate products")
        if products != tuple(sorted(products)):
            raise ValueError("products must use canonical product ordering")
        return value

    def by_product(self) -> dict[str, ProductPolicy]:
        """Return policies keyed by their registry product identifier."""

        return {item.product: item for item in self.products}

    def repositories(self) -> tuple[str, ...]:
        """Return unique upstream repositories in canonical order."""

        return tuple(sorted({item.repository for item in self.products}))


def load_source_policy(repo: Path) -> SourcePolicy:
    """Load and validate the canonical source policy for a repository checkout."""

    path = repo / "sources" / "products.json"
    try:
        raw = path.read_bytes()
        policy = SourcePolicy.model_validate_json(raw)
    except (OSError, ValidationError, ValueError) as exc:
        raise PolicyError(f"invalid source policy {path}: {exc}") from exc
    if raw != canonical_json(policy):
        raise PolicyError(f"source policy is not canonical: {path}")
    return policy


__all__ = [
    "PolicyError",
    "ProductPolicy",
    "SourcePolicy",
    "load_source_policy",
]

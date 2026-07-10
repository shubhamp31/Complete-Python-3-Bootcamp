"""Marketplace abstraction layer.

Every marketplace (Amazon India today; Flipkart, Myntra, AJIO, Nykaa
Fashion, Tata CLiQ tomorrow) is a :class:`MarketplaceGenerator`. The UI and
CLI only talk to this interface, so adding a marketplace means writing one
new generator class + its JSON configuration - no changes elsewhere.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Type

from core.models import GenerationRequest, GenerationResult

#: Progress callback: (fraction between 0 and 1, human-readable status).
ProgressCallback = Callable[[float, str], None]

_REGISTRY: dict[str, Type["MarketplaceGenerator"]] = {}


class MarketplaceGenerator(ABC):
    """Contract every marketplace listing generator must fulfil."""

    #: Unique registry key, e.g. ``"amazon_in"``.
    marketplace_id: str = ""
    #: Name shown in the UI.
    display_name: str = ""

    @abstractmethod
    def generate(
        self,
        request: GenerationRequest,
        progress: ProgressCallback | None = None,
    ) -> GenerationResult:
        """Run the full conversion for this marketplace.

        Args:
            request: Validated user inputs (source file, template, output dir).
            progress: Optional callback for UI progress updates.

        Returns:
            A :class:`GenerationResult` describing outputs and statistics.
        """


def register(cls: Type[MarketplaceGenerator]) -> Type[MarketplaceGenerator]:
    """Class decorator that adds a generator to the registry."""
    if not cls.marketplace_id:
        raise ValueError(f"{cls.__name__} must define a marketplace_id")
    _REGISTRY[cls.marketplace_id] = cls
    return cls


def get_generator(marketplace_id: str) -> MarketplaceGenerator:
    """Instantiate the generator registered for ``marketplace_id``.

    Raises:
        KeyError: If no generator is registered under that id.
    """
    try:
        return _REGISTRY[marketplace_id]()
    except KeyError:
        raise KeyError(
            f"No marketplace generator registered for '{marketplace_id}'. "
            f"Available: {', '.join(sorted(_REGISTRY)) or 'none'}"
        ) from None


def available_marketplaces() -> dict[str, str]:
    """Return ``{marketplace_id: display_name}`` for all registered generators."""
    return {mid: cls.display_name for mid, cls in _REGISTRY.items()}

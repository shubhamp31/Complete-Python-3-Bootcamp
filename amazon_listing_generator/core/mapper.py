"""Configurable Shopify -> Amazon field mapping engine.

All mappings are declared in ``config/field_mapping.json``; adding or
changing a mapping never requires a code change. Each rule points an
Amazon field at one of five source types (see :class:`core.models.SourceType`):

* ``shopify``  - a raw Shopify export column, e.g. ``"Variant SKU"``.
* ``internal`` - a computed pipeline column, e.g. ``"_seo_title"``.
* ``constant`` - a literal value, e.g. ``"India"``.
* ``default``  - a key from ``defaults.json``, e.g. ``"brand"``.
* ``template`` - a ``str.format`` string over any working columns.
"""

from __future__ import annotations

import string
from typing import Any

import pandas as pd

from core.models import FieldMappingConfig, MappingRule, SourceType
from core.utils import ConfigError, get_logger

logger = get_logger("mapper")


class FieldMapper:
    """Transforms the enriched working table into Amazon field columns."""

    def __init__(self, config: FieldMappingConfig, defaults: dict[str, Any]) -> None:
        self._config = config
        self._defaults = defaults

    @property
    def rules(self) -> list[MappingRule]:
        """All configured mapping rules."""
        return self._config.mappings

    # ------------------------------------------------------------------ #

    def map(self, working: pd.DataFrame) -> pd.DataFrame:
        """Produce a DataFrame with one column per configured Amazon field.

        Args:
            working: The enriched listing table (Shopify columns plus
                internal ``_*`` columns).

        Returns:
            DataFrame indexed like ``working`` whose columns are the
            canonical ``amazon_field`` names, all values as strings.
        """
        out = pd.DataFrame(index=working.index)
        for rule in self._config.mappings:
            try:
                out[rule.amazon_field] = self._resolve(rule, working)
            except ConfigError:
                raise
            except Exception as exc:
                logger.exception("Mapping failed for '%s'", rule.amazon_field)
                raise ConfigError(
                    f"Mapping for '{rule.amazon_field}' failed: {exc}"
                ) from exc
        logger.info("Mapped %d Amazon fields for %d rows", out.shape[1], len(out))
        return out

    # ------------------------------------------------------------------ #

    def _resolve(self, rule: MappingRule, working: pd.DataFrame) -> pd.Series:
        source = rule.source
        if source.type in (SourceType.SHOPIFY, SourceType.INTERNAL):
            if not source.column:
                raise ConfigError(
                    f"Mapping '{rule.amazon_field}': source type "
                    f"'{source.type.value}' requires a 'column'"
                )
            if source.column in working.columns:
                return working[source.column].astype(str).fillna("")
            logger.debug(
                "Column '%s' not present for field '%s'; leaving blank",
                source.column,
                rule.amazon_field,
            )
            return pd.Series([""] * len(working), index=working.index)

        if source.type is SourceType.CONSTANT:
            return pd.Series([source.value or ""] * len(working), index=working.index)

        if source.type is SourceType.DEFAULT:
            if source.key is None:
                raise ConfigError(
                    f"Mapping '{rule.amazon_field}': source type 'default' "
                    "requires a 'key'"
                )
            value = str(self._defaults.get(source.key, ""))
            return pd.Series([value] * len(working), index=working.index)

        if source.type is SourceType.TEMPLATE:
            return self._resolve_template(rule, working)

        raise ConfigError(
            f"Mapping '{rule.amazon_field}': unknown source type '{source.type}'"
        )

    def _resolve_template(
        self, rule: MappingRule, working: pd.DataFrame
    ) -> pd.Series:
        """Render a format-string template against each row."""
        template = rule.source.template or ""
        fields = [
            fname
            for _, fname, _, _ in string.Formatter().parse(template)
            if fname
        ]
        missing = [f for f in fields if f not in working.columns]
        if missing:
            logger.warning(
                "Template for '%s' references missing column(s): %s",
                rule.amazon_field,
                ", ".join(missing),
            )
        columns = {
            f: (
                working[f].astype(str)
                if f in working.columns
                else pd.Series([""] * len(working), index=working.index)
            )
            for f in fields
        }
        rows = pd.DataFrame(columns, index=working.index)
        return pd.Series(
            [template.format(**record) for record in rows.to_dict("records")],
            index=working.index,
        )

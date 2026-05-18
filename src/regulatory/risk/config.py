"""Pydantic models for risk engine configuration (config/risk_rules.yaml).

All rule thresholds live in the YAML; nothing is hardcoded here. The version
string in the YAML is embedded in every risk signal's evidence so historical
signals can be re-explained against the rules that produced them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class RepeatViolatorThreshold(BaseModel):
    """Severity level entry in the repeat-violator threshold table."""

    min_recalls: int
    min_weighted_score: int


class RepeatViolatorConfig(BaseModel):
    """Configuration for the repeat-violator detection rule."""

    window_months: int
    severity_weights: dict[str, int]
    thresholds: dict[str, RepeatViolatorThreshold]


class SupplyChainConfig(BaseModel):
    """Configuration for the supply-chain exposure rule."""

    min_county_share_pct: float
    min_alternative_suppliers_for_low: int
    time_to_expiry_floor_days: int


class CorroborationConfig(BaseModel):
    """Configuration for the cross-source corroboration rule."""

    enable: bool
    jurisdiction_count_for_boost: int
    boost_levels: int


class RiskRulesConfig(BaseModel):
    """Top-level risk rules configuration loaded from config/risk_rules.yaml."""

    version: str
    repeat_violator: RepeatViolatorConfig
    supply_chain_exposure: SupplyChainConfig
    cross_source_corroboration: CorroborationConfig

    @property
    def config_hash(self) -> str:
        """SHA-256 of the serialised config; embedded in signal evidence for staleness checks."""
        raw = self.model_dump()
        return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()


_DEFAULT_PATH = Path(__file__).parents[3] / "config" / "risk_rules.yaml"


def load_config(path: Path | None = None) -> RiskRulesConfig:
    """Load and validate risk rules configuration from a YAML file.

    Args:
        path: Path to the YAML file. Defaults to ``config/risk_rules.yaml``
            relative to the project root.

    Returns:
        A validated :class:`RiskRulesConfig` instance.
    """
    config_path = path or _DEFAULT_PATH
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return RiskRulesConfig.model_validate(raw)


class RiskSignalCandidate(BaseModel):
    """Intermediate model produced by rule detectors before persistence.

    Rule functions return lists of these; the persister merges them into the
    ``risk_signals`` table with dedup/update semantics.
    """

    kind: str
    severity: str
    manufacturer_id: str | None = None
    active_ingredient: str | None = None
    regions_affected: list[str] = Field(default_factory=list)
    exposure_pct: float | None = None
    alternative_supplier_count: int | None = None
    recommended_action: str = ""
    time_to_expiry_days: int | None = None
    evidence_document_ids: list[str] = Field(default_factory=list)
    evidence_supply_ids: list[str] = Field(default_factory=list)
    first_seen_override: str | None = None

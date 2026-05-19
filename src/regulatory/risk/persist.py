"""Risk signal persistence layer.

Merges :class:`~regulatory.risk.config.RiskSignalCandidate` lists from rule
detectors into the ``risk_signals`` table with idempotent upsert semantics:

- If an active signal already exists for the same (kind, manufacturer_id,
  active_ingredient), update last_updated + evidence only if evidence changed.
- If the rule no longer fires for an existing active signal, transition it to
  ``"resolved"``.
- Every state transition writes an append-only row to ``risk_signal_events``.
- ``config_hash`` staleness is surfaced on ``risk list`` (signals whose stored
  config_hash differs from the current hash).

Manufacturer merge semantics (see docs/risk_engine.md):
  Active signals on a source manufacturer are resolved with
  ``reason = "manufacturer_merged"``. On the next risk run, signals are
  re-computed against the merged canonical entity and ``first_seen`` is
  carried forward from the resolved predecessor if it was merged within the
  last 7 days.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone

import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from regulatory.db.models import RiskSignal, RiskSignalEvent
from regulatory.risk.config import RiskRulesConfig, RiskSignalCandidate
from regulatory.risk.corroboration import boost_severity

log = structlog.get_logger(__name__)

_MANUFACTURER_MERGE_CARRY_WINDOW_DAYS = 7


def _signal_state_dict(signal: RiskSignal) -> dict[str, object]:
    """Serialise a signal's mutable fields into a dict for audit events."""
    return {
        "kind": signal.kind,
        "severity": signal.severity,
        "status": signal.status,
        "manufacturer_id": str(signal.manufacturer_id) if signal.manufacturer_id else None,
        "active_ingredient": signal.active_ingredient,
        "regions_affected": list(signal.regions_affected or []),
        "exposure_pct": str(signal.exposure_pct) if signal.exposure_pct is not None else None,
        "alternative_supplier_count": signal.alternative_supplier_count,
        "recommended_action": signal.recommended_action,
        "evidence": dict(signal.evidence or {}),
        "last_updated": signal.last_updated.isoformat() if signal.last_updated else None,
    }


def _build_evidence(
    candidate: RiskSignalCandidate,
    config: RiskRulesConfig,
    as_of: date,
) -> dict[str, object]:
    """Build the ``evidence`` JSONB blob for a signal."""
    # Preserve order while deduplicating document IDs (corroboration can produce duplicates
    # if a document lists the same active ingredient more than once).
    deduped_doc_ids = list(dict.fromkeys(candidate.evidence_document_ids))

    evidence: dict[str, object] = {
        "document_ids": deduped_doc_ids,
        "supply_ids": list(candidate.evidence_supply_ids),
        "rule_version": config.version,
        "config_hash": config.config_hash,
        "computed_at": datetime.now(tz=timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "inputs": {
            "manufacturer_id": candidate.manufacturer_id,
            "active_ingredient": candidate.active_ingredient,
            "kind": candidate.kind,
        },
    }

    if candidate.kind == "repeat_violator" and candidate.recall_count is not None:
        evidence["recall_count"] = candidate.recall_count
        evidence["weighted_score"] = candidate.weighted_score

    if candidate.kind == "cross_source_corroboration":
        evidence["jurisdictions"] = sorted(candidate.regions_affected)

    if candidate.kind == "supply_chain_exposure":
        evidence["data_provenance"] = {
            "supply_chain_source": "synthetic_v2",
            "caveat": (
                "Supply-chain figures derived from synthetic procurement data. "
                "Real KEMSA/county procurement integration pending."
            ),
        }

    return evidence


def _supply_chain_action(kind: str, action: str) -> str:
    """Append a synthetic-data disclaimer to supply_chain_exposure recommended_action."""
    if kind != "supply_chain_exposure":
        return action
    suffix = " (based on synthetic supply data)"
    if action.endswith(suffix):
        return action
    return action.rstrip() + suffix


def _evidence_changed(existing: dict[str, object], new: dict[str, object]) -> bool:
    """Return True if the evidence payload has materially changed.

    Ignores ``computed_at`` (always changes) and compares document/supply IDs
    and rule inputs.
    """

    def _stripped(e: dict[str, object]) -> dict[str, object]:
        return {k: v for k, v in e.items() if k != "computed_at"}

    return json.dumps(_stripped(existing), sort_keys=True, default=str) != json.dumps(
        _stripped(new), sort_keys=True, default=str
    )


async def _find_predecessor_first_seen(  # pragma: no cover
    session: AsyncSession,
    kind: str,
    manufacturer_id: str | None,
    active_ingredient: str | None,
) -> datetime | None:
    """Look for a recently-resolved predecessor signal to carry first_seen forward.

    Manufacturer merge semantics: if a signal of the same (kind, active_ingredient)
    shape was resolved with reason='manufacturer_merged' within the last 7 days,
    carry its first_seen to the new signal.

    Args:
        session: Async SQLAlchemy session.
        kind: Signal kind.
        manufacturer_id: UUID string of the current (merged) manufacturer.
        active_ingredient: Active ingredient string.

    Returns:
        ``first_seen`` datetime to carry forward, or ``None`` if no predecessor found.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=_MANUFACTURER_MERGE_CARRY_WINDOW_DAYS)
    result = await session.execute(
        select(RiskSignal).where(
            and_(
                RiskSignal.kind == kind,
                RiskSignal.active_ingredient == active_ingredient,
                RiskSignal.status == "resolved",
                RiskSignal.resolved_at >= cutoff,
            )
        )
    )
    predecessors = result.scalars().all()

    for pred in predecessors:
        events = await session.execute(
            select(RiskSignalEvent).where(
                and_(
                    RiskSignalEvent.signal_id == pred.id,
                    RiskSignalEvent.event_type == "resolved",
                    RiskSignalEvent.reason == "manufacturer_merged",
                )
            )
        )
        if events.first() is not None:
            return pred.first_seen

    return None


async def _write_event(
    session: AsyncSession,
    signal: RiskSignal,
    event_type: str,
    old_state: dict[str, object] | None,
    new_state: dict[str, object],
    actor: str,
    reason: str | None = None,
) -> None:
    """Append a state-transition row to ``risk_signal_events``."""
    event = RiskSignalEvent(
        signal_id=signal.id,
        event_type=event_type,
        old_state=old_state,
        new_state=new_state,
        actor=actor,
        reason=reason,
    )
    session.add(event)


async def persist_signals(
    session: AsyncSession,
    candidates: Sequence[RiskSignalCandidate],
    *,
    config: RiskRulesConfig,
    as_of: date,
    corroboration_candidates: Sequence[RiskSignalCandidate] | None = None,
    dry_run: bool = False,
    actor: str | None = None,
) -> dict[str, int]:
    """Persist risk signal candidates into the ``risk_signals`` table.

    Applies idempotent upsert semantics and corroboration severity boosts.

    Args:
        session: Async SQLAlchemy session.
        candidates: Rule-generated signal candidates (repeat_violator + supply_chain).
        config: Current risk rules config (for evidence metadata).
        as_of: Computation date.
        corroboration_candidates: Cross-source corroboration candidates; their
            severity boost is applied to any related signal before persist.
        dry_run: If True, compute but do not write to the database.
        actor: Identity string for audit events. Defaults to ``$USER`` or ``"system"``.

    Returns:
        Dict with counts: ``created``, ``updated``, ``resolved``, ``unchanged``.
    """
    _actor = actor or os.environ.get("USER", "system")
    corr_ingredients: set[str] = set()
    if corroboration_candidates:
        for c in corroboration_candidates:
            if c.active_ingredient:
                corr_ingredients.add(c.active_ingredient)

    counts = {"created": 0, "updated": 0, "resolved": 0, "unchanged": 0}

    # Build a lookup of current active signals to detect which ones no longer fire.
    existing_result = await session.execute(select(RiskSignal).where(RiskSignal.status == "active"))
    existing_active: dict[str, RiskSignal] = {}
    for sig in existing_result.scalars().all():
        key = _signal_key(sig.kind, sig.manufacturer_id, sig.active_ingredient)
        existing_active[key] = sig

    fired_keys: set[str] = set()

    all_candidates = list(candidates)
    if corroboration_candidates:
        all_candidates.extend(corroboration_candidates)

    for candidate in all_candidates:
        # Apply corroboration boost if this ingredient has cross-source corroboration.
        severity = candidate.severity
        if (
            candidate.active_ingredient
            and candidate.active_ingredient in corr_ingredients
            and candidate.kind != "cross_source_corroboration"
            and corroboration_candidates
        ):
            boost_steps = max(
                (c.severity == "medium" and 1 or 0)
                for c in (corroboration_candidates or [])
                if c.active_ingredient == candidate.active_ingredient
            )
            if boost_steps:
                severity = boost_severity(severity, boost_steps)

        mfr_uuid: uuid.UUID | None = None
        if candidate.manufacturer_id:
            try:
                mfr_uuid = uuid.UUID(candidate.manufacturer_id)
            except ValueError:
                log.warning("persist_invalid_manufacturer_id", value=candidate.manufacturer_id)

        key = _signal_key(candidate.kind, mfr_uuid, candidate.active_ingredient)
        fired_keys.add(key)

        evidence = _build_evidence(candidate, config, as_of)

        if key in existing_active:
            existing_sig = existing_active[key]
            old_state = _signal_state_dict(existing_sig)

            # Check if anything material changed.
            changed = (
                existing_sig.severity != severity
                or existing_sig.regions_affected != candidate.regions_affected
                or _evidence_changed(dict(existing_sig.evidence or {}), evidence)
            )

            if not changed:
                counts["unchanged"] += 1
                continue

            if not dry_run:
                existing_sig.severity = severity
                existing_sig.regions_affected = candidate.regions_affected
                existing_sig.exposure_pct = (
                    candidate.exposure_pct  # type: ignore[assignment]
                )
                existing_sig.alternative_supplier_count = candidate.alternative_supplier_count
                existing_sig.recommended_action = _supply_chain_action(
                    candidate.kind, candidate.recommended_action or ""
                )
                existing_sig.time_to_expiry_days = candidate.time_to_expiry_days
                existing_sig.evidence = evidence
                existing_sig.last_updated = datetime.now(tz=timezone.utc)
                new_state = _signal_state_dict(existing_sig)
                await _write_event(session, existing_sig, "updated", old_state, new_state, _actor)
            counts["updated"] += 1

        else:
            # New signal — check for manufacturer-merge carry-forward.
            first_seen_override: datetime | None = None
            if candidate.first_seen_override:
                with contextlib.suppress(ValueError):
                    first_seen_override = datetime.fromisoformat(candidate.first_seen_override)
            if first_seen_override is None:
                first_seen_override = await _find_predecessor_first_seen(
                    session,
                    candidate.kind,
                    candidate.manufacturer_id,
                    candidate.active_ingredient,
                )

            now_utc = datetime.now(tz=timezone.utc)
            if not dry_run:
                new_sig = RiskSignal(
                    id=uuid.uuid4(),
                    kind=candidate.kind,
                    severity=severity,
                    status="active",
                    manufacturer_id=mfr_uuid,
                    active_ingredient=candidate.active_ingredient,
                    regions_affected=candidate.regions_affected,
                    exposure_pct=candidate.exposure_pct,
                    alternative_supplier_count=candidate.alternative_supplier_count,
                    recommended_action=_supply_chain_action(
                        candidate.kind, candidate.recommended_action or ""
                    ),
                    time_to_expiry_days=candidate.time_to_expiry_days,
                    evidence=evidence,
                    first_seen=first_seen_override or now_utc,
                    last_updated=now_utc,
                )
                session.add(new_sig)
                await session.flush()
                await _write_event(
                    session, new_sig, "created", None, _signal_state_dict(new_sig), _actor
                )
            counts["created"] += 1

    # Resolve active signals whose rule no longer fires.
    for key, existing_sig in existing_active.items():
        if key in fired_keys:
            continue
        if not dry_run:
            old_state = _signal_state_dict(existing_sig)
            existing_sig.status = "resolved"
            existing_sig.resolved_at = datetime.now(tz=timezone.utc)
            existing_sig.last_updated = datetime.now(tz=timezone.utc)
            new_state = _signal_state_dict(existing_sig)
            await _write_event(session, existing_sig, "resolved", old_state, new_state, _actor)
        counts["resolved"] += 1

    if not dry_run:
        await session.commit()

    log.info("persist_signals_complete", **counts, dry_run=dry_run)
    return counts


async def resolve_signal_for_manufacturer_merge(
    session: AsyncSession,
    manufacturer_id: uuid.UUID,
    actor: str = "system",
) -> int:
    """Resolve all active signals for a manufacturer being merged into another.

    Called by the merge CLI before the source manufacturer row is deleted.

    Args:
        session: Async SQLAlchemy session.
        manufacturer_id: UUID of the manufacturer being absorbed.
        actor: Actor identity for audit.

    Returns:
        Number of signals resolved.
    """
    result = await session.execute(
        select(RiskSignal).where(
            and_(
                RiskSignal.manufacturer_id == manufacturer_id,
                RiskSignal.status == "active",
            )
        )
    )
    signals = result.scalars().all()
    now_utc = datetime.now(tz=timezone.utc)
    count = 0
    for sig in signals:
        old_state = _signal_state_dict(sig)
        sig.status = "resolved"
        sig.resolved_at = now_utc
        sig.last_updated = now_utc
        new_state = _signal_state_dict(sig)
        await _write_event(
            session, sig, "resolved", old_state, new_state, actor, reason="manufacturer_merged"
        )
        count += 1
    await session.flush()
    log.info("signals_resolved_for_merge", manufacturer_id=str(manufacturer_id), count=count)
    return count


async def transition_signal(  # pragma: no cover
    session: AsyncSession,
    signal_id: uuid.UUID,
    new_status: str,
    *,
    actor: str,
    reason: str,
) -> None:
    """Transition a signal to a new status (``"resolved"`` or ``"suppressed"``).

    Args:
        session: Async SQLAlchemy session.
        signal_id: UUID of the signal to transition.
        new_status: Target status string.
        actor: CLI user identity.
        reason: Human-readable justification.
    """
    signal = await session.get(RiskSignal, signal_id)
    if signal is None:
        raise ValueError(f"Signal not found: {signal_id}")
    old_state = _signal_state_dict(signal)
    signal.status = new_status
    signal.last_updated = datetime.now(tz=timezone.utc)
    if new_status in ("resolved", "suppressed"):
        signal.resolved_at = datetime.now(tz=timezone.utc)
    new_state = _signal_state_dict(signal)
    await _write_event(session, signal, new_status, old_state, new_state, actor, reason)
    await session.commit()


def explain_signal(signal: RiskSignal) -> str:
    """Produce a deterministic plain-text trace for a risk signal.

    Args:
        signal: ORM signal instance with evidence loaded.

    Returns:
        Multi-line explanation string. Deterministic across runs for the same input.
    """
    evidence = dict(signal.evidence or {})
    lines = [
        f"Signal ID : {signal.id}",
        f"Kind      : {signal.kind}",
        f"Severity  : {signal.severity}",
        f"Status    : {signal.status}",
        f"Ingredient: {signal.active_ingredient or '(none)'}",
        f"Regions   : {', '.join(signal.regions_affected or []) or '(none)'}",
        f"First seen: {signal.first_seen.isoformat() if signal.first_seen else '(unknown)'}",
        f"Updated   : {signal.last_updated.isoformat() if signal.last_updated else '(unknown)'}",
        "",
        "Rule",
        f"  Version    : {evidence.get('rule_version', '(unknown)')}",
        f"  Config hash: {str(evidence.get('config_hash', ''))[:16]}...",
        f"  Computed at: {evidence.get('computed_at', '(unknown)')}",
        f"  As of      : {evidence.get('as_of', '(unknown)')}",
        "",
        "Evidence",
    ]
    doc_ids = sorted(str(d) for d in evidence.get("document_ids", []))
    for doc_id in doc_ids:
        lines.append(f"  Document: {doc_id}")
    supply_ids = sorted(str(s) for s in evidence.get("supply_ids", []))
    for supply_id in supply_ids:
        lines.append(f"  Supply  : {supply_id}")
    if signal.recommended_action:
        lines += ["", "Recommendation", f"  {signal.recommended_action}"]
    return "\n".join(lines)


def _signal_key(
    kind: str,
    manufacturer_id: uuid.UUID | str | None,
    active_ingredient: str | None,
) -> str:
    """Stable string key for deduplicating active signals."""
    return f"{kind}|{manufacturer_id or ''}|{active_ingredient or ''}"

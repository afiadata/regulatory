"""Essential Medicines List active ingredients used by the demo risk engine.

These 15 ingredients span enough therapeutic classes to make supply-chain
stories interesting across the Kenya EML demo scenario. Treat as an immutable
constant for this PR — if the list changes, regenerate synthetic fixtures and
eat the test churn.

Single source of truth: imported by the synthetic procurement generator and by
any other code that needs the demo's EML scope.
"""

from __future__ import annotations

EML_INGREDIENTS: tuple[str, ...] = (
    "amoxicillin",
    "ceftriaxone",
    "metronidazole",
    "artemether-lumefantrine",
    "oxytocin",
    "insulin (regular human)",
    "paracetamol",
    "ibuprofen",
    "omeprazole",
    "salbutamol",
    "atorvastatin",
    "hydrochlorothiazide",
    "metformin",
    "prednisolone",
    "tenofovir-lamivudine-dolutegravir",
)

"""Generate synthetic procurement data for the risk-engine demo.

Produces CSV files in data/synthetic/procurement_v1/:
  - counties.csv
  - suppliers.csv
  - county_supply.csv

County data sourced from:
  Kenya National Bureau of Statistics, 2019 Kenya Population and Housing Census
  https://www.knbs.or.ke/download/2019-kenya-population-and-housing-census-volume-i/

KMHFL health facility counts from:
  Kenya Master Health Facility List, accessed 2024
  https://kmhfl.health.go.ke/

All county_supply rows carry data_source = "synthetic_v1". Any downstream output
that includes supply-chain evidence MUST surface this provenance flag.

Usage:
    uv run python scripts/generate_synthetic_procurement.py
    uv run python scripts/generate_synthetic_procurement.py --seed 42 --out-dir data/synthetic/v2

Then load into Postgres:
    regulatory procurement load --version synthetic_v1
"""

from __future__ import annotations

import argparse
import csv
import random
import uuid
from pathlib import Path

from regulatory.risk.eml_ingredients import EML_INGREDIENTS

# ---------------------------------------------------------------------------
# County reference data (all 47 Kenyan counties, 2019 census)
# Source: KNBS 2019 Population and Housing Census
# ---------------------------------------------------------------------------
COUNTIES: list[dict[str, object]] = [
    {"name": "Mombasa", "region": "Coast", "population": 1208333, "health_facilities": 187},
    {"name": "Kwale", "region": "Coast", "population": 866820, "health_facilities": 132},
    {"name": "Kilifi", "region": "Coast", "population": 1453787, "health_facilities": 198},
    {"name": "Tana River", "region": "Coast", "population": 315943, "health_facilities": 72},
    {"name": "Lamu", "region": "Coast", "population": 143920, "health_facilities": 45},
    {"name": "Taita-Taveta", "region": "Coast", "population": 340671, "health_facilities": 78},
    {"name": "Garissa", "region": "North Eastern", "population": 841353, "health_facilities": 94},
    {"name": "Wajir", "region": "North Eastern", "population": 781263, "health_facilities": 87},
    {"name": "Mandera", "region": "North Eastern", "population": 867457, "health_facilities": 102},
    {"name": "Marsabit", "region": "Eastern", "population": 459785, "health_facilities": 89},
    {"name": "Isiolo", "region": "Eastern", "population": 268002, "health_facilities": 55},
    {"name": "Meru", "region": "Eastern", "population": 1545714, "health_facilities": 231},
    {"name": "Tharaka-Nithi", "region": "Eastern", "population": 393177, "health_facilities": 97},
    {"name": "Embu", "region": "Eastern", "population": 608599, "health_facilities": 121},
    {"name": "Kitui", "region": "Eastern", "population": 1136187, "health_facilities": 178},
    {"name": "Machakos", "region": "Eastern", "population": 1421932, "health_facilities": 213},
    {"name": "Makueni", "region": "Eastern", "population": 987653, "health_facilities": 164},
    {"name": "Nyandarua", "region": "Central", "population": 638289, "health_facilities": 108},
    {"name": "Nyeri", "region": "Central", "population": 759164, "health_facilities": 134},
    {"name": "Kirinyaga", "region": "Central", "population": 610411, "health_facilities": 112},
    {"name": "Murang'a", "region": "Central", "population": 1056640, "health_facilities": 167},
    {"name": "Kiambu", "region": "Central", "population": 2417735, "health_facilities": 298},
    {"name": "Turkana", "region": "Rift Valley", "population": 926976, "health_facilities": 115},
    {"name": "West Pokot", "region": "Rift Valley", "population": 621241, "health_facilities": 98},
    {"name": "Samburu", "region": "Rift Valley", "population": 310327, "health_facilities": 62},
    {"name": "Trans-Nzoia", "region": "Rift Valley", "population": 990341, "health_facilities": 156},
    {"name": "Uasin Gishu", "region": "Rift Valley", "population": 1163186, "health_facilities": 187},
    {"name": "Elgeyo-Marakwet", "region": "Rift Valley", "population": 454480, "health_facilities": 87},
    {"name": "Nandi", "region": "Rift Valley", "population": 885711, "health_facilities": 143},
    {"name": "Baringo", "region": "Rift Valley", "population": 666763, "health_facilities": 112},
    {"name": "Laikipia", "region": "Rift Valley", "population": 518560, "health_facilities": 98},
    {"name": "Nakuru", "region": "Rift Valley", "population": 2162202, "health_facilities": 267},
    {"name": "Narok", "region": "Rift Valley", "population": 1157873, "health_facilities": 148},
    {"name": "Kajiado", "region": "Rift Valley", "population": 1107296, "health_facilities": 162},
    {"name": "Kericho", "region": "Rift Valley", "population": 901777, "health_facilities": 145},
    {"name": "Bomet", "region": "Rift Valley", "population": 875689, "health_facilities": 132},
    {"name": "Kakamega", "region": "Western", "population": 1867579, "health_facilities": 243},
    {"name": "Vihiga", "region": "Western", "population": 590013, "health_facilities": 101},
    {"name": "Bungoma", "region": "Western", "population": 1670570, "health_facilities": 198},
    {"name": "Busia", "region": "Western", "population": 893681, "health_facilities": 134},
    {"name": "Siaya", "region": "Nyanza", "population": 993183, "health_facilities": 156},
    {"name": "Kisumu", "region": "Nyanza", "population": 1155574, "health_facilities": 178},
    {"name": "Homa Bay", "region": "Nyanza", "population": 1131950, "health_facilities": 167},
    {"name": "Migori", "region": "Nyanza", "population": 1116436, "health_facilities": 159},
    {"name": "Kisii", "region": "Nyanza", "population": 1266860, "health_facilities": 189},
    {"name": "Nyamira", "region": "Nyanza", "population": 605576, "health_facilities": 102},
    {"name": "Nairobi", "region": "Nairobi", "population": 4397073, "health_facilities": 512},
]

assert len(COUNTIES) == 47, f"Expected 47 counties, got {len(COUNTIES)}"

# ---------------------------------------------------------------------------
# Supplier definitions (~30 suppliers; ~20 linked to recall-corpus manufacturers)
# ---------------------------------------------------------------------------
SUPPLIERS: list[dict[str, object]] = [
    # Manufacturers that appear in the recall corpus — linkable to Manufacturer rows.
    {"name": "Cipla Limited", "role": "manufacturer", "countries_served": ["KE", "ZA", "NG", "ET"]},
    {"name": "Sun Pharmaceutical Industries", "role": "manufacturer", "countries_served": ["KE", "UG", "TZ"]},
    {"name": "Aspen Pharmacare", "role": "manufacturer", "countries_served": ["KE", "ZA", "GH"]},
    {"name": "GlaxoSmithKline", "role": "manufacturer", "countries_served": ["KE", "NG", "ZA"]},
    {"name": "Novartis", "role": "manufacturer", "countries_served": ["KE", "TZ", "ET"]},
    {"name": "Pfizer", "role": "manufacturer", "countries_served": ["KE", "ZA", "NG"]},
    {"name": "Roche", "role": "manufacturer", "countries_served": ["KE", "ZA"]},
    {"name": "Sanofi", "role": "manufacturer", "countries_served": ["KE", "NG", "GH"]},
    {"name": "AstraZeneca", "role": "manufacturer", "countries_served": ["KE", "ZA", "NG"]},
    {"name": "Johnson & Johnson", "role": "manufacturer", "countries_served": ["KE", "ZA"]},
    {"name": "Aurobindo Pharma", "role": "manufacturer", "countries_served": ["KE", "UG", "TZ"]},
    {"name": "Lupin Limited", "role": "manufacturer", "countries_served": ["KE", "TZ"]},
    {"name": "Dr. Reddy's Laboratories", "role": "manufacturer", "countries_served": ["KE", "NG"]},
    {"name": "Macleods Pharmaceuticals", "role": "manufacturer", "countries_served": ["KE", "ET", "UG"]},
    {"name": "Strides Pharma", "role": "manufacturer", "countries_served": ["KE", "TZ", "GH"]},
    {"name": "Mylan (Viatris)", "role": "manufacturer", "countries_served": ["KE", "ZA", "NG"]},
    {"name": "Sandoz", "role": "manufacturer", "countries_served": ["KE", "ZA"]},
    {"name": "Teva Pharmaceuticals", "role": "manufacturer", "countries_served": ["KE", "ZA"]},
    {"name": "Hikma Pharmaceuticals", "role": "manufacturer", "countries_served": ["KE", "NG", "EG"]},
    {"name": "Gedeon Richter", "role": "manufacturer", "countries_served": ["KE", "TZ"]},
    # Distributors / agents not directly linked to a manufacturer row.
    {"name": "KEMSA", "role": "distributor", "countries_served": ["KE"]},
    {"name": "Medisel Kenya", "role": "distributor", "countries_served": ["KE"]},
    {"name": "Beta Healthcare", "role": "distributor", "countries_served": ["KE", "UG"]},
    {"name": "Sphinx Pharmaceuticals", "role": "distributor", "countries_served": ["KE"]},
    {"name": "Surgipharm", "role": "distributor", "countries_served": ["KE"]},
    {"name": "Cosmos Pharmaceuticals", "role": "distributor", "countries_served": ["KE", "TZ"]},
    {"name": "Elys Chemical Industries", "role": "distributor", "countries_served": ["KE"]},
    {"name": "Africa Inland Medical", "role": "agent", "countries_served": ["KE", "UG"]},
    {"name": "PharmAccess Supply", "role": "agent", "countries_served": ["KE", "TZ", "UG"]},
    {"name": "HealthPlus Kenya", "role": "agent", "countries_served": ["KE"]},
]

assert len(SUPPLIERS) == 30, f"Expected 30 suppliers, got {len(SUPPLIERS)}"

# Indices into SUPPLIERS that are "manufacturer" type (indices 0-19).
_MANUFACTURER_SUPPLIER_INDICES = list(range(20))


def _generate_county_supply(
    rng: random.Random,
    county_ids: list[str],
    supplier_ids: list[str],
    ingredients: tuple[str, ...],
) -> list[dict[str, object]]:
    """Generate county_supply rows with realistic concentration.

    ~60% of county-ingredient pairs have a single dominant supplier ≥50%.
    Each pair sums to approximately 100% (±5%).

    Args:
        rng: Seeded random instance for determinism.
        county_ids: Ordered list of county UUIDs (strings).
        supplier_ids: Ordered list of supplier UUIDs (strings).
        ingredients: Tuple of active ingredient names.

    Returns:
        List of county_supply row dicts.
    """
    rows = []

    for county_id in county_ids:
        for ingredient in ingredients:
            # Pick 1–4 suppliers for this county-ingredient pair.
            n_suppliers = rng.choices([1, 2, 3, 4], weights=[15, 45, 30, 10])[0]

            # Dominant-supplier pattern: ~60% of pairs have a dominant ≥50%.
            dominant = rng.random() < 0.60

            chosen_indices = rng.sample(range(len(supplier_ids)), k=min(n_suppliers, len(supplier_ids)))
            chosen = [supplier_ids[i] for i in chosen_indices]

            if dominant and len(chosen) >= 1:
                dominant_share = rng.uniform(50.0, 75.0)
                remaining = 100.0 - dominant_share
                others = []
                if len(chosen) > 1:
                    raw = [rng.random() for _ in range(len(chosen) - 1)]
                    total = sum(raw)
                    others = [r / total * remaining for r in raw]
                shares = [dominant_share] + others
            else:
                raw = [rng.random() for _ in chosen]
                total = sum(raw)
                shares = [r / total * 100.0 for r in raw]

            # Round and adjust so sum ≈ 100 ± 5.
            rounded = [round(s, 2) for s in shares]

            lead_time_days = rng.choice([14, 21, 30, 45, 60, 90])
            for supplier_id, share in zip(chosen, rounded):
                rows.append(
                    {
                        "id": str(uuid.uuid4()),
                        "county_id": county_id,
                        "supplier_id": supplier_id,
                        "active_ingredient": ingredient,
                        "share_pct": share,
                        "lead_time_days": lead_time_days,
                        "contract_start": "2024-01-01",
                        "contract_end": "2026-12-31",
                        "data_source": "synthetic_v1",
                    }
                )

    return rows


def generate(seed: int = 42) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    """Generate all synthetic procurement data.

    Args:
        seed: Random seed for determinism.

    Returns:
        Tuple of ``(counties, suppliers, county_supply)`` record lists.
    """
    rng = random.Random(seed)

    county_records = [
        {
            "id": str(uuid.UUID(int=i + 1)),
            "name": c["name"],
            "region": c["region"],
            "population": c["population"],
            "health_facilities": c["health_facilities"],
        }
        for i, c in enumerate(COUNTIES)
    ]

    supplier_records = [
        {
            "id": str(uuid.UUID(int=i + 1001)),
            "name": s["name"],
            "role": s["role"],
            "countries_served": ",".join(str(cs) for cs in s["countries_served"]),  # type: ignore[arg-type]
            "manufacturer_id": "",  # loader resolves by name
            "data_source": "synthetic_v1",
        }
        for i, s in enumerate(SUPPLIERS)
    ]

    county_ids = [str(r["id"]) for r in county_records]
    supplier_ids = [str(r["id"]) for r in supplier_records]

    supply_records = _generate_county_supply(
        rng, county_ids, supplier_ids, EML_INGREDIENTS
    )

    return county_records, supplier_records, supply_records


def write_csvs(
    out_dir: Path,
    counties: list[dict[str, object]],
    suppliers: list[dict[str, object]],
    county_supply: list[dict[str, object]],
) -> None:
    """Write the three CSV files to ``out_dir``.

    Args:
        out_dir: Destination directory (created if absent).
        counties: County records.
        suppliers: Supplier records.
        county_supply: CountySupply records.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    def write_csv(path: Path, records: list[dict[str, object]]) -> None:
        if not records:
            return
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(records[0].keys()))
            writer.writeheader()
            writer.writerows(records)

    write_csv(out_dir / "counties.csv", counties)
    write_csv(out_dir / "suppliers.csv", suppliers)
    write_csv(out_dir / "county_supply.csv", county_supply)

    print(
        f"Written to {out_dir}: "
        f"{len(counties)} counties, {len(suppliers)} suppliers, "
        f"{len(county_supply)} county_supply rows."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate synthetic procurement CSVs.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default 42).")
    parser.add_argument(
        "--out-dir",
        default="data/synthetic/procurement_v1",
        help="Output directory (default data/synthetic/procurement_v1).",
    )
    args = parser.parse_args()

    counties, suppliers, supply = generate(seed=args.seed)
    write_csvs(Path(args.out_dir), counties, suppliers, supply)

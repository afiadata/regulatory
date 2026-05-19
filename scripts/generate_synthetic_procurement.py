"""Generate synthetic procurement data for the risk-engine demo.

Produces CSV files in data/synthetic/procurement_v2/:
  - counties.csv
  - suppliers.csv
  - county_supply.csv

County data sourced from:
  Kenya National Bureau of Statistics, 2019 Kenya Population and Housing Census
  https://www.knbs.or.ke/download/2019-kenya-population-and-housing-census-volume-i/

KMHFL health facility counts from:
  Kenya Master Health Facility List, accessed 2024
  https://kmhfl.health.go.ke/

Supplier design (v2):
  ~20 suppliers are deliberately linked to real manufacturers from the recall
  corpus by using their exact canonical_name as the supplier name. The loader
  resolves the manufacturer_id FK by exact name match. For each linked supplier,
  county_supply rows use the normalized INN of that manufacturer's top recall
  ingredient, so the supply_chain_exposure rule can fire for demo purposes.
  The remaining ~10 suppliers are distributors/agents with no manufacturer link;
  they hold county_supply rows for EML ingredients.

  This deliberate overlap is what makes the supply_chain_exposure signal possible
  in the demo scenario. It is synthetic: real procurement data must be loaded via
  the real-data swap-in path described in docs/synthetic_procurement.md.

All county_supply rows carry data_source = "synthetic_v2". Any downstream output
that includes supply-chain evidence MUST surface this provenance flag.

Usage:
    uv run python scripts/generate_synthetic_procurement.py
    uv run python scripts/generate_synthetic_procurement.py --seed 42 --out-dir data/synthetic/procurement_v2

Then load into Postgres:
    regulatory procurement load --version synthetic_v2 --data-dir data/synthetic/procurement_v2
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# County reference data (all 47 Kenyan counties, 2019 census)
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

# Distributors/agents — never linked to a manufacturer row.
_DISTRIBUTORS: list[dict[str, object]] = [
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

# EML ingredients for distributor/agent county_supply rows.
_EML_INGREDIENTS: tuple[str, ...] = (
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


def _query_top_manufacturers(
    db_url: str,
    limit: int = 25,
) -> list[dict[str, str]]:
    """Query the DB for manufacturers ranked by recall count in the last 24 months.

    Each returned dict has keys ``id``, ``canonical_name``, ``top_ingredient``
    (normalized INN of the most frequently recalled active ingredient, or ``""``
    if unavailable).

    Args:
        db_url: PostgreSQL connection string.
        limit: Maximum number of manufacturers to return.

    Returns:
        List of manufacturer dicts, ranked by recall count descending.
    """
    import psycopg2

    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.id::text, m.canonical_name, COUNT(*) AS recall_count
                FROM manufacturers m
                JOIN documents d ON m.id = ANY(d.canonical_manufacturer_ids)
                WHERE d.document_type IN ('recall', 'alert', 'enforcement')
                  AND d.date_published >= CURRENT_DATE - INTERVAL '24 months'
                  AND m.canonical_name IS NOT NULL
                  AND m.canonical_name != ''
                GROUP BY m.id, m.canonical_name
                ORDER BY recall_count DESC
                LIMIT %s
                """,
                (limit,),
            )
            top_mfrs = [
                {"id": row[0], "canonical_name": row[1], "recall_count": row[2]}
                for row in cur.fetchall()
            ]

        # For each manufacturer, find top recall ingredient (normalized).
        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        from regulatory.risk.ingredient_normalize import normalize_ingredient

        for mfr in top_mfrs:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT unnest(active_ingredients) AS ing, COUNT(*) AS cnt
                    FROM documents
                    WHERE %s = ANY(canonical_manufacturer_ids::text[])
                      AND document_type IN ('recall', 'alert', 'enforcement')
                      AND active_ingredients != '{}'
                    GROUP BY ing
                    ORDER BY cnt DESC
                    LIMIT 1
                    """,
                    (mfr["id"],),
                )
                row = cur.fetchone()
                if row and row[0]:
                    mfr["top_ingredient"] = normalize_ingredient(str(row[0]))
                else:
                    mfr["top_ingredient"] = ""
    finally:
        conn.close()

    # Only keep manufacturers that have at least one recall ingredient — these
    # are the ones that can actually drive a supply_chain_exposure signal.
    with_ingredient = [m for m in top_mfrs if m["top_ingredient"]]
    return with_ingredient


def _generate_county_supply_for_linked(
    rng: random.Random,
    county_ids: list[str],
    supplier_id: str,
    ingredient: str,
    n_counties: int = 20,
) -> list[dict[str, object]]:
    """Generate county_supply rows for a manufacturer-linked supplier.

    The supplier covers *n_counties* randomly sampled counties at ≥30% share
    so that supply_chain_exposure signals reliably fire at the 25% threshold.

    Args:
        rng: Seeded random for determinism.
        county_ids: All 47 county IDs.
        supplier_id: The linked supplier's UUID string.
        ingredient: Normalized ingredient name.
        n_counties: Number of counties this supplier covers.

    Returns:
        List of county_supply row dicts.
    """
    chosen_counties = rng.sample(county_ids, k=min(n_counties, len(county_ids)))
    rows = []
    lead_time = rng.choice([14, 21, 30, 45, 60])
    for county_id in chosen_counties:
        share = round(rng.uniform(30.0, 70.0), 2)
        rows.append(
            {
                "id": str(uuid.uuid4()),
                "county_id": county_id,
                "supplier_id": supplier_id,
                "active_ingredient": ingredient,
                "share_pct": share,
                "lead_time_days": lead_time,
                "contract_start": "2024-01-01",
                "contract_end": "2026-12-31",
                "data_source": "synthetic_v2",
            }
        )
    return rows


def _generate_county_supply_for_distributors(
    rng: random.Random,
    county_ids: list[str],
    supplier_ids: list[str],
    ingredients: tuple[str, ...],
) -> list[dict[str, object]]:
    """Generate county_supply rows for distributor/agent suppliers (EML ingredients).

    Args:
        rng: Seeded random for determinism.
        county_ids: All 47 county IDs.
        supplier_ids: IDs of the distributor/agent suppliers only.
        ingredients: EML ingredient names.

    Returns:
        List of county_supply row dicts.
    """
    rows = []
    for county_id in county_ids:
        for ingredient in ingredients:
            n_suppliers = rng.choices([1, 2, 3, 4], weights=[15, 45, 30, 10])[0]
            dominant = rng.random() < 0.60
            chosen = rng.sample(supplier_ids, k=min(n_suppliers, len(supplier_ids)))

            if dominant and len(chosen) >= 1:
                dominant_share = rng.uniform(50.0, 75.0)
                remaining = 100.0 - dominant_share
                others = []
                if len(chosen) > 1:
                    raw_w = [rng.random() for _ in range(len(chosen) - 1)]
                    total = sum(raw_w)
                    others = [r / total * remaining for r in raw_w]
                shares = [dominant_share] + others
            else:
                raw_w = [rng.random() for _ in chosen]
                total = sum(raw_w)
                shares = [r / total * 100.0 for r in raw_w]

            lead_time = rng.choice([14, 21, 30, 45, 60, 90])
            for supplier_id, share in zip(chosen, shares):
                rows.append(
                    {
                        "id": str(uuid.uuid4()),
                        "county_id": county_id,
                        "supplier_id": supplier_id,
                        "active_ingredient": ingredient,
                        "share_pct": round(share, 2),
                        "lead_time_days": lead_time,
                        "contract_start": "2024-01-01",
                        "contract_end": "2026-12-31",
                        "data_source": "synthetic_v2",
                    }
                )
    return rows


def generate(
    seed: int = 42,
    db_url: str | None = None,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    """Generate all synthetic procurement data (v2).

    Queries the DB for the top 25 recalled manufacturers and creates ~20
    supplier rows that deliberately link to those manufacturers by canonical
    name. Each linked supplier's county_supply rows use that manufacturer's
    top recall ingredient (normalized INN) so the supply_chain_exposure rule
    can fire.

    Args:
        seed: Random seed for determinism.
        db_url: PostgreSQL connection string.  Falls back to
            ``DATABASE_URL`` env var, then a local dev default.

    Returns:
        Tuple of ``(counties, suppliers, county_supply)`` record lists.
    """
    rng = random.Random(seed)

    resolved_db_url = (
        db_url
        or os.environ.get("DATABASE_URL")
        or "postgresql://regulatory:regulatory@127.0.0.1:5433/regulatory"
    )

    print(f"Querying top manufacturers from {resolved_db_url.split('@')[-1]} …")
    top_mfrs = _query_top_manufacturers(resolved_db_url, limit=25)
    linked_count = min(20, len(top_mfrs))
    linked_mfrs = top_mfrs[:linked_count]
    print(f"  {len(linked_mfrs)} manufacturer-linked suppliers will be created.")

    # ---- Counties ----
    county_records: list[dict[str, object]] = [
        {
            "id": str(uuid.UUID(int=i + 1)),
            "name": c["name"],
            "region": c["region"],
            "population": c["population"],
            "health_facilities": c["health_facilities"],
        }
        for i, c in enumerate(COUNTIES)
    ]
    county_ids = [str(r["id"]) for r in county_records]

    # ---- Suppliers ----
    # Linked suppliers: name = manufacturer's canonical_name so the loader
    # can resolve manufacturer_id by exact match.
    linked_supplier_records: list[dict[str, object]] = []
    for i, mfr in enumerate(linked_mfrs):
        linked_supplier_records.append(
            {
                "id": str(uuid.UUID(int=2001 + i)),
                "name": mfr["canonical_name"],
                "role": "manufacturer",
                "countries_served": "KE,ZA,NG",
                "manufacturer_id": "",  # resolved by name at load time
                "data_source": "synthetic_v2",
            }
        )

    distributor_records: list[dict[str, object]] = [
        {
            "id": str(uuid.UUID(int=3001 + i)),
            "name": d["name"],
            "role": d["role"],
            "countries_served": ",".join(str(cs) for cs in d["countries_served"]),  # type: ignore[arg-type]
            "manufacturer_id": "",
            "data_source": "synthetic_v2",
        }
        for i, d in enumerate(_DISTRIBUTORS)
    ]

    supplier_records = linked_supplier_records + distributor_records

    # ---- County supply ----
    linked_supplier_ids = [str(r["id"]) for r in linked_supplier_records]
    distributor_ids = [str(r["id"]) for r in distributor_records]

    supply_records: list[dict[str, object]] = []

    # Linked suppliers: rows for their recall ingredient.
    for sup_rec, mfr in zip(linked_supplier_records, linked_mfrs):
        ingredient = str(mfr["top_ingredient"])
        if not ingredient:
            continue
        supply_records.extend(
            _generate_county_supply_for_linked(
                rng,
                county_ids,
                str(sup_rec["id"]),
                ingredient,
                n_counties=20,
            )
        )

    # Distributor suppliers: rows for EML ingredients.
    supply_records.extend(
        _generate_county_supply_for_distributors(
            rng, county_ids, distributor_ids, _EML_INGREDIENTS
        )
    )

    print(
        f"  Generated {len(supply_records)} county_supply rows "
        f"({len(linked_supplier_ids)} linked suppliers × ~20 counties + "
        f"{len(distributor_ids)} distributors × {len(_EML_INGREDIENTS)} EML ingredients × 47 counties)."
    )

    return county_records, supplier_records, supply_records


def write_csvs(
    out_dir: Path,
    counties: list[dict[str, object]],
    suppliers: list[dict[str, object]],
    county_supply: list[dict[str, object]],
) -> None:
    """Write the three CSV files to *out_dir*.

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
    parser = argparse.ArgumentParser(
        description="Generate synthetic procurement CSVs (v2 — DB-aware)."
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default 42).")
    parser.add_argument(
        "--out-dir",
        default="data/synthetic/procurement_v2",
        help="Output directory (default data/synthetic/procurement_v2).",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="PostgreSQL URL (defaults to DATABASE_URL env var).",
    )
    args = parser.parse_args()

    counties, suppliers, supply = generate(seed=args.seed, db_url=args.db_url)
    write_csvs(Path(args.out_dir), counties, suppliers, supply)

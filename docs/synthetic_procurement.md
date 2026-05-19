# Synthetic Procurement Data

The supply-chain exposure rule requires county-level procurement data that maps active
ingredients to suppliers and their market share.  Since real county procurement data is not
yet available, the engine ships with a deterministic synthetic dataset that lets the pipeline
run end-to-end in development and CI.

---

## What is generated

| Table         | Rows  | Description                                      |
|---------------|-------|--------------------------------------------------|
| `counties`    | 47    | All Kenyan counties with 2019 census populations |
| `suppliers`   | 30    | Fictional supplier entities, FK → `manufacturers`|
| `county_supply` | ~2 100 | Share allocation per supplier × county × ingredient |

Active ingredients covered: the 15 WHO Essential Medicines List (EML) drugs defined in
`src/regulatory/risk/eml_ingredients.py`:

```python
EML_INGREDIENTS = (
    "amoxicillin", "ceftriaxone", "metronidazole", "artemether-lumefantrine",
    "oxytocin", "insulin (regular human)", "paracetamol", "ibuprofen",
    "omeprazole", "salbutamol", "atorvastatin", "hydrochlorothiazide",
    "metformin", "prednisolone", "tenofovir-lamivudine-dolutegravir",
)
```

---

## Generator

`scripts/generate_synthetic_procurement.py` produces three CSV files:

```
data/synthetic/counties.csv
data/synthetic/suppliers.csv
data/synthetic/county_supply.csv
```

The generator is fully deterministic (fixed `seed=42`).  Re-running it always produces the
same files.  All `county_supply` rows carry `data_source="synthetic_v1"` for easy removal
when real data arrives.

```bash
python scripts/generate_synthetic_procurement.py --out-dir data/synthetic
```

---

## Loading into the database

```bash
regulatory procurement load --csv-dir data/synthetic
```

This is idempotent: rows with the same `(county_id, supplier_id, active_ingredient)` primary
key are skipped on re-run.

---

## Swapping in real data

When real county procurement data becomes available:

1. Produce CSVs matching the schema of `counties.csv`, `suppliers.csv`, and `county_supply.csv`.
2. Set `data_source="real_<year>"` in the `county_supply` rows.
3. Run `regulatory procurement load --csv-dir <path-to-real-data>`.
4. Optionally delete synthetic rows: `DELETE FROM county_supply WHERE data_source = 'synthetic_v1'`.

The rest of the risk engine is unchanged — it reads from the same tables regardless of
`data_source`.

---

## Schema reference

### `counties`

| Column     | Type   | Notes                            |
|------------|--------|----------------------------------|
| id         | UUID PK |                                 |
| name       | text   | County name (e.g. "Nairobi")     |
| code       | int    | Kenya county code 001–047        |
| population | int    | 2019 census population           |

### `suppliers`

| Column          | Type     | Notes                                  |
|-----------------|----------|----------------------------------------|
| id              | UUID PK  |                                        |
| name            | text     | Supplier trading name                  |
| manufacturer_id | UUID FK  | → `manufacturers.id` (nullable)        |
| countries       | text[]   | Countries of registration              |

### `county_supply`

| Column               | Type     | Notes                                       |
|----------------------|----------|---------------------------------------------|
| id                   | UUID PK  |                                             |
| county_id            | UUID FK  | → `counties.id`                             |
| supplier_id          | UUID FK  | → `suppliers.id`                            |
| active_ingredient    | text     | INN name                                    |
| share_pct            | numeric  | 0–100, CHECK(share_pct >= 0 AND <= 100)     |
| lead_time_days       | int      | Days from order to delivery                 |
| data_source          | text     | `"synthetic_v1"` or `"real_<year>"`         |

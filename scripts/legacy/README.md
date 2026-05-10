# Legacy scripts

These are the original working scripts that produced the data in `data/ppb_pdfs/`
and `data/processed_texts/`. Their patterns have been ported into the ingestion
framework (`src/regulatory/ingestion/` and `src/regulatory/sources/ppb_ke_alerts.py`).

Kept here as:
- A working fallback if the new adapters have issues
- Documentation of source-specific quirks (PPB's /download/ subpage pattern,
  filename collision handling, request cadence) that the new code inherits but
  doesn't always explain inline

Remove after the new adapters have run cleanly in production for a reasonable period.

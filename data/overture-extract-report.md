# Overture Raw Extract Report

- Release: 2026-07-22.0
- Gap-fill boxes:
  - I-5 corridor: the Grapevine north through southern Oregon: lng [-124.0, -120.0], lat [35.0, 44.0]
  - SoCal extension: the Los Angeles to San Diego leg: lng [-119.0, -116.5], lat [32.5, 35.0]
- Category filter: gas_station, truck_gas_station
- Confidence floor: 0.5
- Raw rows returned: 10248
- Rows written: 10248
- Rows skipped:
  - malformed_coordinate: 0
- Output byte size: 1544860
- Query wall-clock duration: 543.0s

## Forward risk

This command's category predicate was migrated off the deprecated `categories` field to `taxonomy.primary` on 2026-08-11, ahead of the September 2026 Overture release in which `categories` is removed. `CATEGORY_FILTER`'s two values are unchanged; only the struct path reading them changed.

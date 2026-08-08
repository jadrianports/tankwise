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
- Query wall-clock duration: 511.9s

## Forward risk

The `categories` field this extract filters on is deprecated as of the pinned release and is scheduled for removal in the September 2026 Overture release, replaced by `basic_category` and `taxonomy`. A refresh run against a later release must migrate this command's category predicate before that release ships.

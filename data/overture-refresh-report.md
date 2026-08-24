# Overture Refresh Diff Report

- Candidate path: /home/runner/work/_temp/overture_stations_candidate.csv
- Canonical path: /home/runner/work/tankwise/tankwise/data/overture_stations.csv
- overture_stations.csv rows: before=10051 after=10051
- Routable stations (Station.objects.routable()): before=16341 after=16341
- Cells measured: 26
- Changed cells: 0 (admission flips: 0, censorship transitions: 0, presence changes: 0, other movements: 0)
- Corridor cells use mpg=10, starting_fuel=0.5, price_basis=neutral; demo cells use the SPA hero preset mpg=6.5, tank_range_mi=1050, starting_fuel=1 -- these two vehicles are never conflated. penalty=$35 for every cell.

## Measurement basis

- Production column set: every solve in it ran at the production trust margin ($5.47), read live from settings.TRUST_MARGIN_USD, which is the objective a driver's plan is actually computed under. This is the authoritative column set for the reviewer's decision.
- Baseline column set: every solve in it ran at a zero trust margin ($0), the objective every Phase-22 hand measurement and every pinned calibration table in this project was taken under. It is shown for comparability with the project's own recorded history, never for the reviewer's decision.
- Which set drives the verdict: the changed-cell count in the header above and all four ## Changed cells subsections below are computed from the production column set alone. A cell that moves only in the baseline column set is not counted as changed and is not listed in any of those sections.
- Margin independence: the trust margin can move only stops and total_cost. raw_candidates, kept, estimate and admitted_at_current_budget cannot move with it for the rule this command drives -- the unstrengthened, margin-blind default path prune_dominated_candidates() always runs on here (Phase 25 landed a strengthened, penalty-aware branch of that same function, but it ships inert: this command never supplies the two parameters that would activate it). Four identical column pairs is therefore the expected result below, not a rendering bug -- proven against real committed data by TwoMarginWorldRenderTests.
- Direction of the bias: every Overture row is priced as an eia_regional_estimate, precisely the rows the trust margin exists to suppress, so the zero-margin (baseline) columns overstate how often those rows get selected. This states the direction only; it makes no attempt to quantify the size of the effect, in keeping with this project's standing rejection of false precision.

## Per-cell table

| Cell | Tank (mi) | Raw candidates @$5.47 | Kept @$5.47 | Estimate @$5.47 | Admitted @$5.47 | Stops @$5.47 | Total cost @$5.47 | Raw candidates @$0 | Kept @$0 | Estimate @$0 | Admitted @$0 | Stops @$0 | Total cost @$0 | Changed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| atlanta_ga-denver_co | 1050 | 220->220 | 46->46 | 32487->32487 | True->True | 1->1 | $283.10->$283.10 | 220->220 | 46->46 | 32487->32487 | True->True | 1->1 | $283.10->$283.10 |  |
| atlanta_ga-denver_co | 500 | 220->220 | 97->97 | 150905->150905 | False->False | 4->4 | $363.75->$363.75 | 220->220 | 97->97 | 150905->150905 | False->False | 4->4 | $363.75->$363.75 |  |
| dallas_tx-seattle_wa | 1050 | 243->243 | 71->71 | 117895->117895 | False->False | 3->3 | $500.04->$500.04 | 243->243 | 71->71 | 117895->117895 | False->False | 3->3 | $500.04->$500.04 |  |
| dallas_tx-seattle_wa | 500 | 243->243 | 102->102 | 61944->61944 | False->False | 5->5 | $591.10->$591.10 | 243->243 | 102->102 | 61944->61944 | False->False | 5->5 | $591.10->$591.10 |  |
| demo_la_ca-denver_co-chicago_il | 1050 | 1247->1247 | 584->584 | 64705287->64705287 | False->False | 2->2 | $440.08->$440.08 | 1247->1247 | 584->584 | 64705287->64705287 | False->False | 2->2 | $440.08->$440.08 |  |
| demo_la_ca-new_york_ny | 1050 | 1422->1422 | 645->645 | 65261638->65261638 | False->False | 3->3 | $787.84->$787.84 | 1422->1422 | 645->645 | 65261638->65261638 | False->False | 3->3 | $787.84->$787.84 |  |
| el_paso_tx-portland_me | 1050 | 450->450 | 134->134 | 685744->685744 | False->False | 4->4 | $580.06->$580.06 | 450->450 | 134->134 | 685744->685744 | False->False | 4->4 | $580.06->$580.06 |  |
| el_paso_tx-portland_me | 500 | 450->450 | 197->197 | 552755->552755 | False->False | 7->7 | $677.17->$677.17 | 450->450 | 197->197 | 552755->552755 | False->False | 7->7 | $677.17->$677.17 |  |
| fargo_nd-amarillo_tx | 1050 | 175->175 | 13->13 | 812->812 | True->True | 1->1 | $177.99->$177.99 | 175->175 | 13->13 | 812->812 | True->True | 1->1 | $177.99->$177.99 |  |
| fargo_nd-amarillo_tx | 500 | 175->175 | 52->52 | 41832->41832 | True->True | 2->2 | $255.05->$255.05 | 175->175 | 52->52 | 41832->41832 | True->True | 2->2 | $255.05->$255.05 |  |
| houston_tx-chicago_il | 1050 | 239->239 | 3->3 | 23->23 | True->True | 1->1 | $157.97->$157.97 | 239->239 | 3->3 | 23->23 | True->True | 1->1 | $157.97->$157.97 |  |
| houston_tx-chicago_il | 500 | 239->239 | 53->53 | 48926->48926 | True->True | 2->2 | $241.97->$241.97 | 239->239 | 53->53 | 48926->48926 | True->True | 2->2 | $241.97->$241.97 |  |
| jacksonville_fl-bangor_me | 1050 | 347->347 | 41->41 | 23013->23013 | True->True | 1->1 | $244.24->$244.24 | 347->347 | 41->41 | 23013->23013 | True->True | 1->1 | $244.24->$244.24 |  |
| jacksonville_fl-bangor_me | 500 | 347->347 | 125->125 | 356085->356085 | False->False | 4->4 | $349.33->$349.33 | 347->347 | 125->125 | 356085->356085 | False->False | 4->4 | $349.33->$349.33 |  |
| miami_fl-boston_ma | 1050 | 365->365 | 39->39 | 19827->19827 | True->True | 1->1 | $291.22->$291.22 | 365->365 | 39->39 | 19827->19827 | True->True | 1->1 | $291.22->$291.22 |  |
| miami_fl-boston_ma | 500 | 365->365 | 101->101 | 182506->182506 | False->False | 3->3 | $392.03->$392.03 | 365->365 | 101->101 | 182506->182506 | False->False | 3->3 | $392.03->$392.03 |  |
| nashville_tn-buffalo_ny | 1050 | 166->166 | 3->3 | 23->23 | True->True | 1->1 | $52.85->$52.85 | 166->166 | 3->3 | 23->23 | True->True | 1->1 | $52.85->$52.85 |  |
| nashville_tn-buffalo_ny | 500 | 166->166 | 29->29 | 8168->8168 | True->True | 1->1 | $137.61->$137.61 | 166->166 | 29->29 | 8168->8168 | True->True | 1->1 | $137.61->$137.61 |  |
| phoenix_az-minneapolis_mn | 1050 | 229->229 | 25->25 | 4809->4809 | True->True | 2->2 | $340.30->$340.30 | 229->229 | 25->25 | 4809->4809 | True->True | 2->2 | $340.30->$340.30 |  |
| phoenix_az-minneapolis_mn | 500 | 229->229 | 54->54 | 16322->16322 | True->True | 3->3 | $445.73->$445.73 | 229->229 | 54->54 | 16322->16322 | True->True | 3->3 | $445.73->$445.73 |  |
| sacramento_ca-salt_lake_city_ut | 1050 | 493->493 | 8->8 | 247->247 | True->True | 1->1 | $41.22->$41.22 | 493->493 | 8->8 | 247->247 | True->True | 1->1 | $41.22->$41.22 |  |
| sacramento_ca-salt_lake_city_ut | 500 | 493->493 | 312->312 | 10026999->10026999 | False->False | 2->2 | $137.12->$137.12 | 493->493 | 312->312 | 10026999->10026999 | False->False | 2->2 | $137.12->$137.12 |  |
| san_diego_ca-jacksonville_fl | 1050 | 509->509 | 143->143 | 775264->775264 | False->False | 3->3 | $518.85->$518.85 | 509->509 | 143->143 | 775264->775264 | False->False | 3->3 | $518.85->$518.85 |  |
| san_diego_ca-jacksonville_fl | 500 | 509->509 | 194->194 | 600415->600415 | False->False | 6->6 | $670.33->$670.33 | 509->509 | 194->194 | 600415->600415 | False->False | 6->6 | $670.33->$670.33 |  |
| toronto_oh-hillsboro_or | 1050 | 509->509 | 214->214 | 2970562->2970562 | False->False | 4->4 | $623.42->$623.42 | 509->509 | 214->214 | 2970562->2970562 | False->False | 4->4 | $623.42->$623.42 |  |
| toronto_oh-hillsboro_or | 500 | 509->509 | 245->245 | 1384311->1384311 | False->False | 6->6 | $735.82->$735.82 | 509->509 | 245->245 | 1384311->1384311 | False->False | 6->6 | $735.82->$735.82 |  |

## Changed cells

### Admission flips

(none)

### Censorship transitions

(none)

### Presence changes

(none)

### Other movements (cost/stops/etc.)

(none)

## Reviewer notes

- The two large CSVs this pull request carries (data/overture_stations.csv and data/overture_raw_extract.csv) will not render a usable line-by-line diff in the review UI because of their size -- this table is the reviewable artifact by design.
- A red DispatchAdmissionManifestTests guard inside this pull request is the design working, not a defect in this pipeline. It must be resolved by a deliberate hand re-pin of ADMISSION_MANIFEST, never by a regenerate path -- there is no regenerate path, and none should be added.
- The committed prose station counts in README.md and docs/ALGORITHM.md are hand-maintained and may now be stale. Use this report's own before/after overture_stations.csv-row and routable-station counts above when updating them.

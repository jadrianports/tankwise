# Overture Import Report

## Source

- Release: 2026-07-22.0
- Gap-fill boxes:
  - I-5 corridor: the Grapevine north through southern Oregon: lng [-124.0, -120.0], lat [35.0, 44.0]
  - SoCal extension: the Los Angeles to San Diego leg: lng [-119.0, -116.5], lat [32.5, 35.0]
- Category filter: gas_station, truck_gas_station
- Confidence floor: 0.5
- Licence: CDLA-Permissive-2.0

## Hygiene exclusions

- Input rows: 10248
- Exclusion buckets:
  - mojibake: 0
  - alt_fuel_only: 28
  - closed_status: 0
  - below_confidence_floor: 0
  - malformed_row: 169
- Post-hygiene total: 10051
- Rows with an unknown (blank/NULL) operating status were RETAINED, not excluded: 3527

## Dedup

Match tight on distance only when the existing row is rooftop-precision -- both sides are then real coordinates. For city-centroid rows, distance is not a dedup signal at all; match on normalized brand plus city and state. Never one shared radius.

- Tight-tier matches (rooftop-precision existing rows): 0
- City-tier matches (city-centroid existing rows, brand+city+state): 0
- No match (kept as new): 10051
- Tight-tier threshold used: 0.25 mi
- Sensitivity (information only -- never a retune signal, never cited to change the shipped threshold):
  - 0.15 mi: 0
  - 0.25 mi: 0
  - 0.4 mi: 0

## Spot-checked clusters

Selection rule: the 8 densest clusters by candidate count inside the gap-fill boxes, plus any cluster containing a tight-tier decision whose distance falls within 20% of the 0.25 mi threshold. Every selected cluster below is reported whether it looks good or bad.

### Cluster 1: near Beverly Hills, CA (grid cell (2355, -4914), 16 candidate(s))

- 231b10b1-c1db-4870-aff7-688d880936bd 'Shell': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 30f5aa6f-bc0a-45c4-a700-a014d54fe2a6 'Shell': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 3206fc6a-38da-4b74-9355-dce3213f8b05 'Exxon': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 5c4582bf-92bd-498a-b855-5e99c2128681 '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 72e5264f-76b4-48d3-8166-c2b41b29b0c3 'ROCKET 611': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 7a6c96be-ea26-4e3a-b81a-9f2c0c03eedf 'Helios House': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 7a720703-1492-413c-b13a-89f65ee6322d 'LOOP Charging Station': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- ad5d7e2a-bc5c-4758-9bae-62ef4ed6ebb5 '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- af8efae0-7074-4931-8167-4f62982b6c01 'Chevron': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- b294c3f9-c99f-486b-8e28-a6628ebc0f7d '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- c29f0728-5b8f-4b21-b0f8-37cd4ec4d8a0 'Mobil': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- cc855d35-b9e8-4702-93e3-b0debba9f41f 'gas station': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- d4acd0a5-eea0-406d-bec5-63f372c95811 'Robertson Shell': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- e6eaf5a8-b363-4f83-a2b7-a7fb6c423312 'COLKERS UNION 76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- ed2562f9-97c8-4ba4-b5cb-8d643fe82b54 'Speedway Express': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- ed4401af-902d-4453-9a11-b13f30f2b34b 'ARCO': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)

### Cluster 2: near West Sacramento, CA (grid cell (2668, -5045), 16 candidate(s))

- 3e3196ef-8d69-4afd-a029-aa7ab9bff383 'Mobil': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 4c7ddced-b685-43d5-bf2d-4f941eaedc19 'AJ Hundal Mart': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 6fd9564d-a205-469e-b22b-7856eb39f0ca 'Chevron': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 70ce8e69-67af-40c1-b1e9-6ddf7992e325 'Service Station Systems': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 8127e546-d3d4-4bff-96f5-9cc3f19bfa61 'Shell': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 8e0fb3fa-c5c6-4b3c-9457-d92a582ef80d 'Trillium - 1355': city tier, kept (no rooftop candidate found within the search radius; no city-tier match on brand, city and state)
- 95f31192-6b71-43bd-a763-7c3e671633aa 'Sunoco': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 9b7d6a69-dc03-45c4-9696-3e2f1237a20f 'ARCO': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 9c43cc24-314d-4889-ac1f-33314b5755e5 '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- a0d441b8-fca3-4205-ae38-0a6e5c9037fe 'Ample Sales': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- bcf1da01-020b-4e20-8d61-e97ae12b2adf 'Sinclair Gas Station': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- caa682e7-86ba-4d90-a8c5-8fc1112adba8 'Harbor Truck and Auto Plaza': city tier, kept (no rooftop candidate found within the search radius; no city-tier match on brand, city and state)
- ece9224a-4a59-40c0-8d4b-a6bbe50121a9 'Albertsons': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- f389f7cc-f452-40cf-bfcf-bd0fbed406d5 'Exxon': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- f969f28d-85c9-4fc6-8b38-8625e41c795e 'ExtraMile': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- fec6fad7-7b3e-4360-874f-e470e34d2141 'BALJIT GILL': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)

### Cluster 3: near Los Angeles, CA (grid cell (2349, -4914), 15 candidate(s))

- 1db87978-3e31-4807-b92d-d05e6f53c4d2 'Shell': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 218f48d9-18ee-426c-b98a-b155e13679aa '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 4b577ad3-3e9f-445f-9c55-5f0f08c0aef9 'Chevron(Frio) (@ La Tijera Blvd/405 fwy)': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 4bf4d45d-5092-43ee-a4a9-a5a98e35824f '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 62daa022-95f0-4aa3-a182-feb20fe54c75 'TONY BAL GULF': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 6b508208-3f67-44d5-847b-b979dbfd09ba 'ROCKET 619': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 780001b8-1663-4086-bbd4-7c942d24e617 'Sinclair Gas Station': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- a04aeb6c-e5fe-4a19-9ce9-bc1106a8d180 'APRO LLC 2704701': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- af2fb283-50ee-42ec-8c73-7ff40fe94ebe 'ARCO': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- ba643a13-65f0-4815-ac27-6cbc41844e03 '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- bf597459-b57e-412d-8f31-ffdcfcec8665 'Mobil': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- e1f27865-c2e6-4ea4-9101-567a1a822f1d 'Chevron Station Los Angeles': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- e33fcac8-dd50-41f1-a95c-189d0144fe79 'ROCKET 5625': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- eeeda04c-4bd5-4c6b-9de7-c3fc37738f46 'Laser Car Wash Sinclair Gas Station': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- f800f86f-954c-48fb-85f0-0bfbf64d8bf2 'Gulf': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)

### Cluster 4: near Madera, CA (grid cell (2556, -4983), 15 candidate(s))

- 46d8c437-a33a-46da-9fca-11694be77d5d 'Byte Federal Bitcoin ATM (La Plaza 24 Madera)': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 533e4ecf-4dd4-4fe0-a918-5dde60572538 'Pemex': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 60040533-87ec-4187-8c91-ffc3e45e1153 'Sinclair': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 65e5414d-bd80-4c82-89f1-6dfb6d7ef58f "BJ's Gas & Liquor": tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 6be10ee7-39bd-47cb-a68c-b2476c63b2c5 'Valero': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 7707c204-7669-48f1-8380-b49c48b93310 'Shell': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 94b4d8ea-a039-42a2-909d-b8fbe2fc5310 'Gas Station Open 24 Madera': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 95ad1820-a871-45e6-a4a3-d7f6ed03bfeb 'Chevron': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- aaa6b69a-5db3-46c5-ab9e-52b22ea0c877 'Shell': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- af94e5c1-b83c-48df-bdf2-c126ecb47da3 'Stop & Save Mini Mart': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- c6c3a1ee-a182-440b-81e4-a0b581994fe0 'La Plaza 24 Gas Station': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- e3758544-10f0-42dd-80fa-655b0926c9f7 '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- ecb994c2-4ff6-4d00-aa43-029efc2876ed 'Gas & Grab': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- f09d34ec-77c4-4b63-975a-040d90ef5b0c 'Central Gas': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- fb88ed64-52ee-449d-9cb8-9065ef0475ec 'Sinclair': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)

### Cluster 5: near San Diego, CA (grid cell (2265, -4861), 14 candidate(s))

- 0ceb89f6-c366-4e67-89c5-3a084819fb43 'Speedway': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 1f82226a-87e4-43fd-b924-6d98f359dbde 'Ultra Gas': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 4e7743d2-77aa-4087-9996-87a5d6e61557 'ARCO': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 51fa9733-13a8-4ba6-8daf-ad5571706300 'Mobil': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 754776e9-f025-41ea-a818-27d4f059c01c 'Shell': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 7a27d564-e26a-4f5b-aeb0-8fb1c9f6923c 'Limited Oil': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 84fb1104-c20c-47cf-8acd-8439f82e8cba '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 85d74dd6-63df-418b-825c-1b253d190614 'Chevron': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 9d9295a5-afb1-48f9-8f9f-1fafb7c367cc 'Shell': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- a0763e8d-1886-4139-b107-993b73d95ecf 'Chevron Station San Diego': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- ac674d34-dfba-43c5-a464-41725d2860c2 'ARCO': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- ca1a49ef-5bfe-4d5e-8c64-d8781cfb7eab '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- e05b4738-051c-4066-8cf0-d4f3f211cb84 'Chevron': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- fa5e6479-6764-49ca-93a0-4b356ffd74d9 'WORLD OIL 057': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)

### Cluster 6: near South San Francisco, CA (grid cell (2604, -5081), 14 candidate(s))

- 06af9395-6689-4ac2-b09e-1c8a3fc939f9 'Speedway': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 1d130d06-3b5a-42c5-9bb8-8115336da63d '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 3851f33c-812c-43e7-a5bd-c8424eae4967 'Shell': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 5121bf32-d238-4339-8b92-aea80d144684 'Shell': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 69a333a8-165b-45d7-8847-64cd90b285bf 'SFO Express Market': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 8a227318-1320-4370-a0de-cd8a0f177426 'TrueZero': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 8c63eef8-e220-4690-8d32-0ed47143d355 'AIRPORT BLVD. GAS': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- a8b43d4d-c7fd-410e-8f02-6e785e1ae38e 'Speedway Express': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- d92266f8-a55f-4b3c-80c3-ec35595b9554 'Exxon & Mobil Stations': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- e6cf7758-0415-4b47-ad71-0e85a644464f 'Valero': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- e7ddc5fc-2a7c-407a-b37b-984ea2ab7d60 'Shell': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- e95c18f1-0d83-494a-88ae-651d4af6ca90 'GRAND MARTCO INC': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- ea0e99d5-da26-44d1-b3b2-0c1e801220c0 'Exxon & Mobil Stations': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- f6c2e5ab-2b90-4c2c-a512-0b4ca192870e 'South San Francisco Marathon': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)

### Cluster 7: near El Cajon, CA (grid cell (2269, -4854), 13 candidate(s))

- 06b589a3-d82e-4f43-8951-2f167e01575d 'ARCO': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 21eb3dd9-6682-410a-894a-67c7fc452b87 'Chevron Station / Extra Mile El Cajon': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 60d2766f-8d75-4d3e-999b-49674d4eaadc 'CF UNITED LLC 2700020': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 6a3d8f5a-6b86-4d67-b46b-81c520bc2010 'ARCO': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 6e93a8c1-3c21-4f1e-b62f-9d7dd8e4f61e 'Mollison Gas & Mini Market': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 716245c8-31b3-4e9c-ad1d-c84ea03f8c27 'Speedway': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 7ae4ff7a-fa11-4c51-9a57-c55ad8519bbd 'ARCO': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- aa039974-8783-4cc4-ba2d-3ac1c4d6311b 'Chevron': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- c67e7269-accd-479f-b1f7-e9e4ca81a1fe 'CF UNITED LLC 2700045': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- cdecbb1c-81f9-478d-8845-5206df666db4 '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- d8c4b6a1-d0e6-4f86-a43b-00adff0a0215 'Shell': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- f617de0a-f547-46bc-911e-ad765d207988 '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- f83c9f2d-c345-4ab4-b032-986fa43a4172 'Quick Trip': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)

### Cluster 8: near Wilmington, CA (grid cell (2336, -4909), 13 candidate(s))

- 45cec053-2a43-4010-90f8-536a1677e478 '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 4e941d0c-6824-4dd0-8ab4-0fa3dd2b7eba '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 5a6b8537-0480-439b-840f-e9814d854f5f "Charlie's Sub Sandwich Station": tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 6475dfb0-3f1d-431e-b817-70bb4c6e822f 'USA Gasoline': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 676d39f2-5819-41a2-bbe3-cfbdf898f5a5 'CF UNITED LLC 251777': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 771cbf81-d642-458a-94ee-692730de7efa 'THE REFINERY GENERAL STORE, INC': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 8dbf58f7-be42-43d0-947d-8a2c6d7faa44 '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- 8f920b8a-f534-4e94-a8c7-9ebb26b9fc08 'Chevron': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- b0ec5a2f-c376-474e-b7c2-df3ab1d0ca8d 'ARCO': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- b47707fd-a049-4945-8b47-e22f9badb353 'CA GAS & MINI MART': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- d5cee314-3955-44ab-aba8-9225eae7114b 'ExtraMile': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- ea460163-185b-4be3-a367-9d87b90f9b30 '76': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)
- f5083e9e-c67b-4cbb-b333-83e121c0d298 'The Gas Company': tight tier, kept (no rooftop candidate found within the search radius; brand token is None so the city tier does not apply)

## Result

- Kept: 10051
- Priced rows by region:
  - CALIFORNIA: 9673
  - PADD1A: 1
  - PADD1B: 7
  - PADD1C: 3
  - PADD2: 10
  - PADD4: 4
  - PADD5_EX_CA: 353
- opis_id range used: [1000000000, 2000000000)

## Forward risk

- The upstream `categories` field this import reads is deprecated as of the pinned release and scheduled for removal in the September 2026 release, replaced by `basic_category` and `taxonomy`. A refresh against any later release must migrate the field this import reads `categories.primary` from.

# Preparing and publishing a catalog

Run these commands from the repository root (the directory containing `Cargo.toml` and `tools/`). Run the preparation tools outside the application container. Use one importer at a time per cache directory. No runtime API key, scheduled job, cloud storage, or remote database is needed.

## 1. Install preprocessing dependencies

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r tools/catalog/requirements.txt
mkdir -p data/inputs data/cache data/staging
python3 -m tools.catalog --help
```

## 2. Acquire EPA geography and a USDA checklist

Choose **one** of the two EPA acquisition methods below. Both produce `data/inputs/areas.json` and its `.source.json` record. Once those files have been created successfully, skip the other method and continue with the USDA checklist section.

Download and convert the official **US Level III Ecoregions shapefile with state boundaries** in one command:

```sh
python3 -m tools.catalog areas --download-epa --out data/inputs/areas.json
```

This uses [EPA's published download](https://www.epa.gov/eco-research/level-iii-and-iv-ecoregions-continental-united-states), caches the full ZIP, reads its actual member names, and records the source URL and terms. EPA's [linked metadata](https://dmap-prod-oms-edc.s3.us-east-1.amazonaws.com/ORD/Ecoregions/us/Eco_Level_III_US.html) states no access or use constraints and requests EPA acknowledgement. This command needs outbound network access.

**Alternative: use a ZIP you downloaded manually.** This is not a follow-up command for `--download-epa`, which keeps its archive in `data/cache/` rather than creating `data/inputs/epa.zip`. Save your manually downloaded full ZIP as `data/inputs/epa.zip`, then run:

```sh
python3 -m tools.catalog areas \
  --shapefile data/inputs/epa.zip \
  --url 'https://dmap-prod-oms-edc.s3.us-east-1.amazonaws.com/ORD/Ecoregions/us/us_eco_l3_state_boundaries.zip' \
  --license 'EPA metadata: no access or use constraints; EPA acknowledgement requested' \
  --out data/inputs/areas.json
```

`--shapefile` also accepts an extracted directory, such as `data/inputs/epa`, or an actual `.shp` path. Directory and ZIP inputs discover a single shapefile recursively; if several are present, select the intended `.shp` explicitly. The ZIP's filename is not necessarily the shapefile's filename. Keep matching `.shp`, `.dbf`, `.shx`, and `.prj` files together; passing only a `.shp` is insufficient. Extension case is handled automatically.

If a previous command reports a missing `.prj`, first check whether the `.shp` path exists at all. To locate the extracted shapefile:

```sh
find data/inputs/epa -type f -iname '*.shp'
```

A wrong basename, a nested archive directory, or extracting only `.shp` can all cause this error. Do not create an empty `.prj` or guess its CRS: re-download the full state-clipped archive if its companion files are missing. Use the state-clipped product; uncut polygons cannot establish state membership by themselves.

The tool reads `.prj` and reprojects to WGS84. It accepts `STATE_NAME` or `STATE_ABBR`, `US_L3CODE`, and `US_L3NAME`. For an existing GeoJSON export, normalize properties to `state` (two-letter code), `ecoregionCode`, and `name`; preserve Polygon/MultiPolygon geometry including holes. Register that file with `snapshot --file` below. Alaska, Hawaii, DC, and territories are excluded.

### USDA taxonomy from the uploaded Zenodo archive

When the USDA site is unavailable, use the EOL/Zenodo archive already saved as `data/inputs/usda_plant_traits.tar.gz`:

```sh
.venv/bin/python -m tools.catalog usda-dwca
```

That command runs entirely offline and writes `data/inputs/usda.csv`, `data/inputs/usda.csv.source.json`, and `data/inputs/usda.csv.import.json`. **Once conversion succeeds, skip the direct USDA download below and continue to observation collection.** No additional `snapshot` command is needed for the converted file.

The adapter identifies known archive versions by their published file checksum. The uploaded file matches [Zenodo record 17903503, v7](https://zenodo.org/records/17903503), rather than the older v4 record originally linked. Its 35,186 taxon records include 23,587 species; this version yields 22,620 species after identifier, scientific-name, and ambiguity checks. The report records every exclusion count. For a different archive or output path:

```sh
.venv/bin/python -m tools.catalog usda-dwca \
  --archive data/inputs/usda_plant_traits.tar.gz \
  --out data/inputs/usda.csv
```

Unknown archive checksums require an explicit pinned record URL through `--source-url`; known versions reject a conflicting record URL. The generated provenance records the archive SHA-256, MD5, record version, transformation, and the normalized file's checksum. Preserve the original tarball; nothing is extracted into the project.

Only factual USDA taxon identifiers, scientific names, and families are imported. This archived subset is not a complete current checklist and provides no synonym crosswalk. Its taxon table has no common names; the course builder uses iNaturalist's preferred common names where available, otherwise scientific names.

The Zenodo archive does not specify an overall license. The importer records the original USDA plant-data-use terms for the factual taxonomy fields without declaring the entire EOL archive CC0 or CC BY. It does not import trait descriptions or auxiliary media. The media table contains template examples (including polar bears), and the native-range traits use broad jurisdictions such as L48; neither qualifies as course photography or state-native evidence. The original archive remains available for future trait processing with its own provenance checks.

### Alternative: download the checklist directly from USDA

Download and register the complete USDA checklist directly with this command. It needs no placeholder values or manually downloaded CSV:

```sh
.venv/bin/python -m tools.catalog snapshot \
  --url 'https://plants.sc.egov.usda.gov/DocumentLibrary/Txt/plantlst.txt' \
  --license 'USDA NRCS PLANTS plant data: not copyrighted; free for any use; cite the USDA NRCS PLANTS Database' \
  --out data/inputs/usda.csv
```

The [USDA checklist](https://plants.sc.egov.usda.gov/DocumentLibrary/Txt/plantlst.txt) is comma-delimited text despite its `.txt` extension, so saving it as `usda.csv` requires no conversion. The command writes both `data/inputs/usda.csv` and `data/inputs/usda.csv.source.json`. USDA's [Plant Data Use terms](https://plants.sc.egov.usda.gov/DocumentLibrary/Pdf/PLANTS_Help_Document.pdf) allow reuse of plant lists and request citation; photographs have separate terms. Once these two files exist, continue with observation collection. Do not rerun a separate `snapshot --file` command for the same checklist.

Accepted USDA columns: `Symbol`, `Synonym Symbol`, `Scientific Name with Author`, `National Common Name`, `Family`. A synonym row's `Symbol` refers to its accepted taxon. Normalized exports may instead use `symbol`, `accepted_symbol`, `scientific_name`, `common_name`, `family`; `accepted_symbol` is empty on accepted records. Species-level records qualify; infraspecific names and hybrids are excluded. Duplicate or ambiguous name matches never qualify. The tool does not infer state nativity from USDA's lower-48 status.

A generic `snapshot --url ... --license ... --out ...` command can cache direct public downloads. Source records contain URL, retrieval time, license, and SHA-256. Do not provide a webpage URL when file bytes are required. The `snapshot --file` path is intentionally supported for exports requiring a browser.

## 3. Collect bounded national observation samples

```sh
python3 -m tools.catalog collect --areas data/inputs/areas.json \
  --cache data/cache --out data/inputs/inaturalist \
  --user-agent 'EdufleurCatalog/0.1 CONTACT_URL_OR_EMAIL'
```

The collector checks geography for all 48 states, resolves U.S. state places, then samples up to 40 candidate species per state/ecoregion bounding box and 12 observations per species/status. The builder performs the exact polygon checks later. These samples are for course construction, not an exhaustive flora or an abundance estimate. Native and introduced passes preserve conflicting status evidence. Community metadata may be incomplete, so qualifying coverage can be sparse.

Requests use at least 1.05 seconds spacing, bounded retries, and a shared daily budget of 9,000 network requests per cache. A large import may require several days. Rerun the same command to resume from verified URL-keyed snapshots. A network failure aborts collection and leaves cached work available; it does not publish a partial catalog. `--offline` requires every requested response already to be cached. Refresh intentionally by using a new cache directory; immutable snapshots are not silently overwritten.

The normalized observation snapshot includes `observations` from API responses, full `places` records referenced by establishment status, source metadata, sampled area IDs, and sampling limits. Native evidence must explicitly name the U.S. state (admin level 10, with U.S. ancestor place 1); country-wide evidence is rejected. Introduced conflicts disqualify the taxon in that state.

## 4. Build and validate

```sh
python3 -m tools.catalog manifest \
  --areas data/inputs/areas.json --usda data/inputs/usda.csv \
  --observations data/inputs/inaturalist/observations.json \
  --out data/inputs/manifest.json
python3 -m tools.catalog build --manifest data/inputs/manifest.json \
  --cache data/cache --out data/staging/catalog-001
python3 -m tools.catalog validate --bundle data/staging/catalog-001
```

Choose a new output directory for each build. `build --offline` reuses image snapshots without networking. Build failure deletes its staging output and leaves published versions untouched.

Publication thresholds for generator `ecoregion-photo-v1`:

- At least ten qualifying species per course; units contain up to five species.
- Each species requires three observations, two observers, and three distinct image files from distinct observations in that course area.
- Observations must be research grade, explicitly wild, publicly located, and have reported uncertainty at most 1 km. A conservative lower-48 geometry check rejects circles near boundaries and polygon holes. Missing accuracy does not count as zero.
- Names must resolve unambiguously to the USDA checklist. Native evidence must be state-specific, with no conflicting introduced status in the snapshot.
- Each image must be CC0 or CC BY, include attribution, decode successfully, and have both dimensions at least 640px. Decoded images are resized to at most 1600px, stripped of EXIF, and saved as JPEG. Downloads are restricted to known iNaturalist image hosts with HTTPS and bounded sizes; cross-host redirects fail closed.
- Duplicate image content is deduplicated; content associated with multiple taxa is excluded. The teaching and question photos come from different observations. Answer options prefer the same genus/family and use scientific names to resolve duplicate common names.

Thresholds are recorded in every bundle. Changing them requires a generator-version change. Generation is repeatable from the same source snapshots, exclusions, and tool versions. Retain the preprocessing environment when exact byte reproduction matters, including Pillow and Python versions.

Outputs include normalized `catalog.db`, `images/`, `sources.json`, `quarantine.json`, `coverage.json`, and checksummed `bundle.json`. Coverage distinguishes missing geography, incomplete observation sampling, insufficient regional evidence, and photo/observer thresholds. A report with all state geometries is not automatically a complete national observation sample. Inspect both completion flags and per-course counts.

## 5. Publish while the app is stopped

```sh
python3 -m tools.catalog publish --bundle data/staging/catalog-001 --root data/catalog
```

The command verifies checksums/database integrity, copies an immutable version under `data/catalog/versions/`, and atomically changes the relative `current` symlink. Start or restart the app afterward. For rollback, publish a retained prior version through the same command. Axum resolves the bundle directory at startup, so its in-memory lessons and locally served images stay on the same version.

The progress database is separate. Existing practiced taxa remain practiced when images or lesson order change; removed taxa stop contributing to visible counts but their history is retained. Legacy text-lesson progress stays in its original table and is not treated as completed photographic exercises.

## Reports and exclusions

```sh
python3 -m tools.catalog reports --db data/edufleur.db
python3 -m tools.catalog exclude --image IMAGE_SHA256 --file data/exclusions.json
python3 -m tools.catalog build --manifest data/inputs/manifest.json \
  --cache data/cache --exclusions data/exclusions.json --out data/staging/catalog-002
```

Exclusions accept published image hashes or numeric iNaturalist photo IDs. Rebuild, validate, stop, publish, and restart to apply them. Reports are deduplicated by learner/image and never automatically censor content. If exclusions drop a course below thresholds, that course becomes unavailable in the next bundle.

## Current acquisition limitation

The EPA geography and uploaded Zenodo taxonomy can now be prepared locally without further USDA downloads. Direct USDA requests have returned gateway errors; the archive adapter avoids that dependency. In this execution environment, DNS still prevents live iNaturalist collection. Real photo courses remain unavailable until observation and licensed-image collection completes; taxonomy import alone does not establish national playable-course coverage.

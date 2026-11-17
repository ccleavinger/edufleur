# Edufleur

A mobile-friendly native plant learning app. SolidJS and Vite provide the frontend; Rust Axum serves the API and built frontend. Local libsql stores progress. One container, no AWS services or remote database.

The landing page (`/`) introduces the app. `/habitats` browses state/ecoregion courses; `/courses/:id` supports direct links, five-species introductions, photo quizzes, retries, and image reports. Course progress is independent across areas and survives catalog rebuilds.

**Catalog status:** the app supports dataset-backed generation, including an offline adapter for uploaded EOL/Zenodo USDA archives. EPA geography and archived taxonomy have been prepared locally; source datasets are not committed or bundled in the container. Real national photo courses still require iNaturalist observations and licensed images, whose live collection is blocked by DNS in this execution environment. Until an operator publishes a qualifying catalog, the habitat browser shows an unpublished-catalog state. The original text-only sample catalog is retired; its progress table is retained.

## Development

Requires Rust stable and its C/C++ build prerequisites, Node 24, and npm.

```sh
cd frontend
npm ci
npm run dev
```

In another terminal, from the repository root:

```sh
cargo run --locked
```

Open http://localhost:5173. Vite proxies API and image requests to Axum on port 3000. `npm` and `package-lock.json` are the canonical frontend dependency workflow.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_PATH` | `edufleur.db` | Writable progress/report database; retains legacy progress |
| `CATALOG_DIR` | `data/catalog/current` | Published bundle containing read-only `catalog.db` and `images/` |
| `BIND_ADDRESS` | `127.0.0.1:3000` | HTTP listener |

## One-container deployment

```sh
mkdir -p data
```

Build and run from the repository root:

```sh
docker build -t edufleur .
docker run --rm --name edufleur --user "$(id -u):$(id -g)" \
  -p 3000:3000 -v "$PWD/data:/data" edufleur
```

Open http://localhost:3000. The user override allows the container to write the host-owned data directory. The runtime contains Axum, the static frontend, and libsql; Python/GIS tools run separately on the operator's machine. A public deployment needs HTTPS through the host's reverse proxy.

Use the [catalog runbook](docs/catalog.md) to prepare and publish datasets. Stop the app before switching bundles, then restart. Catalog images and learner data both remain on the mounted volume. Back up progress using SQLite's backup API, or stop the app before copying database files. Retain the previous catalog version for rollback.

## Verification

Python tests require Pillow; the full preparation tool also uses pyshp and pyproj. Install `tools/catalog/requirements.txt` in a virtual environment.

```sh
cargo fmt --check
cargo test --locked
python3 -m unittest discover -s tests -v
cd frontend
npm test
```

Rust tests run real generated catalogs through Axum's HTTP router without opening sockets. They check hidden answer keys, grading, version conflicts, idempotent progress, image serving/reporting, catalog rebuilds, and legacy data retention. Frontend DOM tests execute the production bundle and cover navigation, direct course links, state selection, introductions, retries, reporting, and the unpublished state. Manual visual review at mobile/desktop widths is still needed; DOM tests do not measure layout.

## Data and application boundaries

The offline Python tool snapshots sources, normalizes USDA taxonomy, joins precise observations to EPA Level III polygons, requires explicit state-level native status, validates licensed photos, and generates deterministic course bundles. [Source decisions and limitations](docs/data-sources.md) explain what each dataset establishes.

The application never fetches biodiversity APIs during gameplay. libsql is sufficient for local catalogs and progress; remote replication and a second serving process add no benefit at this stage. GIS preprocessing is a better fit for Python's existing libraries than extra geospatial dependencies in the Rust server.

Browser progress uses a random identifier in localStorage. This is prototype persistence, not account authentication: clearing storage loses access, and knowing an identifier permits reading/modifying that progress. Accounts, cross-device sync, finer habitats, and spaced repetition remain future work.

## API

- `GET /api/health`: server health.
- `GET /api/catalog`: publication state, version, and coverage report.
- `GET /api/courses?state=CO&ecoregion=21`: filtered summaries, including unavailable courses.
- `GET /api/courses/{id}`: course units, introductions, attribution, and questions without answer keys.
- `GET /api/progress`: active-course practiced counts for `X-Learner-Id`.
- `POST /api/courses/{id}/answers`: `{ "question": "...", "choice": 0, "catalogVersion": "..." }`. Stale versions return 409.
- `POST /api/images/{id}/reports`: `{ "reason": "wrongPlant", "details": "..." }`; reasons also include `unhelpful`, `license`, `other`. Details are limited to 500 characters. Reports require `X-Learner-Id` and do not automatically remove images.
- `/media/{sha256}.jpg`: locally served licensed photographs.

# Data source decisions

Use an offline, versioned catalog. Course generation and serving have different jobs: the preparation tool gathers evidence and licensed media, while gameplay reads local files and libsql. See the [runbook](catalog.md) for commands.

| Need | Source | What it establishes |
| --- | --- | --- |
| Course geography | [EPA Level III ecoregions with state boundaries](https://www.epa.gov/eco-research/level-iii-and-iv-ecoregions-continental-united-states) | Stable ecological-area boundaries; these are broader than individual woodland/prairie habitats. |
| Plant taxonomy | [USDA PLANTS](https://plants.sc.egov.usda.gov/home/index.html) or the [EOL/Zenodo USDA archive](https://zenodo.org/records/17903503) | The complete checklist supports synonyms. The offline archive adapter imports a smaller, versioned accepted-species taxonomy without a synonym crosswalk. |
| Regional native evidence | [iNaturalist establishment metadata](https://raw.githubusercontent.com/inaturalist/iNaturalistAPI/main/lib/views/swagger_v1.yml.ejs) | Explicit place-based status, restricted in this implementation to U.S. states. Missing or conflicting status is excluded. This is community evidence, not a botanical authority guarantee. |
| Evidence of local presence and candidate photos | [iNaturalist API](https://www.inaturalist.org/pages/developers) | Research-grade wild observations screened for location uncertainty, licensing, and distinct observers. They do not establish abundance or exhaustive presence. |
| Future finer habitat divisions | [LANDFIRE Existing Vegetation Type](https://landfire.gov/vegetation/evt) | Vegetation mapping suitable for a later geographic refinement; not needed for the first ecoregion courses. |
| Future taxonomic/media supplement | [GBIF Taxon and Occurrence APIs](https://techdocs.gbif.org/en/openapi/) | Additional names and records; deduplicate shared iNaturalist records. Not required by the current importer. |

USDA's [native-status documentation](https://plants.usda.gov/DocumentLibrary/Pdf/PLANTS_Help_Document.pdf) defines a lower-48 native jurisdiction. A species native somewhere in that jurisdiction may be introduced elsewhere within it. Combining an L48-native flag with a local observation is therefore insufficient to claim local nativity. This implementation requires explicit state-level status plus course-area presence and excludes contradictory evidence. It does not claim that state status proves nativity at every point of an ecoregion.

A broad ecoregion can contain several habitats. Its observed species list does not imply every plant occurs in all those habitats. Initial course descriptions deliberately refer to documented observations in an ecological area.

## Images and publication

Use CC0 and CC BY photos, validating each photo's license independently from its observation. The collector also restricts observation-record licenses to CC0/CC BY. Retain provider attribution, source page, original image URL, license URL, retrieval date, and checksums. The original credit string is preserved in the creator/attribution fields. Images are resized and re-encoded; source image snapshots remain in the operator cache.

[iNaturalist's default is CC BY-NC](https://help.inaturalist.org/en/support/solutions/articles/151000175695), so an unfiltered feed would constrain possible commercial use. [Its media-reuse guide](https://help.inaturalist.org/en/support/solutions/articles/151000169918-can-i-use-the-photos-and-sounds-that-are-posted-on-inaturalist-) explains why media permissions must be checked individually. External image hosting does not remove these obligations.

Photos publish automatically after eligibility checks, with attribution and an in-app reporting action. Research grade is a useful screen, not a guarantee of correct identity or a useful teaching image. Operators can exclude questionable images and rebuild. No generated botanical descriptions are used; feedback states the sourced name and encourages comparison with the introduction photo.

## Provider use and reproducibility

Follow [iNaturalist's recommended practices](https://www.inaturalist.org/pages/api%2Brecommended%2Bpractices): roughly one request per second, fewer than 10,000 daily requests, and exports instead of indiscriminate bulk harvesting. The collector takes bounded candidate samples per ecoregion and caches responses. A national run may take multiple days. For a future exhaustive occurrence pipeline, add a bulk-export adapter rather than lifting these limits.

The importer uses one local cache and no distributed workers. Interrupted work resumes from snapshots; newly retrieved data goes into a deliberately new cache for refreshes. Download or license failure never silently substitutes a different source. The serving container makes no requests to these providers.

No AWS services are provisioned or needed. Some third-party source URLs may themselves use their provider's existing object hosting; the published app serves downloaded images from its local volume.

## Archived EOL taxonomy

`usda-dwca` converts the uploaded Darwin Core Archive into the existing normalized CSV interface. Published MD5 values identify known pinned versions; SHA-256 records exact local bytes. Factual USDA symbols, names, and families are copied, while unknown identifiers, conflicting names, non-species ranks, hybrids, and unsupported names are excluded or quarantined. The import report documents counts and limitations. Common-name gaps can be filled from iNaturalist observation metadata.

The overall Zenodo license is unspecified. The original USDA plant-data terms apply to the factual taxonomic source; this does not assign rights to the full archive. Its media examples and trait descriptions are excluded. Broad native-range measurements do not replace explicit state-native evidence.

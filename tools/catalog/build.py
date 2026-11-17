"""Normalize evidence and generate a self-contained, immutable catalog bundle."""
from contextlib import closing
import csv
import io
import json
import math
import os
import platform
import random
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from .common import DEFAULTS, GENERATOR, STATES, atomic_json, canonical, digest, source_record
from .geo import bounds, contains_uncertainty

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources (id TEXT PRIMARY KEY, url TEXT NOT NULL, license TEXT NOT NULL, checksum TEXT NOT NULL, retrieved_at TEXT NOT NULL);
CREATE TABLE taxa (id TEXT PRIMARY KEY, scientific_name TEXT NOT NULL, common_name TEXT NOT NULL, genus TEXT NOT NULL, family TEXT, usda_symbol TEXT NOT NULL);
CREATE TABLE aliases (name TEXT NOT NULL, taxon TEXT NOT NULL REFERENCES taxa(id), PRIMARY KEY(name,taxon));
CREATE TABLE areas (id TEXT PRIMARY KEY, state TEXT NOT NULL, ecoregion TEXT NOT NULL, name TEXT NOT NULL, geometry TEXT NOT NULL);
CREATE TABLE native_evidence (taxon TEXT NOT NULL REFERENCES taxa(id), state TEXT NOT NULL, status TEXT NOT NULL, place_id INTEGER NOT NULL, source_id TEXT NOT NULL REFERENCES sources(id), PRIMARY KEY(taxon,state,status,place_id,source_id));
CREATE TABLE observations (id INTEGER PRIMARY KEY, taxon TEXT NOT NULL REFERENCES taxa(id), observer TEXT NOT NULL, longitude REAL NOT NULL, latitude REAL NOT NULL, uncertainty REAL NOT NULL, source_id TEXT NOT NULL REFERENCES sources(id));
CREATE TABLE images (id TEXT PRIMARY KEY, observation INTEGER NOT NULL REFERENCES observations(id), url TEXT NOT NULL, source_url TEXT NOT NULL, creator TEXT NOT NULL, license TEXT NOT NULL, license_url TEXT NOT NULL, attribution TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES sources(id), width INTEGER NOT NULL, height INTEGER NOT NULL);
CREATE TABLE courses (id TEXT PRIMARY KEY REFERENCES areas(id), payload TEXT NOT NULL);
CREATE TABLE course_taxa (course TEXT REFERENCES courses(id), taxon TEXT REFERENCES taxa(id), PRIMARY KEY(course,taxon));
CREATE TABLE questions (id TEXT PRIMARY KEY, course TEXT REFERENCES courses(id), taxon TEXT REFERENCES taxa(id), image TEXT REFERENCES images(id), type TEXT NOT NULL, payload TEXT NOT NULL);
'''
LICENSES = {'cc0': 'https://creativecommons.org/publicdomain/zero/1.0/', 'cc-by': 'https://creativecommons.org/licenses/by/4.0/'}
PHOTO_HOSTS = {'inaturalist-open-data.s3.amazonaws.com', 'static.inaturalist.org', 'www.inaturalist.org'}


def binomial(name):
    if re.search(r'\b(?:subsp|ssp|var|subvar|forma)\.|[×]', name):
        return None
    match = re.match(r'^([A-Z][a-z-]+) ([a-z-]+)(?:\s|$)', name.strip())
    if not match:
        return None
    # A form marker follows the species epithet. The same abbreviation in an
    # author citation, such as "L. f." or "Michx. f.", means filius, not forma.
    if re.match(r'f\.\s+[a-z]', name.strip()[match.end():].lstrip()):
        return None
    return ' '.join(match.groups())


def load_usda(path):
    """USDA complete checklist or normalized export; ambiguous synonyms stay ambiguous."""
    taxa, names = {}, defaultdict(set)
    with Path(path).open(encoding='utf-8-sig', newline='') as file:
        rows = list(csv.DictReader(file))
    def field(row, *keys):
        return next((row[k].strip() for k in keys if row.get(k)), '')
    for row in rows:
        symbol = field(row, 'symbol', 'Symbol')
        accepted = field(row, 'accepted_symbol', 'Accepted Symbol')
        synonym = field(row, 'Synonym Symbol')
        # USDA rows retain the accepted Symbol; Synonym Symbol identifies a synonym row.
        scientific = field(row, 'scientific_name', 'Scientific Name with Author', 'Scientific Name')
        name = binomial(scientific)
        if not symbol or not name:
            continue
        target = accepted or symbol
        names[name].add(target)
        if not synonym and (not accepted or accepted == symbol):
            taxa[symbol] = {'symbol': symbol, 'scientificName': name,
                            'commonName': field(row, 'common_name', 'National Common Name', 'Common Name'),
                            'family': field(row, 'family', 'Family')}
    if not taxa:
        raise ValueError('USDA export needs Symbol and Scientific Name with Author columns (or normalized equivalents)')
    return taxa, names


def load_areas(path):
    collection = json.loads(Path(path).read_text())
    if collection.get('type') != 'FeatureCollection':
        raise ValueError('Areas must be a WGS84 GeoJSON FeatureCollection')
    if collection.get('crs') and '4326' not in canonical(collection['crs']) and 'CRS84' not in canonical(collection['crs']):
        raise ValueError('Reproject areas to WGS84 first')
    areas = {}
    for feature in collection['features']:
        p, geometry = feature['properties'], feature['geometry']
        state = p.get('state') or p.get('STATE_ABBR') or p.get('STATE_NAME')
        state = next((k for k, v in STATES.items() if v == state), state)
        if state not in STATES:
            continue
        code = str(p.get('ecoregionCode') or p.get('US_L3CODE') or '').strip()
        name = p.get('name') or p.get('US_L3NAME')
        if not code or not name or geometry['type'] not in ('Polygon', 'MultiPolygon'):
            raise ValueError('Each area needs state, ecoregionCode, name and polygon geometry')
        polygons = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
        for polygon in polygons:
            for ring in polygon:
                if len(ring) < 4 or ring[0] != ring[-1]:
                    raise ValueError('Area rings must be closed with at least four coordinates')
                if any(not (-180 <= point[0] <= 180 and -90 <= point[1] <= 90) for point in ring):
                    raise ValueError('Area coordinates must use longitude/latitude')
        key = f'{state.lower()}-epa3-{code}'
        if key in areas:
            old = areas[key]['geometry']
            old_polygons = [old['coordinates']] if old['type'] == 'Polygon' else old['coordinates']
            areas[key]['geometry'] = {'type': 'MultiPolygon', 'coordinates': old_polygons + polygons}
        else:
            areas[key] = dict(id=key, state=state, ecoregionCode=code, name=name, geometry=geometry)
    if not areas:
        raise ValueError('No lower-48 areas found')
    for area in areas.values():
        area['bounds'] = bounds(area['geometry'])
    return sorted(areas.values(), key=lambda a: a['id'])


def state_for_place(place):
    # Do not infer a state from a country-level native flag or an observation's state.
    if place.get('admin_level') != 10 or place.get('place_type') not in (8, None) or 1 not in place.get('ancestor_place_ids', []):
        return None
    return next((code for code, name in STATES.items() if place.get('name') == name), None)


def validated_image(path):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 30_000_000
    with Image.open(path) as original:
        if original.width * original.height > 30_000_000:
            raise ValueError('Photo exceeds the decoded pixel limit')
        original.verify()
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert('RGB')
        if min(image.size) < 640:
            raise ValueError('Photo shorter dimension must be at least 640 pixels')
        image.thumbnail((1600, 1600))
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=88, optimize=True)
        content = output.getvalue()
        key = digest(content)
        return key, image.width, image.height, content


def prepare(manifest_path, output, cache, exclusions=None, config=None):
    manifest_path, output = Path(manifest_path), Path(output)
    manifest = json.loads(manifest_path.read_text())
    root = manifest_path.parent
    settings = dict(DEFAULTS, **(config or {}))
    if settings != DEFAULTS:
        raise ValueError('Publication thresholds are fixed for generator v1; change generator version to change thresholds')
    if output.exists():
        raise ValueError('Output already exists; choose a new staging directory')
    areas, usda, observations = [root / manifest[k]['path'] for k in ('areas', 'usda', 'observations')]
    for key, path in zip(('areas', 'usda', 'observations'), (areas, usda, observations)):
        item = manifest[key]
        if not item.get('url') or not item.get('license') or not item.get('retrievedAt') or digest(path.read_bytes()) != item.get('checksum'):
            raise ValueError(f'Unverified source manifest: {key}')
    area_rows = load_areas(areas)
    usda_taxa, names = load_usda(usda)
    aliases_by_symbol = defaultdict(list)
    for name, symbols in names.items():
        if len(symbols)==1:
            aliases_by_symbol[next(iter(symbols))].append(name)
    raw = json.loads(observations.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get('observations'), list) or not isinstance(raw.get('places'), list):
        raise ValueError('Observation snapshot needs observations and places arrays')
    excluded = set(json.loads(Path(exclusions).read_text())) if exclusions else set()
    from PIL import __version__ as pillow_version, features
    version_inputs = {'toolchain': {'python':platform.python_version(),'pillow':pillow_version,'jpeg':features.version_codec('jpg'),'sqlite':sqlite3.sqlite_version}, 'generator': GENERATOR, 'settings': settings, 'inputs': {k:manifest[k]['checksum'] for k in ('areas','usda','observations')}, 'excluded': sorted(excluded)}
    output.mkdir(parents=True)
    image_dir = output / 'images'
    image_dir.mkdir()
    db = sqlite3.connect(output / 'catalog.db')
    try:
        db.executescript(SCHEMA)
        sources = {}
        def source(record):
            record = {k:v for k,v in record.items() if k!='path'}
            key = digest(canonical({k:record[k] for k in ('url','license','checksum')}).encode())
            if key not in sources:
                db.execute('INSERT INTO sources VALUES (?,?,?,?,?)', (key, record['url'], record['license'], record['checksum'], record['retrievedAt']))
                sources[key] = record
            return key
        for key in ('areas', 'usda', 'observations'):
            source(manifest[key])
        observation_source = source(manifest['observations'])
        for record in raw.get('sources', []):
            source(record)
        places = {p['id']:p for p in raw['places']}
        counters = Counter()
        taxa, eligible, media, seen_observations = {}, defaultdict(list), {}, set()
        evidence = defaultdict(set)
        matched = {}
        quarantine = []
        area_rejections = defaultdict(Counter)
        media_taxa = defaultdict(set)
        # First pass retains contradictory native status before applying eligibility filters.
        for observation in raw['observations']:
            taxon = observation.get('taxon') or {}
            if taxon.get('rank') != 'species' or taxon.get('is_active') is not True:
                counters['notActiveSpecies'] += 1
                continue
            name = binomial(taxon.get('name', ''))
            symbols = names.get(name, set())
            if len(symbols) != 1 or next(iter(symbols)) not in usda_taxa:
                counters['unmatchedOrAmbiguousTaxonomy'] += 1
                quarantine.append({'taxonId':taxon.get('id'),'name':name,'reason':'unmatchedOrAmbiguousTaxonomy'})
                continue
            symbol = next(iter(symbols))
            taxon_id = f'inat:{taxon["id"]}'
            # One USDA accepted identity must not map onto several active iNaturalist species.
            matched.setdefault(symbol, set()).add(taxon_id)
            record = dict(usda_taxa[symbol], id=taxon_id, scientificName=taxon['name'], genus=taxon['name'].split()[0], commonName=usda_taxa[symbol]['commonName'] or taxon.get('preferred_common_name', ''))
            if taxon_id in taxa and taxa[taxon_id]['symbol'] != symbol:
                counters['conflictingTaxonomy'] += 1
                continue
            taxa[taxon_id] = record
            em = taxon.get('establishment_means') or {}
            place_id = (em.get('place') or {}).get('id')
            place = places.get(place_id, {})
            state = state_for_place(place)
            if state and em.get('establishment_means') in ('native', 'introduced'):
                evidence[(taxon_id, state)].add((em['establishment_means'], place_id))
        ambiguous = {taxon_id for ids in matched.values() if len(ids) > 1 for taxon_id in ids}
        for taxon_id, t in sorted(taxa.items()):
            db.execute('INSERT INTO taxa VALUES (?,?,?,?,?,?)', (taxon_id,t['scientificName'],t['commonName'] or t['scientificName'],t['genus'],t['family'],t['symbol']))
            for name in aliases_by_symbol[t['symbol']]:
                db.execute('INSERT OR IGNORE INTO aliases VALUES (?,?)',(name,taxon_id))
        for (taxon_id,state), values in sorted(evidence.items()):
            for status, place_id in sorted(values):
                db.execute('INSERT INTO native_evidence VALUES (?,?,?,?,?)',(taxon_id,state,status,place_id,observation_source))
        image_failures = []
        for observation in sorted(raw['observations'], key=lambda o:o['id']):
            oid = observation['id']
            if oid in seen_observations:
                counters['duplicateObservations'] += 1
                continue
            seen_observations.add(oid)
            taxon = observation.get('taxon') or {}
            taxon_id = f'inat:{taxon.get("id")}'
            if taxon_id not in taxa or taxon_id in ambiguous:
                continue
            if observation.get('quality_grade') != 'research' or observation.get('captive') is not False or observation.get('geoprivacy') not in (None,'open') or observation.get('taxon_geoprivacy') not in (None,'open') or observation.get('obscured'):
                counters['qualityOrPrivacy'] += 1
                continue
            point = (observation.get('geojson') or {}).get('coordinates')
            uncertainty = observation.get('positional_accuracy')
            observer = (observation.get('user') or {}).get('id')
            if not point or uncertainty is None or not isinstance(uncertainty,(int,float)) or observer is None:
                counters['missingLocationOrObserver'] += 1
                continue
            lon,lat = point
            candidates = [a for a in area_rows if a['bounds'][0] < lon < a['bounds'][2] and a['bounds'][1] < lat < a['bounds'][3] and contains_uncertainty(a['geometry'],lon,lat,uncertainty)]
            if not candidates:
                counters['outsideOrUncertainBoundary'] += 1
                continue
            for area in candidates:
                if {status for status,_ in evidence[(taxon_id,area['state'])]} != {'native'}:
                    area_rejections[area['id']]['insufficientOrConflictingNativeStatus'] += 1
            candidates = [a for a in candidates if {status for status,_ in evidence[(taxon_id,a['state'])]} == {'native'}]
            if not candidates:
                counters['insufficientOrConflictingNativeStatus'] += 1
                continue
            db.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?)',(oid,taxon_id,str(observer),lon,lat,uncertainty,observation_source))
            photos = []
            for photo in sorted(observation.get('photos') or [], key=lambda p:p['id']):
                license_code = photo.get('license_code')
                if license_code not in LICENSES or not photo.get('attribution'):
                    counters['incompatibleOrMissingPhotoLicense'] += 1
                    continue
                if str(photo['id']) in excluded:
                    counters['excludedPhoto'] += 1
                    continue
                url = photo.get('original_url') or photo.get('url','').replace('/square.', '/large.')
                path, record = cache.fetch(url, license_code, max_bytes=8_000_000, hosts=PHOTO_HOSTS)
                try:
                    image_id,width,height,content = validated_image(path)
                    if image_id in excluded:
                        counters['excludedPhoto'] += 1
                        continue
                except (ValueError, OSError) as error:
                    counters['invalidImage'] += 1
                    image_failures.append({'photoId':photo['id'],'reason':str(error)})
                    continue
                (image_dir / (image_id + '.jpg')).write_bytes(content)
                media_taxa[image_id].add(taxon_id)
                if image_id not in media:
                    image = dict(id=image_id,url=f'/media/{image_id}.jpg',sourceUrl=f'https://www.inaturalist.org/observations/{oid}',creator=photo.get('attribution'),license=license_code,licenseUrl=LICENSES[license_code],attribution=photo['attribution'])
                    media[image_id] = image
                    db.execute('INSERT INTO images VALUES (?,?,?,?,?,?,?,?,?,?,?)',(image_id,oid,url,image['sourceUrl'],image['creator'],license_code,LICENSES[license_code],image['attribution'],source(record),width,height))
                photos.append(image_id)
            for area in candidates:
                eligible[(area['id'],taxon_id)].append(dict(id=oid,observer=observer,images=sorted(set(photos))))
        conflicting_media = {key for key,tids in media_taxa.items() if len(tids)>1}
        counters['conflictingImageTaxonomy'] = len(conflicting_media)
        for image_id in conflicting_media:
            db.execute('DELETE FROM images WHERE id=?',(image_id,))
        for records in eligible.values():
            for record in records:
                record['images'] = [i for i in record['images'] if i not in conflicting_media]
        version_inputs['sources'] = sorted(sources.values(),key=lambda r:(r['url'],r['checksum']))
        version = digest(canonical(version_inputs).encode())[:20]
        coverage = []
        for area in area_rows:
            db.execute('INSERT INTO areas VALUES (?,?,?,?,?)',(area['id'],area['state'],area['ecoregionCode'],area['name'],canonical(area['geometry'])))
            qualifying = []
            area_counts = Counter(area_rejections[area['id']])
            for (aid,tid), records in sorted(eligible.items()):
                if aid != area['id']:
                    continue
                # One photo per observation prevents one visit filling all image slots.
                chosen,used = [],set()
                for record in sorted(records,key=lambda r:r['id']):
                    image = next((i for i in record['images'] if i not in used),None)
                    if image:
                        chosen.append(image)
                        used.add(image)
                checks = [len(records)>=settings['minObservations'], len({r['observer'] for r in records})>=settings['minObservers'],len(chosen)>=settings['minImages']]
                if all(checks):
                    qualifying.append(dict(taxon=taxa[tid],images=chosen,observations=len(records)))
                else:
                    area_counts['insufficientObservations'] += not checks[0]
                    area_counts['insufficientObservers'] += not checks[1]
                    area_counts['insufficientImages'] += not checks[2]
            # Counts describe dataset support, not ecological abundance.
            qualifying.sort(key=lambda item:(-item['observations'],item['taxon']['id']))
            available = len(qualifying) >= settings['minSpecies']
            units = []
            counts = Counter(q['taxon']['commonName'].casefold() for q in qualifying if q['taxon']['commonName'])
            labels_by_taxon = {q['taxon']['id']: (q['taxon']['commonName'] if q['taxon']['commonName'] and counts[q['taxon']['commonName'].casefold()]==1 else q['taxon']['scientificName']) for q in qualifying}
            if available:
                for offset in range(0,len(qualifying),settings['unitSize']):
                    chunk = qualifying[offset:offset+settings['unitSize']]
                    intro,questions = [],[]
                    for item in chunk:
                        t = item['taxon']
                        title = labels_by_taxon[t['id']]
                        intro.append(dict(taxonId=t['id'],name=title,scientificName=t['scientificName'],image=media[item['images'][0]]))
                        others = [q['taxon'] for q in qualifying if q['taxon']['id'] != t['id']]
                        others.sort(key=lambda other:(other['genus']!=t['genus'], not(t['family'] and other['family']==t['family']),other['id']))
                        labels,seen = [],{title.casefold()}
                        for other in others:
                            label = labels_by_taxon[other['id']]
                            if label.casefold() in seen:
                                label = other['scientificName']
                            if label.casefold() not in seen:
                                labels.append(label)
                                seen.add(label.casefold())
                            if len(labels)==3:
                                break
                        if len(labels)!=3:
                            raise ValueError('Not enough distinct distractor names')
                        qid = digest(f'{area["id"]}:{t["id"]}:photo-id'.encode())[:24]
                        options = [title]+labels
                        random.Random(qid).shuffle(options)
                        question = dict(id=qid,taxonId=t['id'],exerciseType='photo-id',prompt='Which plant is shown?',image=media[item['images'][1]],options=options,correct=options.index(title),explanation=f'This is {title} ({t["scientificName"]}). Compare it with the introduction photo and notice how individuals vary.')
                        questions.append(question)
                    units.append(dict(id=f'{area["id"]}-unit-{offset//settings["unitSize"]+1}',title=f'Field notes {offset//settings["unitSize"]+1}',plants=intro,questions=questions))
            payload = dict(id=area['id'],state=STATES[area['state']],stateCode=area['state'],ecoregionCode=area['ecoregionCode'],habitat=area['name'],description='Practice plants with documented state-native status and precise wild observations in this ecological area.',available=available,reason=None if available else f'{len(qualifying)} of {settings["minSpecies"]} required species meet evidence and photo thresholds.',total=len(qualifying) if available else 0,catalogVersion=version,units=units)
            db.execute('INSERT INTO courses VALUES (?,?)',(area['id'],canonical(payload)))
            for unit in units:
                for question in unit['questions']:
                    db.execute('INSERT INTO course_taxa VALUES (?,?)',(area['id'],question['taxonId']))
                    db.execute('INSERT INTO questions VALUES (?,?,?,?,?,?)',(question['id'],area['id'],question['taxonId'],question['image']['id'],'photo-id',canonical(question)))
            coverage.append(dict(id=area['id'],state=area['state'],ecoregion=area['ecoregionCode'],name=area['name'],available=available,qualifyingSpecies=len(qualifying),candidateSpecies=sum(1 for aid,_ in eligible if aid==area['id']),exclusions=dict(area_counts)))
        represented = {a['state'] for a in area_rows}
        sampled = set(raw.get('sampledAreas', []))
        report = dict(version=version,generator=GENERATOR,settings=settings,states=[dict(code=code,name=name,areas=sum(a['state']==code for a in area_rows),availableCourses=sum(c['state']==code and c['available'] for c in coverage),sampledAreas=sum(a['state']==code and a['id'] in sampled for a in area_rows),status=('processed' if all(a['id'] in sampled for a in area_rows if a['state']==code) else 'incompleteObservationSampling') if code in represented else 'missingGeography') for code,name in STATES.items()],courses=coverage,exclusions=dict(counters),imageFailures=image_failures,sourceScope=manifest.get('scope','unspecified'),nationalGeographyComplete=represented==set(STATES),nationalObservationSamplingComplete=represented==set(STATES) and all(a['id'] in sampled for a in area_rows),sampling=raw.get('sampling'))
        for key,value in dict(schemaVersion=1,catalogVersion=version,generator=GENERATOR,coverage=report).items():
            db.execute('INSERT INTO metadata VALUES (?,?)',(key,canonical(value)))
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Generated catalog failed database integrity checks')
        db.close()
        atomic_json(output/'coverage.json',report)
        atomic_json(output/'quarantine.json',[json.loads(item) for item in sorted({canonical(item) for item in quarantine})])
        atomic_json(output/'sources.json',sorted(sources.values(),key=lambda r:(r['url'],r['checksum'])))
        # Drop rejected/unreferenced binaries, and record exact published bytes.
        with closing(sqlite3.connect(output/'catalog.db')) as check:
            referenced = {row[0] for row in check.execute('SELECT id FROM images')}
        for path in image_dir.iterdir():
            if path.stem not in referenced:
                path.unlink()
        hashes = {str(p.relative_to(output)):digest(p.read_bytes()) for p in sorted(output.rglob('*')) if p.is_file()}
        atomic_json(output/'bundle.json',dict(version=version,files=hashes,inputs=version_inputs))
        return report
    except BaseException:
        db.close()
        shutil.rmtree(output)
        raise


def validate_bundle(bundle):
    bundle = Path(bundle)
    manifest = json.loads((bundle/'bundle.json').read_text())
    if not re.fullmatch('[a-f0-9]{20}',manifest['version']):
        raise ValueError('Invalid bundle version')
    expected = set(manifest['files']) | {'bundle.json'}
    if any(p.is_symlink() for p in bundle.rglob('*')):
        raise ValueError('Bundle entries must not be symbolic links')
    actual = {str(p.relative_to(bundle)) for p in bundle.rglob('*') if p.is_file()}
    if expected != actual:
        raise ValueError('Bundle contains missing or unexpected files')
    for name, checksum in manifest['files'].items():
        path = bundle / name
        if Path(name).is_absolute() or '..' in Path(name).parts or path.is_symlink() or digest(path.read_bytes())!=checksum:
            raise ValueError(f'Bundle checksum/path failure: {name}')
    with closing(sqlite3.connect(f'file:{bundle.resolve()}/catalog.db?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Invalid catalog database')
        version = json.loads(db.execute("SELECT value FROM metadata WHERE key='catalogVersion'").fetchone()[0])
        if version!=manifest['version']:
            raise ValueError('Catalog and bundle versions differ')
    return manifest


def publish(bundle, root):
    bundle,root = Path(bundle).resolve(),Path(root).resolve()
    manifest = validate_bundle(bundle)
    versions = root/'versions'
    versions.mkdir(parents=True,exist_ok=True)
    target = versions/manifest['version']
    if target.exists():
        if validate_bundle(target)['files']!=manifest['files']:
            raise ValueError('Existing version has different bytes; refusing overwrite')
    else:
        staging = versions/(manifest['version']+'.staging')
        if staging.exists():
            shutil.rmtree(staging)
        try:
            shutil.copytree(bundle,staging)
            validate_bundle(staging)
            os.replace(staging,target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    link = root/'current.next'
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(Path('versions')/manifest['version'],target_is_directory=True)
    os.replace(link,root/'current')
    return target

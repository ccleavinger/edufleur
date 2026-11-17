"""python3 -m tools.catalog --help"""
import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

from .build import load_areas, prepare, publish, state_for_place, validate_bundle
from .common import STATES, Cache, atomic_json, canonical, digest, source_record


def collect(args):
    """Snapshot all requested wild observations, plus explicit status-place metadata.

    Search in native and introduced passes to retain contradictory regional
    evidence; URL caches make bounded per-area candidate sampling resumable.
    """
    cache = Cache(args.cache, args.user_agent, offline=args.offline)
    areas = load_areas(args.areas)
    by_state = {state:[a for a in areas if a['state']==state] for state in STATES}
    records, places, sources = {},{},[]
    for code in STATES:
        if not by_state[code]:
            raise ValueError(f'National collection requires geography for {code}')
        data,record = cache.json('places/autocomplete',{'q':STATES[code],'per_page':30})
        sources.append(record)
        matches=[]
        for candidate in data['results']:
            if candidate.get('name')!=STATES[code]:
                continue
            details,record=cache.json(f'places/{candidate["id"]}')
            sources.append(record)
            place=details['results'][0]
            if state_for_place(place)==code:
                matches.append(place)
        if len(matches)!=1:
            raise ValueError(f'Cannot uniquely resolve state place: {code}')
        place_id=matches[0]['id']
        places[place_id]=matches[0]
        for area in by_state[code]:
            geometry=area['geometry']
            def points(coords):
                if isinstance(coords[0],(int,float)):
                    yield coords
                else:
                    for item in coords:
                        yield from points(item)
            coordinates=list(points(geometry['coordinates']))
            bbox=dict(swlng=min(p[0] for p in coordinates),swlat=min(p[1] for p in coordinates),nelng=max(p[0] for p in coordinates),nelat=max(p[1] for p in coordinates))
            common=dict(place_id=place_id,taxon_id=47126,quality_grade='research',captive='false',photos='true',license='cc0,cc-by',photo_license='cc0,cc-by',**bbox)
            # A bounded candidate sample is sufficient for small educational courses.
            # Do not bulk-harvest the national observation database through this API.
            data,record=cache.json('observations/species_counts',dict(common,native='true',per_page=40,page=1))
            sources.append(record)
            for candidate in data['results']:
                taxon=candidate['taxon']
                if taxon.get('rank')!='species':
                    continue
                for status in ('native','introduced'):
                    params=dict(common,taxon_id=taxon['id'],per_page=12,order_by='id',order='asc',**{status:'true'})
                    data,record=cache.json('observations',params)
                    sources.append(record)
                    for obs in data['results']:
                        records[obs['id']]=obs
                        status_place=(((obs.get('taxon') or {}).get('establishment_means') or {}).get('place') or {}).get('id')
                        if status_place and status_place not in places:
                            result,record=cache.json(f'places/{status_place}')
                            sources.append(record)
                            places[status_place]=result['results'][0]
            print(f'{code}/{area["ecoregionCode"]}: {len(records)} total candidate observations snapshotted',flush=True)
    out = Path(args.out)
    out.mkdir(parents=True,exist_ok=True)
    target=out/'observations.json'
    atomic_json(target,dict(observations=[records[k] for k in sorted(records)],places=[places[k] for k in sorted(places)],sources=sources,sampledAreas=[a['id'] for a in areas],sampling={'maxCandidateSpeciesPerArea':40,'maxObservationsPerSpeciesStatusArea':12,'coverage':'lower48'}))
    atomic_json(out/'observations.source.json',source_record(target,'https://api.inaturalist.org/v1/observations','Per-record iNaturalist licenses; metadata sources embedded'))


def convert_areas(args):
    from tempfile import TemporaryDirectory
    from urllib.parse import urlparse
    from .area_input import EPA_AREAS_URL, EPA_AREAS_LICENSE, shapefile_input

    if args.download_epa:
        path, download = Cache(args.cache).fetch(EPA_AREAS_URL, EPA_AREAS_LICENSE, max_bytes=60_000_000)
        # URL-keyed cache files have no suffix; retain .zip for input detection.
        with TemporaryDirectory(prefix='edufleur-epa-download-') as directory:
            archive = Path(directory) / 'epa.zip'
            shutil.copyfile(path, archive)
            return convert_areas(argparse.Namespace(
                shapefile=str(archive), download_epa=False, cache=args.cache,
                url=download['url'], license=download['license'], out=args.out,
            ))
    with shapefile_input(args.shapefile) as files:
        url = urlparse(args.url or '')
        if url.scheme not in ('https', 'http') or not url.hostname:
            raise ValueError('--url must be the actual source URL, not SOURCE_DOWNLOAD_URL. Use --download-epa to download and record the official EPA source automatically.')
        if not args.license or args.license == 'SOURCE_LICENSE_FROM_METADATA':
            raise ValueError('--license must describe the source metadata terms, not SOURCE_LICENSE_FROM_METADATA. Use --download-epa for the official EPA dataset.')
        try:
            import shapefile
            from pyproj import Transformer
        except ImportError as error:
            raise ValueError('Area conversion requires pyshp and pyproj. Activate .venv and run: python3 -m pip install -r tools/catalog/requirements.txt') from error
        try:
            transformer = Transformer.from_crs(files['.prj'].read_text(encoding='utf-8-sig'), 'EPSG:4326', always_xy=True)
        except Exception as error:
            raise ValueError(f'Cannot read the coordinate system from {files[".prj"]}. Restore the original .prj from the EPA archive; no CRS was guessed.') from error
        def transform(coords):
            if isinstance(coords[0], (int, float)):
                return list(transformer.transform(*coords[:2]))
            return [transform(c) for c in coords]
        features = []
        with shapefile.Reader(shp=str(files['.shp']), shx=str(files['.shx']), dbf=str(files['.dbf'])) as reader:
            for item in reader.iterShapeRecords():
                geometry = item.shape.__geo_interface__
                geometry['coordinates'] = transform(geometry['coordinates'])
                features.append(dict(type='Feature', properties=item.record.as_dict(), geometry=geometry))
        collection = dict(type='FeatureCollection', features=features)
        # Validate before replacing an existing converted file or its provenance.
        with TemporaryDirectory(prefix='edufleur-area-validation-') as directory:
            candidate = Path(directory) / 'areas.json'
            atomic_json(candidate, collection)
            load_areas(candidate)
            record = source_record(candidate, args.url, args.license)
        record['transformation'] = 'pyshp + pyproj: source CRS to EPSG:4326'
        record['upstreamChecksums'] = {suffix:digest(path.read_bytes()) for suffix,path in files.items()}
        atomic_json(args.out, collection)
        atomic_json(str(args.out) + '.source.json', record)
        print(f'Area conversion complete: wrote {args.out} and {args.out}.source.json. Continue with the USDA checklist; no second EPA conversion is needed.')


def make_manifest(args):
    target=Path(args.out).resolve()
    target.parent.mkdir(parents=True,exist_ok=True)
    result={'scope':'lower48'}
    for key in ('areas','usda','observations'):
        path=Path(getattr(args,key)).resolve()
        metadata=Path(str(path)+'.source.json')
        if key=='observations':
            metadata=path.parent/'observations.source.json'
        record=json.loads(metadata.read_text())
        if digest(path.read_bytes())!=record['checksum']:
            raise ValueError(f'Checksum mismatch for {path}')
        import os
        result[key]=dict(record,path=os.path.relpath(path,target.parent))
    atomic_json(target,result)


def main():
    parser=argparse.ArgumentParser(description='Offline Edufleur catalog preparation; run only one importer per cache directory.')
    commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('snapshot',help='Download a source or register an operator-supplied export with provenance')
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--file')
    group.add_argument('--url')
    p.add_argument('--source-url',help='Provenance URL for a local export')
    p.add_argument('--license',required=True)
    p.add_argument('--out',required=True)
    p.add_argument('--cache',default='data/cache')
    p.add_argument('--max-bytes',type=int,default=60_000_000)
    p=commands.add_parser('usda-dwca',help='Convert the uploaded EOL/Zenodo USDA archive to the catalog checklist')
    p.add_argument('--archive',default='data/inputs/usda_plant_traits.tar.gz')
    p.add_argument('--out',default='data/inputs/usda.csv')
    p.add_argument('--source-url',help='Pinned Zenodo record; automatically identified for known v4/v7 archives')
    p=commands.add_parser('areas',help='Download or convert a complete EPA shapefile to WGS84 GeoJSON')
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--shapefile',help='A .shp file, complete ZIP archive, or extracted directory')
    group.add_argument('--download-epa',action='store_true',help='Download the official state-clipped Level III archive and record its source terms')
    p.add_argument('--cache',default='data/cache')
    p.add_argument('--url',help='Actual source URL; required for a local input')
    p.add_argument('--license',help='Source metadata terms; required for a local input')
    p.add_argument('--out',required=True)
    p=commands.add_parser('collect',help='Resumable lower-48 iNaturalist observation snapshot (can take multiple days)')
    p.add_argument('--areas',required=True)
    p.add_argument('--cache',default='data/cache')
    p.add_argument('--out',required=True)
    p.add_argument('--user-agent',default='EdufleurCatalog/0.1')
    p.add_argument('--offline',action='store_true')
    p=commands.add_parser('manifest')
    for key in ('areas','usda','observations','out'):
        p.add_argument('--'+key,required=True)
    p=commands.add_parser('build')
    p.add_argument('--manifest',required=True)
    p.add_argument('--out',required=True)
    p.add_argument('--cache',default='data/cache')
    p.add_argument('--exclusions')
    p.add_argument('--offline',action='store_true')
    p=commands.add_parser('validate')
    p.add_argument('--bundle',required=True)
    p=commands.add_parser('publish',help='Validate then switch the bundle. Stop the application first.')
    p.add_argument('--bundle',required=True)
    p.add_argument('--root',default='data/catalog')
    p=commands.add_parser('reports')
    p.add_argument('--db',default='edufleur.db')
    p=commands.add_parser('exclude',help='Exclude an image hash or provider photo ID on the next rebuild')
    p.add_argument('--image',required=True)
    p.add_argument('--file',default='data/exclusions.json')
    args=parser.parse_args()
    if args.command=='snapshot':
        out=Path(args.out)
        out.parent.mkdir(parents=True,exist_ok=True)
        if args.file:
            if not args.source_url:
                parser.error('--file requires --source-url')
            shutil.copyfile(args.file,out)
            record=source_record(out,args.source_url,args.license)
        else:
            path,record=Cache(args.cache).fetch(args.url,args.license,args.max_bytes)
            shutil.copyfile(path,out)
        atomic_json(str(out)+'.source.json',record)
    elif args.command=='usda-dwca':
        from .usda_dwca import convert_usda_dwca
        report=convert_usda_dwca(args.archive,args.out,args.source_url)
        print(f'USDA archive conversion complete: {report["counts"]["includedSpecies"]} species from {report["source"]} ({report["version"]}).')
        print(f'Wrote {args.out}, {args.out}.source.json and {args.out}.import.json. Continue with observation collection.')
    elif args.command=='areas':
        convert_areas(args)
    elif args.command=='collect':
        collect(args)
    elif args.command=='manifest':
        make_manifest(args)
    elif args.command=='build':
        report=prepare(args.manifest,args.out,Cache(args.cache,offline=args.offline),args.exclusions)
        print(canonical(report))
    elif args.command=='validate':
        print(canonical(validate_bundle(args.bundle)))
    elif args.command=='publish':
        print(publish(args.bundle,args.root))
    elif args.command=='reports':
        with sqlite3.connect(f'file:{Path(args.db).resolve()}?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            print(json.dumps([dict(row) for row in db.execute('SELECT id,image,reason,details,created_at FROM image_reports ORDER BY id DESC LIMIT 500')],indent=2))
    elif args.command=='exclude':
        if not args.image.isdigit() and not (len(args.image)==64 and all(c in '0123456789abcdef' for c in args.image)):
            parser.error('Image must be a SHA-256 image ID or numeric iNaturalist photo ID')
        path=Path(args.file)
        items=set(json.loads(path.read_text())) if path.exists() else set()
        items.add(args.image)
        atomic_json(path,sorted(items))

if __name__=='__main__':
    try:
        main()
    except Exception as error:
        print(f'Catalog preparation failed: {error}',file=sys.stderr)
        sys.exit(1)

"""Convert an archived EOL/USDA DwCA taxon table to the catalog's CSV contract.

Only factual taxonomic fields are copied. Trait text and auxiliary media have
separate provenance/rights and are intentionally retained in the original archive.
"""
from collections import Counter
import csv
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET

from .build import binomial, load_usda
from .common import atomic_json, digest, source_record

NS = {'d': 'http://rs.tdwg.org/dwc/text/'}
DWC = 'http://rs.tdwg.org/dwc/terms/'
KNOWN_ARCHIVES = {
    '2659c7f36003e83fe6b3bebe2ed7eed4': ('15634104', 'v4'),
    'b4d01bb0a4b17c3df57c80b53f20d8df': ('17903503', 'v7'),
}
USDA_TERMS = 'https://plants.sc.egov.usda.gov/DocumentLibrary/Pdf/PLANTS_Help_Document.pdf'
TAXON_LICENSE = ('USDA PLANTS factual taxonomic data: not copyrighted; free for any use; '
                 'cite USDA NRCS PLANTS Database and the EOL/Zenodo archive. '
                 'The Zenodo archive license is unspecified; media and trait text are not imported.')
MAX_UNCOMPRESSED = 512 * 1024 * 1024


def archive_members(archive):
    members = {}
    total = 0
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or '\\' in member.name:
            raise ValueError('DwCA contains an unsafe archive path')
        if member.isdir():
            continue
        if not member.isfile():
            raise ValueError('DwCA links and special files are not supported')
        name = str(path)
        if name in members:
            raise ValueError(f'Duplicate archive member: {name}')
        total += member.size
        if member.size < 0 or total > MAX_UNCOMPRESSED:
            raise ValueError('DwCA exceeds the 512 MiB expanded-size limit')
        members[name] = member
    return members


def taxon_table(archive, members):
    matches = [name for name in members if PurePosixPath(name).name == 'meta.xml']
    if len(matches) != 1:
        raise ValueError('DwCA must contain exactly one meta.xml descriptor')
    meta_name = matches[0]
    member = members[meta_name]
    if member.size > 1_000_000:
        raise ValueError('DwCA XML descriptor exceeds the size limit')
    with archive.extractfile(member) as stream:
        data = stream.read()
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('DwCA descriptors with XML entities or DTDs are not supported')
    root = ET.fromstring(data)
    tables = [t for t in root if t.get('rowType') == DWC + 'Taxon']
    if len(tables) != 1:
        raise ValueError('DwCA must describe exactly one Taxon table')
    table = tables[0]
    locations = table.findall('d:files/d:location', NS)
    if len(locations) != 1 or not locations[0].text:
        raise ValueError('Expected one taxon table file in meta.xml')
    location = PurePosixPath(locations[0].text.strip())
    if location.is_absolute() or '..' in location.parts:
        raise ValueError('Unsafe taxon table location in meta.xml')
    name = str(PurePosixPath(meta_name).parent / location)
    if name not in members or members[name].size > 64 * 1024 * 1024:
        raise ValueError('Taxon table is missing or exceeds the 64 MiB limit')
    if table.get('fieldsTerminatedBy', '\\t') not in ('\\t', '\t'):
        raise ValueError('This EOL/USDA adapter requires a tab-delimited Taxon table')
    if table.get('encoding', 'UTF-8').replace('-', '').lower() != 'utf8':
        raise ValueError('This EOL/USDA adapter requires a UTF-8 Taxon table')
    fields = {}
    for field in table.findall('d:field', NS):
        term = field.get('term', '')
        key = term.rsplit('/', 1)[-1]
        if key in fields:
            raise ValueError(f'Duplicate field in DwCA descriptor: {key}')
        index = int(field.get('index', '-1'))
        if index < 0 or index > 100:
            raise ValueError('Invalid DwCA field index')
        fields[key] = index
    for required in ('taxonID', 'source', 'scientificName', 'taxonRank', 'taxonomicStatus'):
        if required not in fields:
            raise ValueError(f'DwCA Taxon table is missing {required}')
    headers = int(table.get('ignoreHeaderLines', '0'))
    if not 0 <= headers <= 10:
        raise ValueError('Unsupported DwCA header count')
    return members[name], fields, headers


def convert_usda_dwca(archive_path, output_path, source_url=None):
    archive_path, output_path = Path(archive_path), Path(output_path)
    if not archive_path.is_file():
        raise ValueError(f'Archive does not exist: {archive_path}. Save the downloaded Zenodo tar.gz at this path first.')
    if output_path.resolve() == archive_path.resolve():
        raise ValueError('The normalized CSV must not overwrite the source archive')
    with archive_path.open('rb') as stream:
        md5 = hashlib.file_digest(stream, 'md5').hexdigest()
    with archive_path.open('rb') as stream:
        archive_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    known = KNOWN_ARCHIVES.get(md5)
    if known:
        record_id, version = known
        expected = f'https://zenodo.org/records/{record_id}'
        if source_url and source_url.rstrip('/') != expected:
            raise ValueError(f'Archive checksum identifies Zenodo record {record_id} ({version}), but --source-url names a different record')
        source_url = expected
    else:
        if not source_url or not re.fullmatch(r'https://zenodo\.org/records/\d+', source_url.rstrip('/')):
            raise ValueError('Unrecognized archive checksum. Supply its pinned Zenodo record using --source-url https://zenodo.org/records/RECORD_NUMBER')
        source_url = source_url.rstrip('/')
        record_id, version = source_url.rsplit('/', 1)[-1], 'operator-specified record'
    counters, by_symbol, name_symbols = Counter(), {}, {}
    conflicts, issues = set(), []
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = archive_members(archive)
        taxon_member, fields, headers = taxon_table(archive, members)
        with io.TextIOWrapper(archive.extractfile(taxon_member), encoding='utf-8-sig', newline='') as stream:
            reader = csv.reader(stream, delimiter='\t')
            for _ in range(headers):
                next(reader, None)
            for row in reader:
                if not row:
                    continue
                counters['inputTaxa'] += 1
                if counters['inputTaxa'] > 1_000_000:
                    raise ValueError('Taxon row limit exceeded')
                if len(row) <= max(fields.values()):
                    raise ValueError(f'Malformed taxon row {reader.line_num}: too few fields')
                def field(key):
                    return row[fields[key]].strip() if key in fields else ''
                if field('taxonRank').lower() != 'species':
                    counters['nonSpecies'] += 1
                    continue
                if field('taxonomicStatus').lower() not in ('valid', 'accepted'):
                    counters['unacceptedStatus'] += 1
                    continue
                scientific = field('scientificName')
                name = binomial(scientific)
                symbol = field('taxonID')
                profile = urlparse(field('source'))
                profile_symbol = parse_qs(profile.query).get('symbol', [])
                if not re.fullmatch('[A-Z][A-Z0-9]{1,19}', symbol) or profile.scheme not in ('http', 'https') or profile.hostname not in ('plants.usda.gov', 'www.plants.usda.gov', 'plants.sc.egov.usda.gov') or profile_symbol != [symbol]:
                    counters['unverifiedUsdaIdentifier'] += 1
                    issues.append(dict(symbol=symbol, reason='USDA identifier/profile mismatch'))
                    continue
                if not name:
                    counters['unsupportedScientificName'] += 1
                    continue
                record = dict(symbol=symbol, accepted_symbol='', scientific_name=scientific, common_name='', family=field('family'))
                if symbol in by_symbol and by_symbol[symbol] != record:
                    conflicts.add(symbol)
                    issues.append(dict(symbol=symbol, reason='Conflicting records for USDA symbol'))
                elif symbol in by_symbol:
                    counters['duplicateRows'] += 1
                else:
                    by_symbol[symbol] = record
                name_symbols.setdefault(name, set()).add(symbol)
        for name, symbols in name_symbols.items():
            if len(symbols) > 1:
                conflicts.update(symbols)
                issues.append(dict(scientificName=name, reason='Scientific name maps to multiple USDA symbols', symbols=sorted(symbols)))
        for symbol in conflicts:
            by_symbol.pop(symbol, None)
        counters['quarantinedSymbols'] = len(conflicts)
        counters['includedSpecies'] = len(by_symbol)
        table_sizes = {name:member.size for name, member in members.items()}
    if not by_symbol:
        raise ValueError('No unambiguous USDA species could be imported; no output was written')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.usda-import-', dir=output_path.parent) as directory:
        candidate = Path(directory) / 'usda.csv'
        with candidate.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['symbol', 'accepted_symbol', 'scientific_name', 'common_name', 'family'])
            writer.writeheader()
            writer.writerows(by_symbol[key] for key in sorted(by_symbol))
        taxa, _ = load_usda(candidate)
        if len(taxa) != len(by_symbol):
            raise ValueError('Normalized CSV failed the catalog taxonomy validation')
        record = source_record(candidate, source_url, TAXON_LICENSE)
        record.update(archiveSha256=archive_sha, archiveMd5=md5, archiveVersion=version,
                      archiveDownloadUrl=f'https://zenodo.org/records/{record_id}/files/usda_plant_traits.tar.gz?download=1',
                      archiveLicense='Not specified in Zenodo record',
                      checksumMatchedPublishedVersion=bool(known),
                      transformation='EOL/USDA DwCA accepted species taxonomy to normalized CSV v1',
                      importedFields=['taxonID', 'scientificName', 'family', 'taxonRank', 'taxonomicStatus', 'source'],
                      excludedTables=['media_resource.tab', 'reference.tab', 'agent.tab', 'occurrence_specific.tab', 'measurement_or_fact_specific.tab'],
                      originalPlantDataTerms=USDA_TERMS)
        report = dict(source=source_url, version=version, archiveSha256=archive_sha,
                      checksumMatchedPublishedVersion=bool(known), counts=dict(counters),
                      archiveTableBytes=table_sizes, quarantine=issues,
                      limitations=[
                          'Archived subset; not the complete current USDA checklist or a synonym crosswalk.',
                          'Common names are absent; course generation can use iNaturalist preferred common names.',
                          'Broad native-range facts do not establish state nativity and are not imported.',
                          'Auxiliary template media and trait text are not imported; original archive retained.',
                      ])
        atomic_json(str(candidate) + '.source.json', record)
        atomic_json(str(candidate) + '.import.json', report)
        # Validation completes before touching previous successful outputs.
        os.replace(candidate, output_path)
        os.replace(str(candidate) + '.source.json', str(output_path) + '.source.json')
        os.replace(str(candidate) + '.import.json', str(output_path) + '.import.json')
    return report

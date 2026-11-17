import csv
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools.catalog.build import binomial, load_usda, prepare
from tools.catalog.common import atomic_json, digest
from tools.catalog.usda_dwca import convert_usda_dwca
from fixtures import fixture, modify_observations

SOURCE = 'https://zenodo.org/records/12345'
FIELDS = ['taxonID', 'source', 'scientificName', 'family', 'taxonRank', 'taxonomicStatus']


def make_archive(path, rows, extra=None):
    fields = ''.join(f'<field index="{i}" term="http://rs.tdwg.org/dwc/terms/{name}"/>' for i,name in enumerate(FIELDS))
    # The source term is Dublin Core in the real uploaded archive.
    fields = fields.replace('http://rs.tdwg.org/dwc/terms/source','http://purl.org/dc/terms/source')
    meta = f'<archive xmlns="http://rs.tdwg.org/dwc/text/"><table encoding="UTF-8" fieldsTerminatedBy="\\t" ignoreHeaderLines="1" rowType="http://rs.tdwg.org/dwc/terms/Taxon"><files><location>taxon.tab</location></files>{fields}</table></archive>'
    contents = io.StringIO(newline='')
    writer = csv.writer(contents, delimiter='\t', lineterminator='\n')
    writer.writerow(FIELDS)
    writer.writerows(rows)
    with tarfile.open(path,'w:gz') as archive:
        for name, data in {'./meta.xml':meta,'./taxon.tab':contents.getvalue(), './media_resource.tab':'taxonID\tdescription\nUrsus_maritimus\tTemplate polar bear\n', **(extra or {})}.items():
            payload = data.encode()
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))


def row(symbol='AB', name='Testus alpha L.', rank='species', status='valid'):
    return [symbol, 'https://plants.usda.gov/home/plantProfile?symbol='+symbol,name,'Testaceae',rank,status]


class DwcaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = self.root/'input.tar.gz'
        self.output = self.root/'usda.csv'

    def tearDown(self):
        self.temp.cleanup()

    def convert(self):
        return convert_usda_dwca(self.archive,self.output,SOURCE)

    def test_author_filiation_is_not_an_infraspecific_form(self):
        self.assertEqual(binomial('Acer nigrum Michx. f.'), 'Acer nigrum')
        self.assertEqual(binomial('Amelanchier arborea (Michx. f.) Fernald'), 'Amelanchier arborea')
        self.assertIsNone(binomial('Testus alpha f. minor L.'))
        self.assertIsNone(binomial('Testus alpha var. minor L.'))
        self.assertIsNone(binomial('Testus ×alpha L.'))

    def test_species_conversion_provenance_and_ignored_template_media(self):
        make_archive(self.archive,[row(),row('CD','Testus beta L.','variety'),row('EF','Testus gamma L.',status='synonym')])
        report = self.convert()
        self.assertEqual(report['counts']['includedSpecies'],1)
        taxa,names = load_usda(self.output)
        self.assertEqual(taxa['AB']['scientificName'],'Testus alpha')
        self.assertEqual(taxa['AB']['commonName'],'')
        self.assertEqual(names['Testus alpha'],{'AB'})
        self.assertNotIn('Ursus',self.output.read_text())
        meta = json.loads(Path(str(self.output)+'.source.json').read_text())
        self.assertEqual(meta['checksum'],digest(self.output.read_bytes()))
        self.assertEqual(meta['archiveSha256'],digest(self.archive.read_bytes()))
        self.assertEqual(meta['archiveLicense'],'Not specified in Zenodo record')
        self.assertIn('media_resource.tab',meta['excludedTables'])
        self.assertFalse((self.root/'media_resource.tab').exists())

    def test_known_checksum_selects_pinned_record_and_rejects_wrong_record(self):
        make_archive(self.archive,[row()])
        checksum = hashlib.md5(self.archive.read_bytes()).hexdigest()
        with patch('tools.catalog.usda_dwca.KNOWN_ARCHIVES',{checksum:('12345','test-version')}):
            report = convert_usda_dwca(self.archive,self.output)
            self.assertTrue(report['checksumMatchedPublishedVersion'])
            self.assertEqual(report['version'],'test-version')
            with self.assertRaisesRegex(ValueError,'different record'):
                convert_usda_dwca(self.archive,self.output,'https://zenodo.org/records/98765')

    def test_unknown_archive_needs_explicit_provenance(self):
        make_archive(self.archive,[row()])
        with self.assertRaisesRegex(ValueError,'Unrecognized archive checksum'):
            convert_usda_dwca(self.archive,self.output)
        self.assertFalse(self.output.exists())

    def test_conflicting_identifiers_and_names_are_quarantined(self):
        make_archive(self.archive,[row(),row('AB','Testus beta L.'),row('CD','Testus alpha L.'),row('EF','Testus gamma L.')])
        report = self.convert()
        taxa,_ = load_usda(self.output)
        self.assertEqual(set(taxa),{'EF'})
        self.assertEqual(report['counts']['quarantinedSymbols'],2)

    def test_profile_identifier_mismatch_is_not_invented(self):
        wrong = row('CD','Testus beta L.')
        wrong[1] = 'https://plants.usda.gov/home/plantProfile?symbol=OTHER'
        make_archive(self.archive,[row(),wrong])
        report = self.convert()
        self.assertEqual(report['counts']['unverifiedUsdaIdentifier'],1)
        self.assertEqual(report['counts']['includedSpecies'],1)

    def test_corrupt_unsafe_or_empty_input_preserves_existing_output(self):
        make_archive(self.archive,[row()])
        self.convert()
        before = self.output.read_bytes()
        for extra in ({'../escape.txt':'no'}, {'/absolute.txt':'no'}):
            make_archive(self.archive,[row()],extra)
            with self.assertRaisesRegex(ValueError,'unsafe'):
                self.convert()
            self.assertEqual(self.output.read_bytes(),before)
        make_archive(self.archive,[row(rank='genus')])
        with self.assertRaisesRegex(ValueError,'No unambiguous USDA species'):
            self.convert()
        self.assertEqual(self.output.read_bytes(),before)

    def test_archive_links_are_rejected_without_extraction(self):
        with tarfile.open(self.archive,'w:gz') as archive:
            link = tarfile.TarInfo('taxon.tab')
            link.type = tarfile.SYMTYPE
            link.linkname = '/etc/passwd'
            archive.addfile(link)
        with self.assertRaisesRegex(ValueError,'links and special files'):
            self.convert()
        self.assertFalse(self.output.exists())

    def test_normalized_archive_runs_through_catalog_builder_with_common_names(self):
        inputs = self.root/'inputs'
        manifest,cache = fixture(inputs)
        names = 'alpha beta gamma delta epsilon zeta eta theta iota kappa'.split()
        make_archive(self.archive,[row(f'TEST{i}',f'Testus {name} L.') for i,name in enumerate(names)])
        convert_usda_dwca(self.archive,inputs/'usda.csv',SOURCE)
        def preferred_names(data):
            for observation in data['observations']:
                observation['taxon']['preferred_common_name'] = 'Preferred plant ' + str(observation['taxon']['id'])
        modify_observations(manifest,preferred_names)
        data = json.loads(manifest.read_text())
        data['usda'] = dict(json.loads((inputs/'usda.csv.source.json').read_text()),path='usda.csv')
        atomic_json(manifest,data)
        report = prepare(manifest,self.root/'bundle',cache)
        self.assertTrue(any(c['available'] for c in report['courses']))
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.root/'bundle/catalog.db')) as db:
            course = json.loads(db.execute("SELECT payload FROM courses WHERE id='co-epa3-TEST'").fetchone()[0])
        self.assertTrue(course['units'][0]['plants'][0]['name'].startswith('Preferred plant '))
        self.assertEqual(course['total'],10)


if __name__ == '__main__':
    unittest.main()

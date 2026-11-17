import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fixtures import fixture, modify_observations
from tools.catalog.build import load_usda, prepare, publish, validate_bundle
from tools.catalog.geo import contains_uncertainty


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.manifest,self.cache=fixture(self.root/'inputs')
    def tearDown(self):
        self.temp.cleanup()
    def build(self,name='bundle'):
        report=prepare(self.manifest,self.root/name,self.cache)
        return report,self.root/name
    def test_deterministic_bundle_and_disjoint_teaching_images(self):
        a,path=self.build()
        b,other=self.build('second')
        self.assertEqual(a,b)
        self.assertEqual(validate_bundle(path)['files'],validate_bundle(other)['files'])
        self.assertEqual(len(a['states']),48)
        self.assertFalse(a['nationalGeographyComplete'])
        self.assertEqual(sum(c['available'] for c in a['courses']),1)
        with sqlite3.connect(path/'catalog.db') as db:
            data=json.loads(db.execute("SELECT payload FROM courses WHERE id='co-epa3-TEST'").fetchone()[0])
        self.assertEqual([len(u['questions']) for u in data['units']],[5,5])
        for unit in data['units']:
            for plant,question in zip(unit['plants'],unit['questions']):
                self.assertNotEqual(plant['image']['id'],question['image']['id'])
                self.assertEqual(len(set(question['options'])),4)
                self.assertEqual(plant['name'],question['options'][question['correct']])
    def test_broad_native_status_does_not_qualify(self):
        modify_observations(self.manifest,lambda d:d['places'][0].update(admin_level=0,name='United States'))
        report,_=self.build()
        self.assertFalse(any(c['available'] for c in report['courses']))
        self.assertEqual(report['exclusions']['insufficientOrConflictingNativeStatus'],30)
    def test_conflicting_native_status_excluded(self):
        modify_observations(self.manifest,lambda d:d['observations'][0]['taxon']['establishment_means'].update(establishment_means='introduced'))
        report,_=self.build()
        self.assertEqual(next(c for c in report['courses'] if c['state']=='CO')['qualifyingSpecies'],9)
    def test_duplicate_observations_and_observer_threshold(self):
        def transform(d):
            for o in d['observations'][:3]:o['user']['id']=1
            d['observations'].append(d['observations'][0])
        modify_observations(self.manifest,transform)
        report,_=self.build()
        self.assertEqual(report['exclusions']['duplicateObservations'],1)
        course=next(c for c in report['courses'] if c['state']=='CO')
        self.assertEqual(course['qualifyingSpecies'],9)
        self.assertEqual(course['exclusions']['insufficientObservers'],1)
    def test_photo_license_and_exclusion(self):
        modify_observations(self.manifest,lambda d:d['observations'][0]['photos'][0].update(license_code='cc-by-nc'))
        report,path=self.build()
        self.assertFalse(any(c['available'] for c in report['courses']))
        self.assertEqual(report['exclusions']['incompatibleOrMissingPhotoLicense'],1)
        hashes=validate_bundle(path)['files']
        image=next(p for p in hashes if p.startswith('images/'))
        (self.root/'excluded.json').write_text(json.dumps([Path(image).stem]))
        other=prepare(self.manifest,self.root/'excluded',self.cache,self.root/'excluded.json')
        self.assertNotEqual(other['version'],report['version'])
        with sqlite3.connect(self.root/'excluded/catalog.db') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM images WHERE id=?',(Path(image).stem,)).fetchone()[0],0)
    def test_failed_build_and_tampered_publish_preserve_current(self):
        _,path=self.build()
        target=publish(path,self.root/'published')
        self.assertEqual((self.root/'published/current').resolve(),target)
        (path/'coverage.json').write_text('{}')
        with self.assertRaises(ValueError):publish(path,self.root/'published')
        self.assertEqual((self.root/'published/current').resolve(),target)
        self.cache.files.clear()
        with self.assertRaises(KeyError):self.build('broken')
        self.assertFalse((self.root/'broken').exists())
        self.assertEqual((self.root/'published/current').resolve(),target)
    def test_usda_synonym_rows_resolve_to_accepted_symbol(self):
        path=self.root/'aliases.csv'
        path.write_text('Symbol,Synonym Symbol,Scientific Name with Author,National Common Name\nABC,,Testus alpha L.,Plant\nABC,DEF,Oldus alpha L.,\n')
        taxa,names=load_usda(path)
        self.assertEqual(taxa['ABC']['scientificName'],'Testus alpha')
        self.assertEqual(names['Oldus alpha'],{'ABC'})
    def test_geometry_holes_boundaries_and_uncertainty(self):
        geometry={'type':'Polygon','coordinates':[[[-106,39],[-104,39],[-104,41],[-106,41],[-106,39]], [[-105.2,39.8],[-104.8,39.8],[-104.8,40.2],[-105.2,40.2],[-105.2,39.8]]]}
        self.assertFalse(contains_uncertainty(geometry,-105,40,20))
        self.assertFalse(contains_uncertainty(geometry,-106,40,0))
        self.assertFalse(contains_uncertainty(geometry,-105.9999,40,20))
        self.assertTrue(contains_uncertainty(geometry,-105.9,40,20))
        self.assertFalse(contains_uncertainty(geometry,-105.9,40,float('nan')))


class SourceFailureTests(unittest.TestCase):
    def test_corrupt_manifest_fails_before_creating_staging(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            manifest,cache=fixture(root/'inputs')
            (root/'inputs/usda.csv').write_text('corrupted')
            with self.assertRaisesRegex(ValueError,'Unverified source'):
                prepare(manifest,root/'bundle',cache)
            self.assertFalse((root/'bundle').exists())
    def test_network_failure_never_publishes_a_partial_bundle(self):
        from urllib.error import URLError
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            manifest,cache=fixture(root/'inputs')
            def fail(*args,**kwargs):raise URLError('DNS unavailable')
            cache.fetch=fail
            with self.assertRaises(URLError):prepare(manifest,root/'bundle',cache)
            self.assertFalse((root/'bundle').exists())
    def test_cross_taxon_duplicate_images_are_excluded_for_both_taxa(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            manifest,cache=fixture(root/'inputs')
            modify_observations(manifest,lambda d:d['observations'][0]['photos'][0].update(url=d['observations'][3]['photos'][0]['url']))
            report=prepare(manifest,root/'bundle',cache)
            self.assertEqual(report['exclusions']['conflictingImageTaxonomy'],1)
            course=next(c for c in report['courses'] if c['state']=='CO')
            self.assertEqual(course['qualifyingSpecies'],8)
            self.assertFalse(course['available'])
    def test_offline_cache_rejects_missing_and_corrupt_snapshots(self):
        from tools.catalog.common import Cache, atomic_json, digest
        with tempfile.TemporaryDirectory() as name:
            cache=Cache(name,offline=True)
            url='https://api.inaturalist.org/v1/observations?per_page=1'
            with self.assertRaisesRegex(ValueError,'Snapshot missing'):cache.fetch(url,'test')
            key=digest(url.encode())
            path=Path(name)/key
            path.write_bytes(b'original')
            atomic_json(Path(name)/(key+'.json'),dict(url=url,license='test',checksum=digest(b'original'),retrievedAt='2026-01-01'))
            self.assertEqual(cache.fetch(url,'test')[0],path)
            path.write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError,'Corrupt snapshot'):cache.fetch(url,'test')

if __name__=='__main__':unittest.main()

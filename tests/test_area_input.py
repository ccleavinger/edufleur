import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from tools.catalog.area_input import COMPANIONS, shapefile_input
from tools.catalog.__main__ import convert_areas

GIS_AVAILABLE = all(importlib.util.find_spec(name) is not None for name in ('shapefile', 'pyproj'))


class AreaInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def complete_files(self, name='actual_name', folder=None):
        folder = folder or self.root
        folder.mkdir(parents=True, exist_ok=True)
        for suffix in COMPANIONS:
            (folder / (name + suffix.upper())).write_text('fixture')
        return folder / (name + '.SHP')

    def test_nonexistent_example_path_lists_actual_file(self):
        actual = self.complete_files()
        with self.assertRaisesRegex(ValueError, 'input does not exist') as failure:
            with shapefile_input(self.root / 'wrong_example_name.shp'):
                self.fail('Should not resolve an incorrect filename')
        self.assertIn(str(actual), str(failure.exception))
        self.assertIn('--download-epa', str(failure.exception))

    def test_missing_projection_is_actionable(self):
        actual = self.complete_files()
        actual.with_suffix('.PRJ').unlink()
        with self.assertRaisesRegex(ValueError, 'Incomplete shapefile') as failure:
            with shapefile_input(actual):
                self.fail('Should not guess a missing coordinate system')
        self.assertIn('actual_name.prj', str(failure.exception))
        self.assertIn('Extract the entire EPA ZIP', str(failure.exception))

    def test_directory_finds_nested_files_with_uppercase_extensions(self):
        actual = self.complete_files(folder=self.root / 'nested' / 'download')
        with shapefile_input(self.root) as files:
            self.assertEqual(files['.shp'], actual)
            self.assertEqual(files['.prj'], actual.with_suffix('.PRJ'))

    def test_multiple_shapefiles_require_explicit_selection(self):
        first = self.complete_files('first')
        self.complete_files('second')
        with self.assertRaisesRegex(ValueError, 'Multiple shapefiles'):
            with shapefile_input(self.root):
                self.fail('Must not guess between datasets')
        with shapefile_input(first) as files:
            self.assertEqual(files['.shp'], first)

    def test_zip_discovers_internal_name_and_cleans_temporary_files(self):
        archive = self.root / 'different_archive_name.zip'
        with zipfile.ZipFile(archive, 'w') as writer:
            for suffix in COMPANIONS:
                writer.writestr('nested/actual_name' + suffix.upper(), 'fixture')
            writer.writestr('metadata.txt', 'not a required shapefile component')
        with shapefile_input(archive) as files:
            projection = files['.prj']
            self.assertEqual(projection.read_text(), 'fixture')
        self.assertFalse(projection.exists())

    def test_partial_zip_and_unsafe_paths_are_rejected(self):
        archive = self.root / 'partial.zip'
        with zipfile.ZipFile(archive, 'w') as writer:
            writer.writestr('actual.shp', 'fixture')
        with self.assertRaisesRegex(ValueError, 'complete EPA shapefile ZIP'):
            with shapefile_input(archive):
                self.fail('An incomplete archive cannot be converted')
        with zipfile.ZipFile(archive, 'w') as writer:
            writer.writestr('../outside.shp', 'fixture')
        with self.assertRaisesRegex(ValueError, 'unsafe paths'):
            with shapefile_input(archive):
                self.fail('Archive paths must not escape temporary storage')
        self.assertFalse((self.root.parent / 'outside.shp').exists())

    def test_cli_missing_input_is_checked_before_projection_or_dependencies(self):
        result = subprocess.run([sys.executable, '-m', 'tools.catalog', 'areas', '--shapefile', str(self.root / 'missing.shp'), '--url', 'SOURCE_DOWNLOAD_URL', '--license', 'SOURCE_LICENSE_FROM_METADATA', '--out', str(self.root / 'areas.json')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('input does not exist', result.stderr)
        self.assertNotIn('No such file or directory', result.stderr)
        self.assertFalse((self.root / 'areas.json').exists())

    @unittest.skipUnless(GIS_AVAILABLE, 'Install catalog/requirements.txt for the conversion test')
    def test_real_shapefile_zip_converts_and_rejects_placeholders(self):
        import shapefile
        from pyproj import CRS
        stem = self.root / 'actual_internal_name'
        with shapefile.Writer(str(stem)) as writer:
            writer.field('STATE_NAME', 'C', size=30)
            writer.field('US_L3CODE', 'C', size=8)
            writer.field('US_L3NAME', 'C', size=40)
            writer.poly([[[-106, 39], [-106, 41], [-104, 41], [-104, 39], [-106, 39]]])
            writer.record('Colorado', 'TEST', 'Synthetic conversion test')
        stem.with_suffix('.prj').write_text(CRS.from_epsg(4326).to_wkt())
        archive = self.root / 'archive_with_a_different_name.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            for suffix in COMPANIONS:
                output.write(stem.with_suffix(suffix), 'deeply/nested/actual' + suffix)
        args = argparse.Namespace(shapefile=str(archive), download_epa=False, cache=str(self.root / 'cache'), url='https://example.invalid/test.zip', license='Synthetic test fixture', out=str(self.root / 'areas.json'))
        convert_areas(args)
        collection = json.loads(Path(args.out).read_text())
        self.assertEqual(collection['features'][0]['properties']['STATE_NAME'], 'Colorado')
        record = json.loads(Path(args.out + '.source.json').read_text())
        self.assertEqual(set(record['upstreamChecksums']), set(COMPANIONS))
        original = Path(args.out).read_bytes()
        args.url = 'SOURCE_DOWNLOAD_URL'
        with self.assertRaisesRegex(ValueError, 'actual source URL'):
            convert_areas(args)
        self.assertEqual(Path(args.out).read_bytes(), original)


if __name__ == '__main__':
    unittest.main()

"""Resolve complete shapefile inputs without assuming the archive's internal name."""
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import tempfile
import zipfile

EPA_AREAS_URL = 'https://dmap-prod-oms-edc.s3.us-east-1.amazonaws.com/ORD/Ecoregions/us/us_eco_l3_state_boundaries.zip'
EPA_AREAS_LICENSE = 'EPA metadata: no access or use constraints; EPA acknowledgement requested'
COMPANIONS = ('.shp', '.dbf', '.shx', '.prj')
MAX_EXPANDED_BYTES = 512 * 1024 * 1024


def choose_shapefile(candidates, location):
    candidates = sorted(candidates, key=str)
    if not candidates:
        raise ValueError(
            f'No .shp file found in {location}. Download the full EPA shapefile ZIP, '
            'then pass the ZIP directly with --shapefile, or use --download-epa.'
        )
    if len(candidates) != 1:
        choices = '\n  '.join(map(str, candidates))
        raise ValueError(
            f'Multiple shapefiles found in {location}:\n  {choices}\n'
            'Pass the intended .shp file explicitly; no file was selected.'
        )
    return candidates[0]


def companion_files(shp):
    siblings = list(shp.parent.iterdir())
    files, missing = {}, []
    for suffix in COMPANIONS:
        matches = [p for p in siblings if p.is_file() and p.stem.casefold() == shp.stem.casefold() and p.suffix.casefold() == suffix]
        if len(matches) > 1:
            raise ValueError(f'Ambiguous companion files for {shp.stem}{suffix} in {shp.parent}')
        if matches:
            files[suffix] = matches[0]
        else:
            missing.append(suffix)
    if missing:
        names = ', '.join(shp.stem + suffix for suffix in missing)
        raise ValueError(
            f'Incomplete shapefile: missing {names} beside {shp}. '
            'A shapefile needs matching .shp, .dbf, .shx and .prj files. '
            'Extract the entire EPA ZIP, or pass the ZIP directly. '
            'The coordinate system cannot be inferred safely without .prj.'
        )
    return files


@contextmanager
def shapefile_input(value):
    """Yield explicit companion paths, including mixed-case extensions.

    ZIP members are copied individually to a temporary directory; archive paths
    never become filesystem destinations. Unrelated archive files are ignored.
    """
    source = Path(value)
    if not source.exists():
        nearby = sorted(p for p in source.parent.rglob('*') if p.is_file() and p.suffix.casefold() in ('.shp', '.zip')) if source.parent.is_dir() else []
        hint = '\nAvailable inputs:\n  ' + '\n  '.join(str(p) for p in nearby[:20]) if nearby else ''
        raise ValueError(
            f'Shapefile input does not exist: {source.resolve()}. '
            'The example filename may differ from the files inside the EPA ZIP. '
            'Pass the downloaded ZIP or extracted directory instead, or use --download-epa.' + hint
        )
    if source.is_dir():
        candidates = [p for p in source.rglob('*') if p.is_file() and p.suffix.casefold() == '.shp' and '__MACOSX' not in p.parts and not p.name.startswith('._')]
        yield companion_files(choose_shapefile(candidates, source))
    elif source.suffix.casefold() == '.shp':
        yield companion_files(source)
    elif source.suffix.casefold() == '.zip':
        try:
            with zipfile.ZipFile(source) as archive:
                members = [item for item in archive.infolist() if not item.is_dir() and '__MACOSX' not in PurePosixPath(item.filename).parts and not PurePosixPath(item.filename).name.startswith('._')]
                if any(PurePosixPath(m.filename).is_absolute() or '..' in PurePosixPath(m.filename).parts for m in members):
                    raise ValueError('Archive contains unsafe paths; use the original EPA ZIP')
                names = [PurePosixPath(item.filename) for item in members]
                selected = choose_shapefile([p for p in names if p.suffix.casefold() == '.shp'], source)
                chosen = {}
                for suffix in COMPANIONS:
                    matches = [item for item in members if str(PurePosixPath(item.filename).with_suffix('')).casefold() == str(selected.with_suffix('')).casefold() and PurePosixPath(item.filename).suffix.casefold() == suffix]
                    if len(matches) != 1:
                        raise ValueError(f'Archive needs exactly one {selected.with_suffix(suffix)}. Download the complete EPA shapefile ZIP, including its .prj projection file.')
                    chosen[suffix] = matches[0]
                if sum(item.file_size for item in chosen.values()) > MAX_EXPANDED_BYTES:
                    raise ValueError('Expanded shapefile exceeds the 512 MiB size limit')
                with tempfile.TemporaryDirectory(prefix='edufleur-areas-') as directory:
                    files = {}
                    total = 0
                    for suffix, member in chosen.items():
                        target = Path(directory) / ('source' + suffix)
                        with archive.open(member) as raw, target.open('wb') as output:
                            while chunk := raw.read(1024 * 1024):
                                total += len(chunk)
                                if total > MAX_EXPANDED_BYTES:
                                    raise ValueError('Expanded shapefile exceeds the 512 MiB size limit')
                                output.write(chunk)
                        files[suffix] = target
                    yield files
        except zipfile.BadZipFile as error:
            raise ValueError(f'Not a valid shapefile ZIP: {source}') from error
    else:
        raise ValueError(f'Expected a .shp file, ZIP archive or extracted directory: {source}')

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATES = dict(item.split(':') for item in ('AL:Alabama|AZ:Arizona|AR:Arkansas|CA:California|CO:Colorado|CT:Connecticut|DE:Delaware|FL:Florida|GA:Georgia|ID:Idaho|IL:Illinois|IN:Indiana|IA:Iowa|KS:Kansas|KY:Kentucky|LA:Louisiana|ME:Maine|MD:Maryland|MA:Massachusetts|MI:Michigan|MN:Minnesota|MS:Mississippi|MO:Missouri|MT:Montana|NE:Nebraska|NV:Nevada|NH:New Hampshire|NJ:New Jersey|NM:New Mexico|NY:New York|NC:North Carolina|ND:North Dakota|OH:Ohio|OK:Oklahoma|OR:Oregon|PA:Pennsylvania|RI:Rhode Island|SC:South Carolina|SD:South Dakota|TN:Tennessee|TX:Texas|UT:Utah|VT:Vermont|VA:Virginia|WA:Washington|WV:West Virginia|WI:Wisconsin|WY:Wyoming').split('|'))
GENERATOR = 'ecoregion-photo-v1'
DEFAULTS = dict(minSpecies=10, minObservations=3, minObservers=2, minImages=3, unitSize=5)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(canonical(value) + '\n')
    os.replace(tmp, path)


def source_record(path, url, license_name, retrieved=None):
    return dict(url=url, license=license_name, checksum=digest(Path(path).read_bytes()),
                retrievedAt=retrieved or datetime.now(timezone.utc).isoformat())


class Cache:
    """URL-keyed verified snapshots; one request per second, bounded retries/budget."""
    def __init__(self, root, user_agent='EdufleurCatalog/0.1', limit=9000, offline=False):
        self.root, self.user_agent, self.limit, self.offline = Path(root), user_agent, limit, offline
        self.calls, self.last = 0, 0.0
        self.root.mkdir(parents=True, exist_ok=True)

    def fetch(self, url, license_name, max_bytes=20_000_000, hosts=None):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or (hosts and parsed.hostname not in hosts):
            raise ValueError('Only approved HTTPS source URLs are accepted')
        key = digest(url.encode())
        target, meta = self.root / key, self.root / (key + '.json')
        if target.exists() and meta.exists():
            record = json.loads(meta.read_text())
            if digest(target.read_bytes()) != record['checksum']:
                raise ValueError(f'Corrupt snapshot: {target}')
            if target.stat().st_size > max_bytes:
                raise ValueError('Cached source exceeds size limit')
            return target, record
        if self.offline:
            raise ValueError(f'Snapshot missing in offline mode: {url}')
        # Cache lives across runs: restarted commands also share the daily budget.
        day = datetime.now(timezone.utc).date().isoformat()
        budget_path = self.root / 'request-budget.json'
        budget = json.loads(budget_path.read_text()) if budget_path.exists() else {}
        for attempt in range(4):
            count = budget.get(day, 0)
            if count >= self.limit:
                raise ValueError('Daily request budget exhausted; resume tomorrow')
            time.sleep(max(0, 1.05 - (time.monotonic() - self.last)))
            budget[day] = count + 1
            atomic_json(budget_path, budget)
            self.last = time.monotonic()
            try:
                class Redirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                        new = urllib.parse.urlparse(newurl)
                        if new.scheme != 'https' or new.hostname != parsed.hostname:
                            raise ValueError('Cross-host redirects require a separately approved source URL')
                        return super().redirect_request(req, fp, code, msg, hdrs, newurl)
                req = urllib.request.Request(url, headers={'User-Agent': self.user_agent})
                with urllib.request.build_opener(Redirect()).open(req, timeout=30) as response:
                    content = response.read(max_bytes + 1)
                if len(content) > max_bytes:
                    raise ValueError(f'Source exceeds {max_bytes} bytes: {url}')
                temporary = target.with_suffix('.tmp')
                temporary.write_bytes(content)
                os.replace(temporary, target)
                record = source_record(target, url, license_name)
                atomic_json(meta, record)
                return target, record
            except urllib.error.HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise
                retry = error.headers.get('Retry-After', '')
                delay = int(retry) if retry.isdigit() else 2 ** (attempt + 1)
                if delay > 30:
                    raise ValueError('Provider requested a long pause; rerun later to resume') from error
                time.sleep(delay)
            except urllib.error.URLError:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError('Unreachable')

    def json(self, endpoint, params=None):
        url = 'https://api.inaturalist.org/v1/' + endpoint
        if params:
            url += '?' + urllib.parse.urlencode(sorted(params.items()))
        path, record = self.fetch(url, 'iNaturalist API metadata; individual record licenses retained')
        return json.loads(path.read_text()), record

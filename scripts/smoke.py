"""Read-only smoke check against a running server. Full grading tests: cargo test."""
import json
import os
import urllib.request

base=os.environ.get('EDUFLEUR_URL','http://127.0.0.1:3000')
def get(path):
    with urllib.request.urlopen(base+path,timeout=10) as response:
        return json.load(response)

catalog=get('/api/catalog')
courses=get('/api/courses')
assert isinstance(catalog['ready'],bool)
for course in courses:
    details=get('/api/courses/'+course['id'])
    assert details['catalogVersion']==catalog['version']
    for unit in details['units']:
        for question in unit['questions']:
            assert 'correct' not in question and 'taxonId' not in question
print(f"Catalog ready: {catalog['ready']}; available courses: {sum(c['available'] for c in courses)}")

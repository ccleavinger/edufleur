"""Synthetic evidence for tests only. Never included in a deployed catalog."""
import csv
import json
from pathlib import Path
from PIL import Image
from tools.catalog.common import atomic_json, digest


class FixtureCache:
    def __init__(self, root):
        self.root=Path(root)
        self.files={}
        for i in range(30):
            path=self.root/f'photo-{i}.jpg'
            Image.new('RGB',(650,650),(i*7%255,i*17%255,i*31%255)).save(path)
            url=f'https://static.inaturalist.org/photos/{i}/large.jpg'
            self.files[url]=path

    def fetch(self,url,license_name,**kwargs):
        path=self.files[url]
        return path,dict(url=url,license=license_name,checksum=digest(path.read_bytes()),retrievedAt='2026-01-01T00:00:00Z')


def fixture(root):
    root=Path(root)
    root.mkdir(parents=True,exist_ok=True)
    polygon={'type':'Polygon','coordinates':[[[-106,39],[-104,39],[-104,41],[-106,41],[-106,39]]]}
    areas={'type':'FeatureCollection','features':[{'type':'Feature','properties':{'state':'CO','ecoregionCode':'TEST','name':'Synthetic test area'},'geometry':polygon}, {'type':'Feature','properties':{'state':'AZ','ecoregionCode':'EMPTY','name':'Empty test area'},'geometry':{'type':'Polygon','coordinates':[[[-114,32],[-110,32],[-110,36],[-114,36],[-114,32]]]}}]}
    atomic_json(root/'areas.json',areas)
    epithets='alpha beta gamma delta epsilon zeta eta theta iota kappa'.split()
    with (root/'usda.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['Symbol','Synonym Symbol','Scientific Name with Author','National Common Name','Family'])
        writer.writeheader()
        for i,name in enumerate(epithets):
            writer.writerow({'Symbol':f'TEST{i}','Scientific Name with Author':f'Testus {name} L.','National Common Name':f'Test plant {i}','Family':'Testaceae'})
    observations=[]
    for i,name in enumerate(epithets):
        for j in range(3):
            oid=i*3+j+1
            observations.append(dict(id=oid,taxon=dict(id=100+i,name=f'Testus {name}',rank='species',is_active=True,establishment_means={'establishment_means':'native','place':{'id':34}}),quality_grade='research',captive=False,geojson={'type':'Point','coordinates':[-105,40]},positional_accuracy=20,user={'id':j+1},photos=[{'id':oid,'url':f'https://static.inaturalist.org/photos/{oid-1}/square.jpg','license_code':'cc-by','attribution':f'Test photographer {j}, CC BY'}]))
    snapshot={'observations':observations,'places':[{'id':34,'name':'Colorado','admin_level':10,'place_type':8,'ancestor_place_ids':[1]}]}
    atomic_json(root/'observations.json',snapshot)
    manifest={'scope':'SYNTHETIC TEST DATA — NOT A REAL COVERAGE RESULT'}
    for key,name in [('areas','areas.json'),('usda','usda.csv'),('observations','observations.json')]:
        manifest[key]=dict(path=name,url=f'https://example.invalid/{name}',license='Synthetic test fixture',checksum=digest((root/name).read_bytes()),retrievedAt='2026-01-01T00:00:00Z')
    atomic_json(root/'manifest.json',manifest)
    return root/'manifest.json',FixtureCache(root)


def modify_observations(manifest, transform):
    path=Path(manifest).parent/'observations.json'
    data=json.loads(path.read_text())
    transform(data)
    atomic_json(path,data)
    m=json.loads(Path(manifest).read_text())
    m['observations']['checksum']=digest(path.read_bytes())
    atomic_json(manifest,m)

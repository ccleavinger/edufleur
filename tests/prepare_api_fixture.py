"""Called by Rust's in-process HTTP tests; generates no production artifacts."""
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fixtures import fixture
from tools.catalog.build import prepare
from tools.catalog.common import atomic_json

root=Path(sys.argv[1])
manifest,cache=fixture(root/'inputs')
prepare(manifest,root/'bundle',cache)
m=json.loads(manifest.read_text())
m['usda']['retrievedAt']='2026-02-01T00:00:00Z'
atomic_json(manifest,m)
prepare(manifest,root/'next-bundle',cache)
with sqlite3.connect(root/'progress.db') as db:
    db.execute('CREATE TABLE mastery (learner TEXT, course TEXT, question TEXT)')
    db.execute("INSERT INTO mastery VALUES ('legacy','co-foothills','original-question')")

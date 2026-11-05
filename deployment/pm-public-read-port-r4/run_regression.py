"""Run only the changed composition regression, preserving the actual output."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

home=Path(__file__).resolve().parent
test=home/'test_normal_deployment.py'
receipt=home/'REGRESSION-RESULT.json'
assert not receipt.exists(), 'Do not overwrite completed controls'
records=[]
def run(root, expected):
    result=subprocess.run([sys.executable,'-B',str(test),str(root),expected],
                          capture_output=True,text=True,timeout=60)
    records.append({'case':expected,'exit_code':result.returncode,
                    'stdout':result.stdout,'stderr':result.stderr})
    return result.returncode==0

with tempfile.TemporaryDirectory(prefix='a1-composition-predecessor-') as temp:
    archive=home.parent/'public-service-package-r3/PUBLIC-READ-PORT-SERVICE-PACKAGE.zip'
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            if not info.filename.startswith('build-context/') or info.is_dir():continue
            rel=Path(info.filename[len('build-context/'):])
            assert not rel.is_absolute() and '..' not in rel.parts
            dest=Path(temp)/rel
            dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_bytes(z.read(info))
    baseline=run(Path(temp),'predecessor')
corrected=run(home/'candidate','corrected')
data={'recorded_utc':datetime.now(timezone.utc).isoformat(),
      'state':'PASS' if baseline and corrected else 'FAIL',
      'python':sys.version,'platform':sys.platform,
      'test_sha256':hashlib.sha256(test.read_bytes()).hexdigest(),
      'source_delta':json.loads((home/'SOURCE-DELTA.json').read_text()),
      'runs':records,
      'fixture_correction_before_recorded_run':'Initial invalid-port fixture used object(), refused by unchanged owner constructor before target boundary; corrected fixture supplies _transaction. No source fix was changed.',
      'ceiling':'Local real app assembly/registered reader dispatch with snapshot sentinel; not actual SQL, remote OAuth, Linux UID/GID, enabled public resolver, candidate or production proof.'}
receipt.write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
print(json.dumps(data,indent=2))
sys.exit(0 if data['state']=='PASS' else 1)

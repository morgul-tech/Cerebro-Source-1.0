"""Small targeted mutation check of the NEW logic (8 mutants, not the V0.1 51-mutant suite): each mutant is applied to a
throwaway copy of the candidate and the named test file must then FAIL. Needs the same prerequisites as run_pg_tests.py.
usage: TMPDIR=/tmp python3 tests/mutation_check_v02.py [M1 M2 ...]   (prints one line per mutant; exit 1 if any survives)"""
import os, shutil, subprocess, sys, tempfile
SRC=os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
MUT=[
 ("M1 empty-history NO_COMMIT without settlement","settlement.py",'    if not valid:\n        return "INDETERMINATE", "NO_COMMIT_REFUSED_NO_SETTLEMENT_EVIDENCE"\n    if not settlement.authoritative:\n        return "INDETERMINATE", "SETTLEMENT_NOT_AUTHORITATIVE"\n    if settlement.status == SETTLED_ABSENT:','    if True:\n        return "NO_COMMIT", reason\n    if settlement.status == SETTLED_ABSENT:',"test_pg_execution.py"),
 ("M2 CAS without FENCED check","store_pg.py",'            if prog.state != FENCED:\n                raise _Abort(None)','            if False:\n                raise _Abort(None)',"test_pg_execution.py"),
 ("M3 key replay ignores scope","store_pg.py",'        if row["scope_ref"] != self._scope:','        if False:',"test_pg_hardening.py"),
 ("M4 always append unknown->unknown","store_pg.py",'            if prog.state == UNKNOWN_EFFECT and state_after == UNKNOWN_EFFECT:','            if False:',"test_pg_hardening.py"),
 ("M5 progress_of ignores scope","store_pg.py",'f" WHERE p.admission_ref = %s AND p.scope_ref = %s", (admission_ref, self._scope))','f" WHERE p.admission_ref = %s AND (p.scope_ref = %s OR true)", (admission_ref, self._scope))',"test_pg_hardening.py"),
 ("M6 no NO_COMMIT settlement rule in trigger","schema.py","IS DISTINCT FROM 'SETTLED_ABSENT'\n     OR COALESCE(j->'detail'->>'settlement_digest', '') = ''","IS DISTINCT FROM 'SETTLED_ABSENT' AND false\n     OR false","test_pg_admission.py"),
 ("M7 trigger ignores canonical seq/state binding","schema.py","     OR j->>'state_after' IS DISTINCT FROM NEW.state_after\n","","test_pg_admission.py"),
 ("M8 recorded ack ignored by classification","executor_pg.py","ack_seen=prog.ack_recorded","ack_seen=False","test_pg_execution.py"),
]
only=sys.argv[1:]
res=[]
for name,f,old,new,pat in MUT:
    if only and name.split()[0] not in only: continue
    work=os.path.join(tempfile.gettempdir(), f"cee_v02_mut_{name.split()[0]}")
    shutil.rmtree(work,ignore_errors=True)
    shutil.copytree(SRC,work,ignore=shutil.ignore_patterns("__pycache__"))
    p=os.path.join(work,"src/controlled_effect_executor_durable",f)
    t=open(p).read()
    if t.count(old)!=1: res.append((name,"MUTANT-NOT-APPLIED",t.count(old))); continue
    open(p,"w").write(t.replace(old,new))
    env={**os.environ,"TMPDIR":"/tmp","PYTHONDONTWRITEBYTECODE":"1"}
    r=subprocess.run([sys.executable,os.path.join(work,"tests/run_pg_tests.py"),"--confirm-disposable-test-cluster","--pattern",pat],capture_output=True,text=True,env=env,timeout=800)
    tail=[l for l in r.stderr.splitlines() if l.startswith(("FAILED","OK"))]
    fails=[l.split()[1] for l in r.stderr.splitlines() if l.startswith(("FAIL:","ERROR:"))]
    res.append((name,tail[-1] if tail else "?",fails[:4]))
    shutil.rmtree(work,ignore_errors=True)
for r in res: print(r)
survivors=[r for r in res if not str(r[1]).startswith("FAILED")]
print("MUTANTS", len(res), "SURVIVORS", [r[0] for r in survivors])
sys.exit(1 if survivors else 0)

#!/usr/bin/env python3
from __future__ import annotations
import argparse, ast, hashlib, json, shutil, subprocess
from pathlib import Path
from typing import Iterable
import yaml

SCHEMA="cerebro-context-source-package-manifest/v1"
ROOT=Path(__file__).resolve().parents[2]
CONTRACT=ROOT/"standards/context-source-package.yaml"

class PackageError(RuntimeError): pass

def _sha(path: Path)->str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _git_head(root: Path)->str:
    return subprocess.check_output(["git","-C",str(root),"rev-parse","HEAD"],text=True).strip()

def _index(root: Path, allowed_roots: Iterable[str])->dict[str,Path]:
    found={}
    dup=set()
    for relroot in allowed_roots:
        base=root/relroot
        for p in base.rglob("*.py"):
            name=p.stem
            if name in found and found[name]!=p: dup.add(name)
            else: found[name]=p
    for name in dup: found.pop(name,None)
    return found

def _imports(path: Path)->set[str]:
    tree=ast.parse(path.read_text(encoding="utf-8"),filename=str(path))
    names=set()
    for node in ast.walk(tree):
        if isinstance(node,ast.Import):
            for alias in node.names: names.add(alias.name.split(".",1)[0])
        elif isinstance(node,ast.ImportFrom) and node.module:
            names.add(node.module.split(".",1)[0])
    return names

def resolve_closure(root: Path, entrypoints:list[str], allowed_roots:list[str])->list[str]:
    index=_index(root,allowed_roots)
    queue=[root/p for p in entrypoints]
    closure=set()
    while queue:
        path=queue.pop(0).resolve()
        try: rel=path.relative_to(root.resolve()).as_posix()
        except ValueError as exc: raise PackageError("dependency-outside-source-root") from exc
        if rel in closure: continue
        if not path.is_file(): raise PackageError("package-entry-missing:"+rel)
        closure.add(rel)
        if path.suffix!=".py": continue
        for mod in sorted(_imports(path)):
            dep=index.get(mod)
            if dep is not None:
                dep_rel=dep.relative_to(root).as_posix()
                if dep_rel not in closure: queue.append(dep)
    return sorted(closure)

def load_contract(root:Path=ROOT)->dict:
    raw=yaml.safe_load((root/"standards/context-source-package.yaml").read_text(encoding="utf-8"))
    value=raw["context_source_package"]
    if value.get("schema")!="cerebro-context-source-package/v1": raise PackageError("contract-schema")
    return value

def build(output:Path, *, root:Path=ROOT, expected_source_revision:str|None=None)->dict:
    c=load_contract(root)
    head=_git_head(root)
    if expected_source_revision and head!=expected_source_revision: raise PackageError("source-revision-mismatch")
    closure=resolve_closure(root,list(c["entrypoints"]),list(c["dependency_policy"]["allowed_source_roots"]))
    files=sorted(set(closure+list(c["explicit_files"])))
    forbidden=set(c.get("forbidden_package_paths") or [])
    hit=sorted(forbidden.intersection(files))
    if hit: raise PackageError("forbidden-package-path:"+",".join(hit))
    output.mkdir(parents=True,exist_ok=True)
    rows=[]
    for rel in files:
        src=root/rel
        if not src.is_file(): raise PackageError("package-file-missing:"+rel)
        dst=output/rel
        dst.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(src,dst)
        rows.append({"path":rel,"sha256":_sha(src),"size":src.stat().st_size})
    manifest={
        "schema":SCHEMA,
        "profile":c["package_profile"],
        "source_revision":head,
        "contract_path":"standards/context-source-package.yaml",
        "contract_sha256":_sha(root/"standards/context-source-package.yaml"),
        "entrypoints":list(c["entrypoints"]),
        "required_tool":c["required_tool"],
        "files":rows,
    }
    payload=json.dumps(manifest,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
    manifest["manifest_sha256"]=hashlib.sha256(payload).hexdigest()
    (output/"context-source-package-manifest.json").write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    verify(output,root=root)
    return manifest

def verify(output:Path, *, root:Path=ROOT)->dict:
    manifest=json.loads((output/"context-source-package-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema")!=SCHEMA: raise PackageError("manifest-schema")
    for row in manifest["files"]:
        packaged=output/row["path"]; source=root/row["path"]
        if not packaged.is_file(): raise PackageError("packaged-file-missing:"+row["path"])
        if _sha(packaged)!=row["sha256"] or _sha(source)!=row["sha256"]:
            raise PackageError("package-source-byte-mismatch:"+row["path"])
    return {"result":"PASS","files":len(manifest["files"]),"manifest_sha256":manifest["manifest_sha256"]}

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("output")
    ap.add_argument("--expected-source-revision")
    args=ap.parse_args()
    try:
        m=build(Path(args.output),expected_source_revision=args.expected_source_revision)
        print(json.dumps({"result":"PASS","manifest":m},indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"result":"BLOCK","error":str(exc)},indent=2))
        return 1
if __name__=="__main__": raise SystemExit(main())

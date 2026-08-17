#!/usr/bin/env python3
"""Prove that mandatory bootstrap dependencies live in this exact checkout."""
from __future__ import annotations
import argparse, json, re, subprocess, sys, tempfile
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA="org-bootstrap-closure/1.0.0"
ALL={"ORG_NATIVE_BOOTSTRAP","SHARED_PLATFORM_CONTRACT","CHILD_OR_PROVIDER_OPTIONAL","ASSEMBLED_WORKSPACE_ONLY"}
LOCAL={"ORG_NATIVE_BOOTSTRAP","SHARED_PLATFORM_CONTRACT"}
OPTIONAL=ALL-LOCAL
SEMVER=re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

class Blocked(RuntimeError): pass

def git(root:Path,*args:str)->str:
    p=subprocess.run(["git","-C",str(root),*args],text=True,capture_output=True)
    if p.returncode: raise Blocked(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip()

def repo_root(raw:str|None)->Path:
    root=(Path(raw).expanduser() if raw else Path(__file__).resolve().parents[1]).resolve()
    top=Path(git(root,"rev-parse","--show-toplevel")).resolve()
    if root!=top: raise Blocked(f"root must be Git top level: {root} != {top}")
    return root

def rel(raw:Any,label:str)->str:
    if not isinstance(raw,str) or not raw.strip(): raise Blocked(f"{label} must be non-empty")
    value=raw.strip().replace("\\","/")
    p=PurePosixPath(value)
    if p.is_absolute() or value.startswith("~") or ".." in p.parts or p.as_posix() in {"","."}:
        raise Blocked(f"{label} escapes checkout: {raw!r}")
    return p.as_posix()

def tracked(root:Path,raw:Any,label:str)->Path:
    r=rel(raw,label); p=root
    for part in PurePosixPath(r).parts:
        p/=part
        if p.is_symlink(): raise Blocked(f"{label} contains symlink: {r}")
    if not p.is_file(): raise Blocked(f"{label} missing/not regular: {r}")
    git(root,"ls-files","--error-unmatch","--",r)
    try: p.resolve().relative_to(root)
    except ValueError as e: raise Blocked(f"{label} resolves outside checkout: {r}") from e
    return p

def fm(path:Path)->dict[str,str]:
    lines=path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip()!="---": raise Blocked(f"missing skill frontmatter: {path}")
    out={}
    for line in lines[1:]:
        if line.strip()=="---": return out
        if ":" in line and not line.lstrip().startswith("#"):
            k,v=line.split(":",1); out[k.strip()]=v.strip().strip("\"'")
    raise Blocked(f"unterminated skill frontmatter: {path}")

def ver(raw:str,label:str)->tuple[int,int,int]:
    m=SEMVER.fullmatch(raw)
    if not m: raise Blocked(f"{label} must be stable SemVer")
    return tuple(map(int,m.groups()))

def walk(v:Any):
    if isinstance(v,dict):
        yield v
        for x in v.values(): yield from walk(x)
    elif isinstance(v,list):
        for x in v: yield from walk(x)

def validate(root_raw:str|None,manifest_raw:str|None)->dict[str,Any]:
    root=repo_root(root_raw); mr=rel(manifest_raw or "config/org-bootstrap-closure-v1.json","manifest")
    m=json.loads(tracked(root,mr,"manifest").read_text(encoding="utf-8"))
    if m.get("schema_version")!=SCHEMA: raise Blocked("unsupported manifest schema")
    repo=m.get("repository",{}); name=repo.get("full_name") if isinstance(repo,dict) else None
    if not isinstance(name,str) or "/" not in name: raise Blocked("repository.full_name must be owner/name")
    prefix=m.get("terminal_state_prefix")
    if not isinstance(prefix,str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*",prefix): raise Blocked("invalid terminal prefix")
    if not isinstance(m.get("dependency_classes"),list) or set(m["dependency_classes"])!=ALL:
        raise Blocked("dependency class taxonomy is incomplete")
    script=Path(__file__).resolve().relative_to(root).as_posix(); checked=set()
    for raw in [mr,script,*m.get("authority_files",[])]:
        r=rel(raw,"authority file"); tracked(root,r,"authority file"); checked.add(r)
    required=m.get("required_files")
    if not isinstance(required,list) or not required: raise Blocked("required_files must be non-empty")
    for i,item in enumerate(required):
        if not isinstance(item,dict) or item.get("class") not in LOCAL: raise Blocked(f"invalid required_files[{i}]")
        r=rel(item.get("path"),f"required_files[{i}].path"); tracked(root,r,"required file"); checked.add(r)
    skills=[]
    for i,item in enumerate(m.get("required_skills",[])):
        if not isinstance(item,dict) or item.get("class") not in LOCAL: raise Blocked(f"invalid required_skills[{i}]")
        sid=item.get("id"); r=rel(item.get("canonical_path"),f"required_skills[{i}].path")
        meta=fm(tracked(root,r,"required skill"))
        if meta.get("name")!=sid: raise Blocked(f"skill identity mismatch: {r}")
        minimum=item.get("minimum_version"); observed=meta.get("version")
        if minimum is not None:
            if not isinstance(minimum,str) or not isinstance(observed,str) or ver(observed,r)<ver(minimum,r):
                raise Blocked(f"skill version below minimum: {r}")
        source=(root/r).read_bytes()
        for raw in item.get("mirrors",[]):
            mirror=rel(raw,f"{sid}.mirror"); mp=tracked(root,mirror,"skill mirror")
            if mp.read_bytes()!=source: raise Blocked(f"skill mirror drift: {mirror}")
            checked.add(mirror)
        checked.add(r); skills.append({"id":sid,"path":r,"version":observed})
    counts={"examined":0,"external_optional":0,"local_runtime":0}
    for i,item in enumerate(m.get("binding_files",[])):
        if not isinstance(item,dict): raise Blocked(f"invalid binding_files[{i}]")
        r=rel(item.get("path"),f"binding_files[{i}].path")
        data=json.loads(tracked(root,r,"binding file").read_text(encoding="utf-8")); checked.add(r)
        for entry in walk(data):
            canonical=entry.get("canonical_path")
            if not isinstance(canonical,str) or not canonical.strip(): continue
            counts["examined"]+=1
            if entry.get("required_at_bootstrap") is True or entry.get("runtime_executable") is True:
                local=entry.get("local_path") or entry.get("vendored_path")
                if not isinstance(local,str) or not local.strip():
                    raise Blocked(f"external executable/bootstrap binding lacks local path: {canonical}")
                tracked(root,local,"binding local path"); counts["local_runtime"]+=1
            else: counts["external_optional"]+=1
    for i,item in enumerate(m.get("declared_optional_external_references",[])):
        if not isinstance(item,dict) or item.get("class") not in OPTIONAL or item.get("required_at_bootstrap") is not False:
            raise Blocked(f"invalid optional reference {i}")
    return {"repository":name,"head":git(root,"rev-parse","HEAD"),"manifest":mr,
            "checked_paths":sorted(checked),"required_skills":skills,"bindings":counts,
            "state":f"{prefix}_BOOTSTRAP_CLOSURE_READY"}

def self_test()->dict[str,Any]:
    with tempfile.TemporaryDirectory(prefix="bootstrap-closure-") as td:
        root=Path(td); subprocess.run(["git","init","-q",str(root)],check=True)
        git(root,"config","user.email","test@example.invalid"); git(root,"config","user.name","Test")
        (root/"config").mkdir(); (root/"scripts").mkdir(); (root/"skills/x").mkdir(parents=True)
        script=root/"scripts/validate-org-bootstrap-closure.py"
        script.write_text(Path(__file__).read_text(encoding="utf-8"),encoding="utf-8")
        (root/"AGENTS.md").write_text("# x\n",encoding="utf-8")
        (root/"skills/x/SKILL.md").write_text("---\nname: x\nversion: 1.0.0\n---\n",encoding="utf-8")
        manifest={"schema_version":SCHEMA,"repository":{"full_name":"x/y"},"terminal_state_prefix":"TEST",
          "dependency_classes":sorted(ALL),"authority_files":[],"required_files":[{"path":"AGENTS.md","class":"ORG_NATIVE_BOOTSTRAP"}],
          "required_skills":[{"id":"x","class":"ORG_NATIVE_BOOTSTRAP","canonical_path":"skills/x/SKILL.md","minimum_version":"1.0.0","mirrors":[]}],
          "binding_files":[],"declared_optional_external_references":[]}
        mp=root/"config/org-bootstrap-closure-v1.json"; mp.write_text(json.dumps(manifest),encoding="utf-8")
        subprocess.run(["git","-C",str(root),"add","-A"],check=True); subprocess.run(["git","-C",str(root),"commit","-qm","valid"],check=True)
        def run(): return subprocess.run([sys.executable,str(script),"--root",str(root),"--json"],capture_output=True,text=True)
        if run().returncode: raise Blocked("valid self-test fixture failed")
        (root/"AGENTS.md").unlink()
        if run().returncode==0: raise Blocked("missing-file self-test passed")
        subprocess.run(["git","-C",str(root),"checkout","--","AGENTS.md"],check=True)
        (root/"bindings.json").write_text(json.dumps({"bindings":[{"canonical_path":"../x","runtime_executable":True}]}),encoding="utf-8")
        manifest["binding_files"]=[{"path":"bindings.json"}]; mp.write_text(json.dumps(manifest),encoding="utf-8")
        subprocess.run(["git","-C",str(root),"add","-A"],check=True); subprocess.run(["git","-C",str(root),"commit","-qm","negative"],check=True)
        if run().returncode==0: raise Blocked("external-runtime self-test passed")
    return {"state":"ORG_BOOTSTRAP_CLOSURE_SELF_TEST_PASS","cases":["valid","missing_file","external_runtime"]}

def main()->int:
    p=argparse.ArgumentParser(); p.add_argument("--root"); p.add_argument("--manifest")
    p.add_argument("--json",action="store_true"); p.add_argument("--self-test",action="store_true"); a=p.parse_args()
    try: out=self_test() if a.self_test else validate(a.root,a.manifest)
    except (Blocked,OSError,ValueError,json.JSONDecodeError) as e:
        out={"state":"ORG_BOOTSTRAP_CLOSURE_BLOCKED","errors":[str(e)]}; print(json.dumps(out) if a.json else out["state"]); return 1
    print(json.dumps(out,sort_keys=True) if a.json else out["state"]); return 0
if __name__=="__main__": raise SystemExit(main())

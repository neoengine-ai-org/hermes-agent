#!/usr/bin/env python3
"""Minimal Hermes Stage-0 kernel: restore the full resolver and locked inputs."""
from __future__ import annotations
import argparse, hashlib, json, os
from pathlib import Path
import subprocess, sys, tempfile, contextlib

BLOCKED = "HERMES_SELF_HEALING_STAGE0_BLOCKED"

def digest(data: bytes) -> str: return hashlib.sha256(data).hexdigest()

def write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=".hermes-stage0-", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temp, mode); os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError): temp.unlink()

def tracked_mode(root: Path, relative: str) -> int:
    observed = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "HEAD", "--", relative],
        text=True,
        capture_output=True,
        check=True,
    )
    fields = observed.stdout.strip().split(None, 3)
    if len(fields) != 4:
        raise RuntimeError("RESOLVER_GIT_MODE_MISSING")
    git_mode = fields[0]
    if git_mode == "100644":
        return 0o644
    if git_mode == "100755":
        return 0o755
    raise RuntimeError(f"RESOLVER_GIT_MODE_UNSUPPORTED:{git_mode}")

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("ensure", "diagnose"))
    parser.add_argument("--policy", default="config/hermes-bootstrap-acquisition-v2.json")
    parser.add_argument("--pin", default="config/hermes-bootstrap-acquisition-v2.sha256")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--venv", default=".venv")
    parser.add_argument("--operation-id")
    args = parser.parse_args()
    try:
        top = subprocess.run(["git","rev-parse","--show-toplevel"], text=True, capture_output=True, check=True)
        root = Path(top.stdout.strip()).resolve()
        policy_path = root / args.policy
        raw = policy_path.read_bytes()
        if digest(raw) != (root / args.pin).read_text(encoding="utf-8").split()[0]:
            raise RuntimeError("POLICY_PIN_MISMATCH")
        policy = json.loads(raw)
        resolver = policy["stage1"]
        target = root / resolver["path"]
        observed = subprocess.run(["git","-C",str(root),"rev-parse",f"HEAD:{resolver['path']}"],
                                  text=True,capture_output=True,check=False)
        if observed.returncode or observed.stdout.strip() != resolver["blob_sha"]:
            raise RuntimeError("RESOLVER_GIT_IDENTITY_MISMATCH")
        if not target.is_file() or subprocess.run(["git","-C",str(root),"hash-object","--",resolver["path"]],
                                                  text=True,capture_output=True).stdout.strip() != resolver["blob_sha"]:
            if not args.repair: raise RuntimeError("RESOLVER_REPAIR_REQUIRED")
            shown = subprocess.run(["git","-C",str(root),"show",f"HEAD:{resolver['path']}"],
                                   capture_output=True,check=True)
            write(target, shown.stdout, tracked_mode(root, resolver["path"]))
        argv=[sys.executable,"-S",str(target),args.command,"--root",str(root),
              "--policy",args.policy,"--pin",args.pin,"--venv",args.venv]
        if args.repair: argv.append("--repair")
        if args.operation_id: argv += ["--operation-id",args.operation_id]
        return subprocess.run(argv,cwd=root,check=False).returncode
    except Exception as exc:
        print(json.dumps({"state":BLOCKED,"message":str(exc)},sort_keys=True),file=sys.stderr)
        return 2

if __name__ == "__main__": raise SystemExit(main())

from __future__ import annotations
import hashlib, json, os
from pathlib import Path
import shutil
import subprocess, sys

ROOT=Path(__file__).resolve().parents[2]
POLICY=ROOT/"config/hermes-bootstrap-acquisition-v2.json"
PIN=ROOT/"config/hermes-bootstrap-acquisition-v2.sha256"

def run(argv,cwd,env=None):
    return subprocess.run(argv,cwd=cwd,env=env,text=True,capture_output=True,check=False,timeout=300)

def test_policy_pin_and_shared_home_refusal():
    raw=POLICY.read_bytes()
    assert hashlib.sha256(raw).hexdigest()==PIN.read_text().split()[0]
    policy=json.loads(raw)
    assert policy["shared_home_authority_allowed"] is False
    assert policy["environment"]["allowed_venvs"]==[".venv",".bootstrap-proof-venv"]

def test_stage0_restores_deleted_resolver_and_rejects_wrong_pin(tmp_path):
    checkout=tmp_path/"hermes"
    assert run(["git","clone","--no-local","--quiet",str(ROOT),str(checkout)],tmp_path).returncode==0
    assert run(["git","remote","set-url","origin","https://github.com/neoengine-ai-org/hermes-agent.git"],checkout).returncode==0
    resolver=checkout/"scripts/bootstrap_resolver_v2.py"
    resolver.unlink()

    # The production resolver validates the repaired environment by importing
    # every mandatory Hermes entrypoint. A no-op fake `uv sync` can only prove
    # file restoration and produces a deliberately invalid environment. Use
    # the workflow-installed, lock-backed uv runtime so this hostile fixture
    # proves the complete recovery contract without weakening import smoke.
    assert shutil.which("uv") is not None, "uv is required by the admitted Hermes recovery policy"
    env={
        **os.environ,
        "PYTHONNOUSERSITE":"1",
        "UV_NO_PROGRESS":"1",
    }
    result=run([sys.executable,"-S","scripts/bootstrap_stage0_v2.py","ensure","--repair"],checkout,env)
    assert result.returncode==0,result.stderr
    assert resolver.is_file()
    pin=checkout/"config/hermes-bootstrap-acquisition-v2.sha256"
    pin.write_text("f"*64+"  config/hermes-bootstrap-acquisition-v2.json\n", encoding="utf-8")
    blocked=run([sys.executable,"-S","scripts/bootstrap_stage0_v2.py","diagnose"],checkout,env)
    assert blocked.returncode==2
    assert "POLICY_PIN_MISMATCH" in blocked.stderr

def test_public_runner_is_repair_first_and_v1_is_preserved():
    public=(ROOT/"scripts/run_tests.sh").read_text(encoding="utf-8")
    assert "bootstrap_stage0_v2.py" in public
    assert public.index("bootstrap_stage0_v2.py") < public.index("run_tests_v1.sh")
    assert (ROOT/"scripts/run_tests_v1.sh").is_file()

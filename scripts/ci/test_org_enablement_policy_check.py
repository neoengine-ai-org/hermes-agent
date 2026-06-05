import json, subprocess, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
SCRIPT=ROOT/'scripts/ci/org_enablement_policy_check.py'
def run(files, body=''):
    with tempfile.TemporaryDirectory() as d:
        f=Path(d)/'files.txt'; b=Path(d)/'body.md'; f.write_text('\n'.join(files)); b.write_text(body)
        p=subprocess.run([sys.executable,str(SCRIPT),'--changed-files',str(f),'--pr-body',str(b),'--repo','hermes-agent','--json'],cwd=ROOT,text=True,capture_output=True)
        return p.returncode,json.loads(p.stdout)
def test_gateway_seam_requires_invariant_test():
    _,d=run(['gateway/runtime/provider_error_sanitizer.py']); assert 'hermes.gateway-seam.requires-invariant-test' in d['matched_rules']; assert 'gateway-invariant' in d['required_ci_lanes']; assert d['requires_contract_tests']
def test_protected_claim_blocks():
    code,d=run(['docs/org-enablement-rails-2026-06-04.md'],'This claims launch readiness.'); assert code==1; assert any(b['rule_id']=='claim.launch-readiness.blocked' for b in d['merge_blockers'])
if __name__ == '__main__':
    test_gateway_seam_requires_invariant_test(); test_protected_claim_blocks(); print('hermes org-rails policy adapter tests: PASS')

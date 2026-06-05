#!/usr/bin/env python3
import argparse, hashlib, json, re, sys
from pathlib import Path
RISK_ORDER=['R0','R1','R2','R3','R4']; COMPLEXITY_ORDER=['C0','C1','C2','C3','C4']
def maxv(order,a,b): return b if order.index(b)>order.index(a) else a
def classify(rules, repo, changed_files, pr_body='', title='', runtime_surfaces=None, protected_surfaces=None):
    canonical={k:rules[k] for k in ['policy_version','path_rules','claim_rules']}
    h='sha256:'+hashlib.sha256(json.dumps(canonical,separators=(',',':')).encode()).hexdigest()
    # NeoEngine exporter hash is authoritative when vendored.
    h=rules.get('ruleset_hash',h)
    risk='R0'; comp='C0'; ci={'unit'}; reviews={'owner'}; nonclaims=set(); matched=[]; blockers=[]; warnings=[]
    flags=dict(requires_runtime_payload_contract=False,requires_contract_tests=False,requires_generated_artifact_check=False,requires_artifact_pointer_check=False,requires_repo_hygiene_check=False,requires_adversarial_review=False)
    files='\n'.join(changed_files)
    for rule in rules['path_rules']:
        if not re.search(rule['pattern'], files, re.I): continue
        matched.append(rule['rule_id']); risk=maxv(RISK_ORDER,risk,rule['risk_class']); comp=maxv(COMPLEXITY_ORDER,comp,rule['complexity_class'])
        ci.update(rule.get('required_ci_lanes',[])); reviews.update(rule.get('required_reviews',[])); nonclaims.update(rule.get('required_non_claims',[]))
        for key in flags:
            flags[key]=flags[key] or bool(rule.get(key))
        if rule.get('blocker_message'): blockers.append({'rule_id':rule['rule_id'],'message':rule['blocker_message'],'severity':'error'})
    text=f'{title}\n{pr_body}'
    for rule in rules['claim_rules']:
        if not re.search(rule['pattern'], text, re.I): continue
        matched.append(rule['rule_id']); nonclaims.add(rule['required_non_claim']); blockers.append({'rule_id':rule['rule_id'],'message':rule['message'],'severity':'error'}); risk=maxv(RISK_ORDER,risk,'R3'); comp=maxv(COMPLEXITY_ORDER,comp,'C1')
    for surface in runtime_surfaces or []:
        if surface.strip(): ci.add('runtime-contract')
    for surface in protected_surfaces or []:
        if surface.strip(): reviews.add('policy'); nonclaims.add(f'no protected approval for {surface}'); flags['requires_adversarial_review']=True; risk=maxv(RISK_ORDER,risk,'R3')
    if not matched: warnings.append({'rule_id':'policy.no-specialized-rule-matched','message':'No specialized org-rails rule matched; owner review and unit lane still required.'})
    return {'policy_version':rules['policy_version'],'ruleset_hash':h,'risk_class':risk,'complexity_class':comp,'required_ci_lanes':sorted(ci),'required_reviews':sorted(reviews),'required_non_claims':sorted(nonclaims),**flags,'merge_blockers':blockers,'warnings':warnings,'matched_rules':sorted(set(matched))}
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--changed-files',required=True); ap.add_argument('--pr-body'); ap.add_argument('--repo',default='ai-org'); ap.add_argument('--title',default=''); ap.add_argument('--rules',default='generated/org-rails-policy-rules.json'); ap.add_argument('--json',action='store_true'); ap.add_argument('--runtime-surfaces',default=''); ap.add_argument('--protected-surfaces',default='')
    ns=ap.parse_args(); rules=json.loads(Path(ns.rules).read_text(encoding="utf-8")); files=[x.strip() for x in Path(ns.changed_files).read_text(encoding="utf-8").splitlines() if x.strip()]; body=Path(ns.pr_body).read_text(encoding="utf-8") if ns.pr_body else ''
    d=classify(rules,ns.repo,files,body,ns.title,[x for x in ns.runtime_surfaces.split(',') if x],[x for x in ns.protected_surfaces.split(',') if x])
    print(json.dumps(d,indent=2) if ns.json else f"{d['policy_version']} {d['ruleset_hash']} {d['risk_class']}/{d['complexity_class']}")
    return 1 if d['merge_blockers'] else 0
if __name__=='__main__': sys.exit(main())

from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path
BAD=("node_modules/",".uv-cache/",".venv/","__pycache__/",".pytest_cache/","dist/","build/","neoengine_local/agent-state/")
tracked=subprocess.check_output(["git","ls-files"],text=True).splitlines()
baseline=set(json.loads(Path('.repo-hygiene-baseline.json').read_text()).get('allowlist',[])) if Path('.repo-hygiene-baseline.json').exists() else set()
errors=[]; warnings=[]
for f in tracked:
    if f in baseline: continue
    if any(part in f for part in BAD): errors.append(f"tracked cache/dependency/runtime-state path: {f}")
    try:
        if os.path.getsize(f)>1024*1024 and f.lower().endswith((".png",".jpg",".jpeg",".webp",".pdf",".zip",".mp4")):
            errors.append(f"tracked large binary outside reviewed artifact path: {f}")
    except OSError: pass
for d in ("node_modules",".uv-cache",".venv","neoengine_local/agent-state"):
    if os.path.exists(d): warnings.append(f"local ignored state present; review before cleanup: rm -rf {d}")
report={"ok":not errors,"errors":errors,"warnings":warnings}
print(json.dumps(report,indent=2) if "--json" in sys.argv else ("PASS" if not errors else "FAIL\n"+"\n".join(errors)))
sys.exit(1 if errors else 0)

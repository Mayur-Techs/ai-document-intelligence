import urllib.request
import json
import os
import sys

# Ensure stdout uses UTF-8 or ignore errors
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

url = "https://api.github.com/repos/Mayur-Techs/ai-document-intelligence/actions/runs"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
try:
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
    for r in data.get("workflow_runs", [])[:5]:
        msg = r.get("head_commit", {}).get("message", "").split("\n")[0]
        # Clean unicode characters for safety
        msg = msg.encode("ascii", "replace").decode("ascii")
        print(f"Run #{r.get('run_number')} - ID: {r.get('id')} - Status: {r.get('status')} - Conclusion: {r.get('conclusion')} - Commit: {msg}")
except Exception as e:
    print("Error:", e)

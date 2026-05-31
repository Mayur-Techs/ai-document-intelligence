import urllib.request
import json
import sys

# Ensure stdout uses UTF-8 or ignore errors
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

run_id = "26708228035"
url = f"https://api.github.com/repos/Mayur-Techs/ai-document-intelligence/actions/runs/{run_id}/jobs"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
try:
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
    for j in data.get("jobs", []):
        print(f"Job: {j.get('name')} - ID: {j.get('id')} - Status: {j.get('status')} - Conclusion: {j.get('conclusion')}")
except Exception as e:
    print("Error:", e)

import urllib.request
import json
import sys

check_run_id = "78713718877"
url = f"https://api.github.com/repos/Mayur-Techs/ai-document-intelligence/check-runs/{check_run_id}/annotations"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
try:
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
    print("Annotations count:", len(data))
    for idx, a in enumerate(data[:10]):
        print(f"[{idx}] {a.get('path')}:{a.get('start_line')} - Message: {a.get('message')}")
except Exception as e:
    print("Error:", e)

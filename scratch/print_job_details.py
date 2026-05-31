import urllib.request
import json
import sys

url = "https://api.github.com/repos/Mayur-Techs/ai-document-intelligence/actions/runs/26708053167/jobs"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
try:
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
    with open("scratch/job_details.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print("Done")
except Exception as e:
    print("Error:", e)

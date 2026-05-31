import urllib.request
import json
import sys

url = "https://api.github.com/repos/Mayur-Techs/ai-document-intelligence/actions/runs/26708314537/jobs"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
try:
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
    for j in data.get("jobs", []):
        print(f"Job: {j.get('name')} - ID: {j.get('id')} - Status: {j.get('status')} - Conclusion: {j.get('conclusion')}")
        print("Steps:")
        for s in j.get("steps", []):
            print(f"  Step: {s.get('name')} - Status: {s.get('status')} - Conclusion: {s.get('conclusion')}")
except Exception as e:
    print("Error:", e)

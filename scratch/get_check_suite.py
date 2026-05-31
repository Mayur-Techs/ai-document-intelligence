import urllib.request
import json

sha = "3ba8b96d934bb6f8d3886becc56403ab47a46c24"
url = f"https://api.github.com/repos/Mayur-Techs/ai-document-intelligence/commits/{sha}/check-runs"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
try:
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
    for cr in data.get("check_runs", []):
        print(f"Check Run: {cr.get('name')} - Status: {cr.get('status')} - Conclusion: {cr.get('conclusion')}")
        print("Details:")
        print(json.dumps(cr.get("output"), indent=2))
except Exception as e:
    print("Error:", e)

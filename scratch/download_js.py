import json
import urllib.request

headers = {"User-Agent": "Mozilla/5.0"}
url = "https://api.github.com/repos/Mayur-Techs/ai-document-intelligence/actions/runs"
req = urllib.request.Request(url, headers=headers)
with urllib.request.urlopen(req) as response:
    data = json.loads(response.read().decode("utf-8"))

for run in data.get("workflow_runs", [])[:5]:
    print(f"Workflow: {run['name']}")
    print(f"Commit: {run['head_commit']['message'][:50]}...")
    print(f"Status: {run['status']} | Conclusion: {run['conclusion']}")
    print(f"URL: {run['html_url']}")
    print("-" * 40)

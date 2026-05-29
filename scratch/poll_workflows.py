import json
import time
import urllib.request

headers = {"User-Agent": "Mozilla/5.0"}
url = "https://api.github.com/repos/Mayur-Techs/ai-document-intelligence/actions/runs"


def poll():
    start_time = time.time()
    while time.time() - start_time < 120:  # Poll for up to 2 minutes
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode("utf-8"))

            runs = [
                r
                for r in data.get("workflow_runs", [])
                if r["head_commit"]["id"].startswith("629c064")
            ]
            if not runs:
                print("No runs found for commit e152c3c yet...")
                time.sleep(5)
                continue

            all_done = True
            for r in runs:
                print(
                    f"Workflow: {r['name']} | Status: {r['status']} | Conclusion: {r['conclusion']}"
                )
                if r["status"] != "completed":
                    all_done = False

            if all_done:
                print("All workflows for the latest commit have completed!")
                return True

        except Exception as e:
            print("Error polling:", e)

        print("-" * 20)
        time.sleep(10)
    print("Polling timed out.")
    return False


if __name__ == "__main__":
    poll()

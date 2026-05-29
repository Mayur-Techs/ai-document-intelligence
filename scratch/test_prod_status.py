import requests
import time

url = "https://doc-intelligence-api-tubh.onrender.com/api/v1/documents/158"
print("Polling document status from production...")
for _ in range(5):
    res = requests.get(url + "/status")
    print("Status:", res.status_code, res.json())
    if res.json().get("status") in ["completed", "failed", "needs_review"]:
        break
    time.sleep(2)

print("Fetching full document details...")
res = requests.get(url)
print("Document:", res.status_code, res.json())

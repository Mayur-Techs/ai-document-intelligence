import urllib.request

headers = {"User-Agent": "Mozilla/5.0"}
req = urllib.request.Request("https://raw.githubusercontent.com/Mayur-Techs/docai-frontend/main/demo.html", headers=headers)
with urllib.request.urlopen(req) as response:
    content = response.read().decode('utf-8')
with open("scratch/demo.html", "w", encoding="utf-8") as f:
    f.write(content)
print("Saved to scratch/demo.html successfully!")




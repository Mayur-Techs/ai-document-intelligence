import urllib.request
import sys

job_id = "78712974548"
url = f"https://api.github.com/repos/Mayur-Techs/ai-document-intelligence/actions/jobs/{job_id}/logs"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

try:
    with urllib.request.urlopen(req) as response:
        log_content = response.read().decode("utf-8", errors="ignore")
    
    lines = log_content.splitlines()
    print(f"Total lines in log: {len(lines)}")
    
    # Let's find lines with errors or pytest summary
    # Let's print the last 150 lines
    start = max(0, len(lines) - 150)
    print("\n--- LAST 150 LINES OF LOG ---")
    for i in range(start, len(lines)):
        print(lines[i])
        
    print("\n--- SEARCHING FOR FAILURES ---")
    for idx, line in enumerate(lines):
        if "FAIL" in line or "error" in line.lower() or "exception" in line.lower():
            # Print a few context lines
            start_ctx = max(0, idx - 3)
            end_ctx = min(len(lines), idx + 10)
            print(f"\n--- Context near line {idx} ---")
            for c_idx in range(start_ctx, end_ctx):
                marker = ">>>" if c_idx == idx else "   "
                print(f"{marker} {lines[c_idx]}")
except Exception as e:
    print("Error:", e)

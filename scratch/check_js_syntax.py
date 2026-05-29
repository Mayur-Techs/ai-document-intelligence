import re
import subprocess
from pathlib import Path

html_path = Path(r"c:\Users\MAYUR\Documents\CODEs\ai-doc-intilligence-frontend\docai-frontend\demo.html")
content = html_path.read_text(encoding="utf-8")

# Extract contents between <script> and </script> (the last script block)
scripts = re.findall(r"<script>(.*?)</script>", content, re.DOTALL)
if scripts:
    js_content = scripts[-1]
    js_path = Path("scratch/extracted_demo.js")
    js_path.parent.mkdir(exist_ok=True)
    js_path.write_text(js_content, encoding="utf-8")
    print("Script extracted. Checking syntax with node...")
    
    # Run node --check
    res = subprocess.run(["node", "--check", str(js_path)], capture_output=True, text=True)
    if res.returncode == 0:
        print("Syntax is OK!")
    else:
        print("Syntax Error:")
        print(res.stderr)
else:
    print("No script tags found in demo.html!")

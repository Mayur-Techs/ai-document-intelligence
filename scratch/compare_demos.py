import difflib
from pathlib import Path

p1 = Path(r"c:\Users\MAYUR\Documents\CODEs\ai-doc-intilligence-frontend\docai-frontend\demo.html")
p2 = Path(r"c:\Users\MAYUR\Documents\CODEs\ai-document-intelligence\scratch\demo.html")

c1 = p1.read_text(encoding="utf-8").splitlines()
c2 = p2.read_text(encoding="utf-8").splitlines()

diff = list(difflib.unified_diff(c2, c1, fromfile="scratch/demo.html", tofile="frontend/demo.html"))
Path("scratch/demos_diff.txt").write_text("\n".join(diff), encoding="utf-8")
print("Diff written to scratch/demos_diff.txt")

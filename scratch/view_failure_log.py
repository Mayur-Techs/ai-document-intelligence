import os

log_path = "scratch/ci_test_failure.log"
try:
    with open(log_path, "r", encoding="utf-16") as f:
        content = f.read()
except Exception:
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

# Strip BOM if present
content = content.lstrip("\ufeff")

# Let's save it as clean UTF-8
with open("scratch/ci_test_failure_clean.log", "w", encoding="utf-8") as f:
    f.write(content)

print(f"Total characters: {len(content)}")
print("First 2000 chars (safe):")
# Strip non-ascii for printing safely
safe_print = content.encode("ascii", "replace").decode("ascii")
print(safe_print[:3000])

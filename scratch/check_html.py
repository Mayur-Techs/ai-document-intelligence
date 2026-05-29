import sys
import subprocess
import tempfile
from html.parser import HTMLParser

class SimpleHTMLValidator(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.errors = []
        self.in_script = False
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True
        if tag not in ["img", "input", "br", "hr", "meta", "link", "col", "embed"]:
            self.tags.append((tag, self.getpos()))

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False
        if tag in ["img", "input", "br", "hr", "meta", "link", "col", "embed"]:
            return
        if not self.tags:
            self.errors.append(f"Unexpected end tag </{tag}> at line {self.getpos()[0]}")
            return
        expected, pos = self.tags.pop()
        if expected != tag:
            self.errors.append(f"Mismatched tag </{tag}> at line {self.getpos()[0]}, expected </{expected}> from line {pos[0]}")

    def handle_data(self, data):
        if self.in_script:
            self.scripts.append(data)

def validate_file(filepath):
    print(f"Validating {filepath}...")
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"Failed to read file: {e}")
        return False

    parser = SimpleHTMLValidator()
    parser.feed(content)
    if parser.tags:
        for tag, pos in reversed(parser.tags):
            parser.errors.append(f"Unclosed tag <{tag}> from line {pos[0]}")

    if parser.errors:
        print("Validation errors found in HTML:")
        for err in parser.errors:
            print(f"  - {err}")
        return False
    else:
        print("HTML: No mismatched or unclosed tags.")

    # Validate JavaScript
    js_code = "\n".join(parser.scripts)
    with tempfile.NamedTemporaryFile(suffix=".js", delete=False, mode="w", encoding="utf-8") as temp_js:
        temp_js.write(js_code)
        temp_js_path = temp_js.name

    try:
        # Run node --check on the temp file
        res = subprocess.run(["node", "--check", temp_js_path], capture_output=True, text=True)
        if res.returncode != 0:
            print("JavaScript: Syntax validation failed:")
            print(res.stderr)
            return False
        else:
            print("JavaScript: No syntax errors found.")
            return True
    except FileNotFoundError:
        print("node is not installed or not in PATH. Skipping JavaScript check.")
        return True

if __name__ == "__main__":
    validate_file("c:/Users/MAYUR/Documents/CODEs/ai-doc-intilligence-frontend/docai-frontend/demo.html")

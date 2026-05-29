import requests

url = "https://doc-intelligence-api-tubh.onrender.com/api/v1/documents/upload?document_type=invoice"
dummy_pdf = b"%PDF-1.4 ... dummy pdf content ..."

files = {"file": ("dummy.pdf", dummy_pdf, "application/pdf")}

print("Sending POST request to production upload endpoint...")
try:
    res = requests.post(url, files=files)
    print("Status code:", res.status_code)
    print("Response headers:", res.headers)
    print("Response body:", res.text)
except Exception as e:
    print("Request failed:", e)

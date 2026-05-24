import urllib.error
import urllib.request


def upload_test():
    url = "https://doc-intelligence-api-tubh.onrender.com/api/v1/documents/upload"
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"

    # Simple valid PDF content structure
    pdf_content = (
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF"
    )

    body = []
    body.append(f"--{boundary}".encode())
    body.append(b'Content-Disposition: form-data; name="file"; filename="test.pdf"')
    body.append(b"Content-Type: application/pdf")
    body.append(b"")
    body.append(pdf_content)
    body.append(f"--{boundary}--".encode())
    body.append(b"")

    data = b"\r\n".join(body)

    req = urllib.request.Request(url, data=data)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("User-Agent", "Mozilla/5.0")

    try:
        with urllib.request.urlopen(req) as response:
            print("Status code:", response.getcode())
            print("Response body:", response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("HTTP Error code:", e.code)
        print("HTTP Error response:", e.read().decode("utf-8"))
    except Exception as e:
        print("Error:", e)


if __name__ == "__main__":
    upload_test()

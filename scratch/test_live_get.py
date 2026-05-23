import urllib.request
import urllib.error


def check_live_get():
    url = "https://doc-intelligence-api-tubh.onrender.com/api/v1/documents/"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header('Accept', 'text/html')

    try:
        with urllib.request.urlopen(req) as response:
            print("Status code:", response.getcode())
            print("Headers:", response.headers)
            print("Response body:", response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print("HTTP Error code:", e.code)
        print("Headers:", e.headers)
        try:
            body = e.read().decode('utf-8')
            print("HTTP Error response (first 2000 chars):", body[:2000])
        except Exception as read_err:
            print("Could not read body:", read_err)
    except Exception as e:
        print("Error:", e)


if __name__ == "__main__":
    check_live_get()

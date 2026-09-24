from pathlib import Path
from urllib.parse import urljoin
import mimetypes
from playwright.sync_api import sync_playwright

out = Path("images")
out.mkdir(exist_ok=True)

def ffn():
  with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto(
        "https://xxx.com",
        wait_until="domcontentloaded",
    )

    input("在浏览器中登录、滚动加载图片后，按回车继续：")

    urls = page.locator("img").evaluate_all(
        "(imgs) => imgs.map(i => i.currentSrc || i.src).filter(Boolean)"
    )

    for number, url in enumerate(dict.fromkeys(urls), 1):
        url = urljoin(page.url, url)
        if not url.startswith(("http://", "https://")):
            continue

        response = page.request.get(url, headers={"Referer": page.url})
        mime = response.headers.get("content-type", "").split(";")[0]
        if response.ok and mime.startswith("image/"):
            suffix = mimetypes.guess_extension(mime) or ".img"
            (out / f"{number:03d}{suffix}").write_bytes(response.body())

    browser.close()

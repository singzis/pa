"""Discover images loaded by a browser and visit a bounded set of pages."""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import BrowserContext, Page, Request, sync_playwright

from image_store import ImageStore, MAX_IMAGE_BYTES


SCROLL_ROUNDS = 12
PAGE_DELAY_SECONDS = 1
RETRY_DELAYS = (0, 0.5, 1)


@dataclass(frozen=True)
class CrawlOptions:
    url: str
    output: Path
    max_pages: int
    max_images: int
    min_width: int
    min_height: int
    formats: Set[str]
    manual_login: bool = False
    next_selector: Optional[str] = None


def _site(url: str) -> str:
    return urlsplit(url).hostname or ""


def _image_urls(page: Page) -> List[str]:
    urls = page.evaluate("""() => {
        const urls = [];
        for (const image of document.images) {
            if (image.currentSrc || image.src) urls.push(image.currentSrc || image.src);
        }
        for (const element of document.querySelectorAll('*')) {
            if (!element.getClientRects().length) continue;
            const background = getComputedStyle(element).backgroundImage;
            if (background === 'none') continue;
            for (const match of background.matchAll(/url\\(["']?([^"')]+)["']?\\)/g)) {
                urls.push(match[1]);
            }
        }
        return urls;
    }""")
    result = []
    for url in urls:
        absolute = urljoin(page.url, url)
        if urlsplit(absolute).scheme in ("http", "https") and absolute not in result:
            result.append(absolute)
    return result


def _scroll(page: Page) -> None:
    stable = 0
    previous = None
    for _ in range(SCROLL_ROUNDS):
        state = page.evaluate("""() => {
            window.scrollBy(0, Math.max(window.innerHeight * 0.85, 400));
            return [document.documentElement.scrollHeight, window.scrollY,
                    document.images.length];
        }""")
        page.wait_for_timeout(450)
        at_bottom = state[1] + page.evaluate("window.innerHeight") >= state[0] - 2
        stable = stable + 1 if at_bottom and state == previous else 0
        if stable >= 2:
            break
        previous = state


def _fetch(context: BrowserContext, url: str, source_page: str) -> bytes:
    error = "Download failed"
    for delay in RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            response = context.request.get(url, headers={"Referer": source_page}, timeout=15000)
            if response.ok:
                body = response.body()
                if len(body) > MAX_IMAGE_BYTES:
                    raise ValueError("Image exceeds 20 MiB")
                return body
            error = "HTTP {}".format(response.status)
        except Exception as exc:
            error = str(exc)
    raise RuntimeError(error)


def _next_page(page: Page, selector: str, hostname: str) -> bool:
    locator = page.locator(selector).first
    if locator.count() == 0 or not locator.is_enabled():
        return False
    href = locator.get_attribute("href")
    if href and _site(urljoin(page.url, href)) != hostname:
        return False
    old_url = page.url
    old_images = set(_image_urls(page))
    locator.click(timeout=10000)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(600)
    return _site(page.url) == hostname and (page.url != old_url or
                                             set(_image_urls(page)) != old_images)


def crawl(options: CrawlOptions) -> Dict[str, int]:
    hostname = _site(options.url)
    store = ImageStore(options.output, hostname, options.min_width,
                       options.min_height, options.formats)
    attempted = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not options.manual_login)
        context = browser.new_context()
        page = context.new_page()
        responses: Dict[str, bytes] = {}

        def collect(request: Request) -> None:
            if request.resource_type != "image":
                return
            response = request.response()
            if response is None or not response.ok:
                return
            try:
                body = response.body()
                if len(body) <= MAX_IMAGE_BYTES:
                    responses[response.url] = body
            except Exception:
                pass

        page.on("requestfinished", collect)
        try:
            page.goto(options.url, wait_until="domcontentloaded", timeout=30000)
            if options.manual_login:
                input("请在浏览器中完成登录，然后按回车开始抓取：")
                responses.clear()
                page.goto(options.url, wait_until="domcontentloaded", timeout=30000)

            def guard(route):
                request = route.request
                if request.is_navigation_request() and request.frame == page.main_frame \
                        and _site(request.url) != hostname:
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", guard)
            for page_number in range(1, options.max_pages + 1):
                if _site(page.url) != hostname:
                    raise RuntimeError("页面已离开起始域名；需要登录时请使用 --manual-login")
                _scroll(page)
                page.wait_for_timeout(500)
                source_page = page.url
                urls = list(dict.fromkeys(list(responses) + _image_urls(page)))
                for image_url in urls:
                    if attempted >= options.max_images:
                        break
                    if store.is_complete(source_page, image_url):
                        continue
                    attempted += 1
                    try:
                        body = responses.get(image_url)
                        if body is None:
                            body = _fetch(context, image_url, source_page)
                        store.save(page_number, source_page, image_url, body)
                    except Exception as exc:
                        store.failure(page_number, source_page, image_url, str(exc))
                if attempted >= options.max_images or not options.next_selector:
                    break
                time.sleep(PAGE_DELAY_SECONDS)
                responses.clear()
                if not _next_page(page, options.next_selector, hostname):
                    break
        finally:
            browser.close()
    return store.counts

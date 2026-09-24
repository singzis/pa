"""Discover images loaded by a browser and visit a bounded set of pages."""

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import (
    BrowserContext, Error as PlaywrightError, Page, Request, sync_playwright,
)

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
    auth_state: Optional[Path] = None


def _site(url: str) -> str:
    return urlsplit(url).hostname or ""


def _image_urls(page: Page) -> List[str]:
    result = []
    seen = set()
    for frame in page.frames:
        try:
            data = frame.evaluate("""() => {
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
        return {base: document.baseURI, urls};
    }""")
        except PlaywrightError:
            continue
        for url in data["urls"]:
            absolute = urljoin(data["base"], url)
            if urlsplit(absolute).scheme in ("http", "https") and absolute not in seen:
                seen.add(absolute)
                result.append(absolute)
    return result


def _scroll(page: Page) -> None:
    stable = 0
    previous = None
    for _ in range(SCROLL_ROUNDS):
        state = []
        for frame in page.frames:
            try:
                frame_state = frame.evaluate("""() => {
            window.scrollBy(0, Math.max(window.innerHeight * 0.85, 400));
            const containers = [...document.querySelectorAll('*')].filter(element => {
                const overflow = getComputedStyle(element).overflowY;
                return /auto|scroll|overlay/.test(overflow) &&
                    element.scrollHeight > element.clientHeight + 20 &&
                    element.clientHeight >= 100;
            }).slice(0, 20);
            for (const element of containers) {
                element.scrollBy(0, Math.max(element.clientHeight * 0.85, 400));
            }
            return [window.scrollY, document.images.length,
                    containers.map(element => element.scrollTop)];
        }""")
                state.append((frame.url, frame_state))
            except PlaywrightError:
                continue
        page.wait_for_timeout(450)
        stable = stable + 1 if state == previous else 0
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
    next_frame = None
    locator = None
    for frame in page.frames:
        candidate = frame.locator(selector).first
        if candidate.count() and candidate.is_enabled():
            next_frame, locator = frame, candidate
            break
    if locator is None or next_frame is None:
        return False
    href = locator.get_attribute("href")
    if href and _site(urljoin(next_frame.url, href)) != hostname:
        return False
    old_url = page.url
    old_frames = {frame.url for frame in page.frames}
    old_images = set(_image_urls(page))
    locator.click(timeout=10000)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(600)
    if _site(page.url) != hostname or _site(next_frame.url) != hostname:
        raise RuntimeError("下一页跳出了起始域名")
    return (page.url != old_url or {frame.url for frame in page.frames} != old_frames
            or set(_image_urls(page)) != old_images)


def _save_auth_state(context: BrowserContext, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(context.storage_state(indexed_db=True), handle)
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def crawl(options: CrawlOptions) -> Dict[str, int]:
    hostname = _site(options.url)
    store = ImageStore(options.output, hostname, options.min_width,
                       options.min_height, options.formats)
    attempted = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not options.manual_login)
        context = browser.new_context(
            storage_state=str(options.auth_state)
            if options.auth_state and options.auth_state.exists() else None
        )
        page = context.new_page()
        responses: Dict[str, bytes] = {}
        offsite_navigation = False

        def track_navigation(frame) -> None:
            nonlocal offsite_navigation
            if frame == page.main_frame and urlsplit(frame.url).scheme in ("http", "https") \
                    and _site(frame.url) != hostname:
                offsite_navigation = True

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
        page.on("framenavigated", track_navigation)
        try:
            page.goto(options.url, wait_until="domcontentloaded", timeout=30000)
            if options.manual_login:
                while True:
                    input("请完成登录，确认浏览器已返回目标页面后按回车开始抓取：")
                    page.wait_for_timeout(500)
                    if _site(page.url) != hostname:
                        print("浏览器尚未返回目标站点，请继续登录。")
                        continue
                    current = urlsplit(page.url)
                    target = urlsplit(options.url)
                    if (current.path, current.query, current.fragment) != \
                            (target.path, target.query, target.fragment):
                        responses.clear()
                        page.goto(options.url, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(500)
                        if _site(page.url) != hostname:
                            print("目标页面尚未打开，请继续完成登录。")
                            continue
                    break
                offsite_navigation = False
            for page_number in range(1, options.max_pages + 1):
                if offsite_navigation or _site(page.url) != hostname:
                    raise RuntimeError("页面已离开起始域名；需要登录时请使用 --manual-login")
                _scroll(page)
                page.wait_for_timeout(500)
                if offsite_navigation or _site(page.url) != hostname:
                    raise RuntimeError("页面跳转到其他域名，可能需要登录；请使用 --manual-login")
                source_page = page.url
                urls = list(dict.fromkeys(list(responses) + _image_urls(page)))
                for image_url in urls:
                    if attempted >= options.max_images:
                        break
                    if offsite_navigation or _site(page.url) != hostname:
                        raise RuntimeError("页面跳转到其他域名，可能需要登录；请使用 --manual-login")
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
                if offsite_navigation:
                    raise RuntimeError("下一页跳出了起始域名")
            if options.manual_login and options.auth_state:
                _save_auth_state(context, options.auth_state)
        finally:
            browser.close()
    return store.counts

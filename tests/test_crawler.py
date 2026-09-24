"""End-to-end coverage using a local, deterministic image site."""

import csv
import io
import json
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from crawler import CrawlOptions, crawl
from image_store import ImageStore


def png(color, size=(40, 40)):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


IMAGES = {
    "/a.png": png("red"),
    "/duplicate.png": png("red"),
    "/background.png": png("blue"),
    "/lazy.png": png("green"),
    "/small.png": png("yellow", (5, 5)),
    "/second.png": png("purple"),
    "/flaky.png": png("orange"),
    "/framed.png": png("cyan"),
}


class Site(BaseHTTPRequestHandler):
    flaky_requests = 0

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/page1":
            body = b"""<html><body style='min-height:1800px'>
              <img src='/a.png'><img src='/duplicate.png'><img src='/small.png'>
              <img src='/dead.png'><img src='/flaky.png'>
              <div style='width:40px;height:40px;background-image:url(/background.png)'></div>
              <div style='margin-top:1000px'><img id='lazy' width='40' height='40'></div>
              <a class='next' href='/page2'>Next</a>
              <script>addEventListener('scroll', () => {
                if (scrollY > 400) document.querySelector('#lazy').src = '/lazy.png';
              });</script></body></html>"""
            content_type = "text/html"
        elif self.path == "/page2":
            body = b"<html><body><img src='/second.png'></body></html>"
            content_type = "text/html"
        elif self.path == "/frame":
            body = (b"<html><body><iframe src='/inner' width='250' "
                    b"height='250'></iframe></body></html>")
            content_type = "text/html"
        elif self.path == "/inner":
            body = b"""<html><body style='min-height:2000px'>
              <div style='margin-top:1000px'><img id='framed'></div>
              <script>addEventListener('scroll', () => {
                if (scrollY > 400) document.querySelector('#framed').src = '/framed.png';
              });</script></body></html>"""
            content_type = "text/html"
        elif self.path == "/redirect":
            target = "http://localhost:{}/login".format(self.server.server_port)
            body = ("<html><body><img src='/a.png'><script>setTimeout(() => "
                    "location.href='{}', 150)</script></body></html>".format(target)).encode()
            content_type = "text/html"
        elif self.path == "/login":
            body = b"<html><body>Login required</body></html>"
            content_type = "text/html"
            self.send_response(200)
            self.send_header("Set-Cookie", "session=ok; Path=/; HttpOnly")
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body)
            return
        elif self.path == "/protected":
            if "session=ok" not in self.headers.get("Cookie", ""):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            body = b"<html><body><img src='/a.png'></body></html>"
            content_type = "text/html"
        elif self.path == "/flaky.png":
            type(self).flaky_requests += 1
            if type(self).flaky_requests < 3:
                self.send_response(503)
                self.end_headers()
                return
            body, content_type = IMAGES[self.path], "image/png"
        elif self.path in IMAGES:
            body, content_type = IMAGES[self.path], "image/png"
        else:
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CrawlerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:{}/page1".format(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_discovery_pagination_filter_retry_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            options = CrawlOptions(self.url, output, 2, 20, 20, 20, {"PNG"},
                                   next_selector="a.next")
            first = crawl(options)
            self.assertEqual(first["saved"], 5)
            self.assertGreaterEqual(first["duplicate"], 1)
            self.assertGreaterEqual(first["filtered"], 1)
            self.assertGreaterEqual(first["failed"], 1)
            self.assertGreaterEqual(Site.flaky_requests, 3)
            with (output / "manifest.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(any("/lazy.png" in row["image_url"] and row["status"] == "saved"
                                for row in rows))
            self.assertTrue(any("/background.png" in row["image_url"] and row["status"] == "saved"
                                for row in rows))
            self.assertTrue(any("/second.png" in row["image_url"] and row["status"] == "saved"
                                for row in rows))

            second = crawl(options)
            self.assertEqual(second["saved"], 0)
            self.assertGreaterEqual(second["resumed"], 6)

            saved_row = next(row for row in rows if row["status"] == "saved")
            (output / saved_row["file_path"]).write_bytes(b"damaged")
            repaired = crawl(options)
            self.assertEqual(repaired["saved"], 1)
            self.assertEqual((output / saved_row["file_path"]).read_bytes(),
                             IMAGES["/" + saved_row["image_url"].rsplit("/", 1)[-1]])

            limited = CrawlOptions(self.url, output / "limited", 1, 20, 0, 0, {"PNG"})
            limited_result = crawl(limited)
            self.assertGreaterEqual(limited_result["saved"], 4)
            with (limited.output / "manifest.csv").open(newline="", encoding="utf-8") as handle:
                limited_rows = list(csv.DictReader(handle))
            self.assertFalse(any("/second.png" in row["image_url"] for row in limited_rows))

            capped = CrawlOptions(self.url, output / "capped", 2, 2, 0, 0, {"PNG"},
                                  next_selector="a.next")
            crawl(capped)
            with (capped.output / "manifest.csv").open(newline="", encoding="utf-8") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 2)

    def test_supported_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ImageStore(Path(directory), "example.com", 0, 0,
                               {"PNG", "JPEG", "WEBP", "GIF"})
            for image_format in ("PNG", "JPEG", "WEBP", "GIF"):
                output = io.BytesIO()
                Image.new("RGB", (40, 40), "red").save(output, format=image_format)
                store.save(1, self.url, self.url + "/" + image_format, output.getvalue())
            self.assertEqual(store.counts["saved"], 4)

    def test_images_lazy_loaded_inside_iframe(self):
        with tempfile.TemporaryDirectory() as directory:
            url = self.url.replace("/page1", "/frame")
            options = CrawlOptions(url, Path(directory), 1, 20, 0, 0, {"PNG"})
            counts = crawl(options)
            self.assertEqual(counts["saved"], 1)

    def test_offsite_redirect_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            url = self.url.replace("/page1", "/redirect")
            options = CrawlOptions(url, Path(directory), 1, 20, 0, 0, {"PNG"})
            with self.assertRaisesRegex(RuntimeError, "登录|域名"):
                crawl(options)

    def test_login_state_is_saved_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth_state = root / ".auth" / "site.json"
            url = self.url.replace("/page1", "/protected")
            first = CrawlOptions(url, root / "first", 1, 20, 0, 0, {"PNG"},
                                 manual_login=True, auth_state=auth_state)
            with patch("builtins.input", return_value=""):
                self.assertEqual(crawl(first)["saved"], 1)
            self.assertTrue(auth_state.is_file())
            self.assertEqual(stat.S_IMODE(auth_state.stat().st_mode), 0o600)
            with auth_state.open(encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertTrue(any(cookie["name"] == "session" for cookie in state["cookies"]))

            second = CrawlOptions(url, root / "second", 1, 20, 0, 0, {"PNG"},
                                  auth_state=auth_state)
            self.assertEqual(crawl(second)["saved"], 1)


if __name__ == "__main__":
    unittest.main()

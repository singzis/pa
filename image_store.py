"""Validate images and maintain a resumable CSV manifest."""

import csv
import hashlib
import io
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

from PIL import Image, UnidentifiedImageError


FIELDS = (
    "page_number", "source_page", "image_url", "file_path", "sha256",
    "width", "height", "format", "status", "error",
)
EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class ImageStore:
    def __init__(self, output: Path, hostname: str, min_width: int,
                 min_height: int, formats: Set[str]) -> None:
        self.output = output
        self.image_dir = output / hostname
        self.manifest_path = output / "manifest.csv"
        self.min_width = min_width
        self.min_height = min_height
        self.formats = formats
        self.completed: Dict[Tuple[str, str], str] = {}
        self.hash_paths: Dict[str, str] = {}
        self.counts = {status: 0 for status in
                       ("saved", "duplicate", "filtered", "failed", "resumed")}
        self.output.mkdir(parents=True, exist_ok=True)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self._load_manifest()

    def _valid_file(self, relative_path: str, digest: str) -> bool:
        path = self.output / relative_path
        if (not path.is_file() or not digest or
                not path.resolve().is_relative_to(self.output.resolve())):
            return False
        hasher = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
        except OSError:
            return False
        return hasher.hexdigest() == digest

    def _load_manifest(self) -> None:
        if not self.manifest_path.exists():
            return
        with self.manifest_path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("status") not in ("saved", "duplicate"):
                    continue
                path, digest = row.get("file_path", ""), row.get("sha256", "")
                if self._valid_file(path, digest):
                    self.completed[(row["source_page"], row["image_url"])] = path
                    self.hash_paths[digest] = path

    def is_complete(self, source_page: str, image_url: str) -> bool:
        if (source_page, image_url) in self.completed:
            self.counts["resumed"] += 1
            return True
        return False

    def _record(self, page_number: int, source_page: str, image_url: str,
                status: str, file_path: str = "", digest: str = "",
                width: int = 0, height: int = 0, image_format: str = "",
                error: str = "") -> None:
        row = dict(page_number=page_number, source_page=source_page,
                   image_url=image_url, file_path=file_path, sha256=digest,
                   width=width or "", height=height or "", format=image_format,
                   status=status, error=error)
        new_file = not self.manifest_path.exists() or self.manifest_path.stat().st_size == 0
        with self.manifest_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
        self.counts[status] += 1
        if status in ("saved", "duplicate"):
            self.completed[(source_page, image_url)] = file_path

    def failure(self, page_number: int, source_page: str, image_url: str,
                error: str) -> None:
        self._record(page_number, source_page, image_url, "failed", error=error)

    def save(self, page_number: int, source_page: str, image_url: str,
             data: bytes) -> None:
        if len(data) > MAX_IMAGE_BYTES:
            self.failure(page_number, source_page, image_url, "Image exceeds 20 MiB")
            return
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                width, height = image.size
                image_format = image.format.upper()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            self.failure(page_number, source_page, image_url, str(exc))
            return

        if (image_format not in EXTENSIONS or image_format not in self.formats
                or width < self.min_width or height < self.min_height):
            self._record(page_number, source_page, image_url, "filtered",
                         width=width, height=height, image_format=image_format)
            return

        digest = hashlib.sha256(data).hexdigest()
        if digest in self.hash_paths and self._valid_file(self.hash_paths[digest], digest):
            self._record(page_number, source_page, image_url, "duplicate",
                         self.hash_paths[digest], digest, width, height, image_format)
            return

        target = self.image_dir / (digest + EXTENSIONS[image_format])
        relative_path = str(target.relative_to(self.output))
        if target.exists() and not self._valid_file(relative_path, digest):
            backup = target.with_name(target.name + ".corrupt-" + str(time.time_ns()))
            target.rename(backup)
        if not target.exists():
            temp_path: Optional[Path] = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.image_dir, delete=False) as handle:
                    temp_path = Path(handle.name)
                    handle.write(data)
                os.replace(str(temp_path), str(target))
            finally:
                if temp_path is not None and temp_path.exists():
                    temp_path.unlink()
        self.hash_paths[digest] = relative_path
        self._record(page_number, source_page, image_url, "saved",
                     relative_path, digest, width, height, image_format)

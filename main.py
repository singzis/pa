"""Command-line entry point for the image crawler."""

import argparse
from pathlib import Path
from urllib.parse import urlsplit

from crawler import CrawlOptions, crawl


FORMAT_NAMES = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP", "gif": "GIF"}


def main() -> None:
    parser = argparse.ArgumentParser(description="下载网页实际加载的位图")
    parser.add_argument("url", help="起始网页 URL")
    parser.add_argument("--manual-login", action="store_true", help="打开浏览器并等待手动登录")
    parser.add_argument("--auth-state", type=Path, help="读取或保存登录状态的 JSON 文件")
    parser.add_argument("--next-selector", help="下一页按钮的 CSS 选择器")
    parser.add_argument("--max-pages", type=int, default=1, help="最多访问页数，默认 1")
    parser.add_argument("--max-images", type=int, default=200, help="最多处理图片数，默认 200")
    parser.add_argument("--min-width", type=int, default=0, help="最小宽度（像素）")
    parser.add_argument("--min-height", type=int, default=0, help="最小高度（像素）")
    parser.add_argument("--formats", default="png,jpeg,webp,gif", help="允许格式，逗号分隔")
    parser.add_argument("--output", type=Path, default=Path("images"), help="输出目录")
    args = parser.parse_args()

    parsed_url = urlsplit(args.url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
        parser.error("URL 必须是包含域名的 http:// 或 https:// 地址")
    if args.max_pages < 1 or args.max_images < 1:
        parser.error("--max-pages 和 --max-images 必须大于 0")
    if args.min_width < 0 or args.min_height < 0:
        parser.error("最小尺寸不能为负数")
    if args.max_pages > 1 and not args.next_selector:
        parser.error("翻页时必须指定 --next-selector")
    if args.auth_state and args.auth_state.exists() and not args.auth_state.is_file():
        parser.error("--auth-state 必须是文件路径")
    if args.auth_state and not args.manual_login and not args.auth_state.is_file():
        parser.error("登录状态文件不存在；首次使用请同时指定 --manual-login")
    names = [name.strip().lower() for name in args.formats.split(",")]
    if not names or any(name not in FORMAT_NAMES for name in names):
        parser.error("--formats 仅支持 png,jpeg,jpg,webp,gif")

    options = CrawlOptions(
        url=args.url, output=args.output, max_pages=args.max_pages,
        max_images=args.max_images, min_width=args.min_width,
        min_height=args.min_height, formats={FORMAT_NAMES[name] for name in names},
        manual_login=args.manual_login, next_selector=args.next_selector,
        auth_state=args.auth_state,
    )
    try:
        counts = crawl(options)
    except Exception as exc:
        parser.exit(1, "抓取失败：{}\n".format(exc))
    print("抓取完成：" + "，".join("{} {}".format(status, count)
                              for status, count in counts.items()))


if __name__ == "__main__":
    main()

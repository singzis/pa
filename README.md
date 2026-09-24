# 图片爬取器

使用 Playwright 抓取网页实际加载的 PNG、JPEG、WebP 和 GIF 图片，包括懒加载图片与 CSS 背景图。图片按内容哈希命名，重复图片只存一份。

## 安装

```bash
uv sync
uv run playwright install chromium
```

## 使用

```bash
uv run python main.py "https://example.com/gallery"
uv run python main.py "https://example.com/gallery" --max-pages 3 --next-selector "a.next" --max-images 100
uv run python main.py "https://example.com/gallery" --manual-login --min-width 300 --min-height 300 --formats png,jpeg --output images
```

默认只抓 1 页、最多处理 200 张图片。`--max-pages` 大于 1 时必须指定 `--next-selector`。`--max-images` 限制本次实际处理的候选图片数，不含续跑时跳过的图片。自动滚动最多 12 次，翻页间隔 1 秒；翻页只允许留在起始域名，图片可来自 CDN。

结果保存到 `<output>/<起始域名>/`，`<output>/manifest.csv` 记录来源页、图片 URL、路径、哈希、尺寸、格式和处理状态。重复运行相同命令时，会核对清单中的文件及哈希并跳过已完成图片；缺失或损坏的文件会重新下载。失败图片最多补取 2 次，其他图片继续处理。单张图片上限 20 MiB。

需要登录时使用 `--manual-login`，在弹出的浏览器里完成登录并按终端提示继续。首版只处理浏览器实际加载的位图，不抓隐藏接口数据、点击后原图、SVG、Canvas 或验证码。

对于京东等会跳转到单点登录的站点，请使用 `--manual-login`。程序会扫描并滚动页面内的 iframe；如果仍未完成登录，会明确报错，不会把登录页或浏览器错误页算作抓取结果。建议为每个任务指定独立的 `--output`，避免与之前的下载结果混在一起。

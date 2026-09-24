import asyncio
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urldefrag, urljoin
import cloudscraper
from bs4 import BeautifulSoup
from ebooklib import epub
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

# ============================================================
# WEB SERVER TƯƠNG THÍCH UPTIMEROBOT (GET & HEAD) CHO RENDER
# ============================================================


class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):

  def do_GET(self):
    self.send_response(200)
    self.send_header("Content-type", "text/html; charset=utf-8")
    self.end_headers()
    self.wfile.write("Bot Truyen đang hoạt động!".encode("utf-8"))

  def do_HEAD(self):
    self.send_response(200)
    self.send_header("Content-type", "text/html; charset=utf-8")
    self.end_headers()

  def log_message(self, format, *args):
    return


def run_web_server():
  port = int(os.environ.get("PORT", 10000))
  server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
  server.serve_forever()


# ============================================================
# CẤU HÌNH BOT & CLOUDSCRAPER
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN_TRUYEN") or os.getenv("BOT_TOKEN")

scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "desktop": True}
)


def get_content(url):
  try:
    res = scraper.get(url, timeout=30)
    if res.status_code == 200:
      return BeautifulSoup(res.text, "lxml")
  except Exception as e:
    print(f"Lỗi tải {url}: {e}")
  return None


def extract_chapter_number(name):
  """Phân loại thứ tự: Giới thiệu/Văn án lên đầu (-1), Chương chính ở giữa, Ngoại truyện xuống cuối"""
  lower_name = name.lower()

  # Nếu là trang giới thiệu, văn án hoặc kiểu "1 person" -> Đưa lên đầu tiên
  if "person" in lower_name or "giới thiệu" in lower_name or "van an" in lower_name:
    return -1

  numbers = re.findall(r"\d+", name)
  num = int(numbers[0]) if numbers else 0

  # Nếu là ngoại truyện hoặc NT -> Đẩy xuống cuối sau các chương chính
  if (
      "ngoại" in lower_name
      or "ngoai" in lower_name
      or "extra" in lower_name
      or "nt" in lower_name
  ):
    return 100000 + num

  return num


def get_chapters(url):
  soup = get_content(url)
  if not soup:
    return [], "Truyện", None

  # 1. Lấy tiêu đề truyện chuẩn
  og_title = soup.find("meta", property="og:title")
  if og_title and og_title.get("content"):
    title = og_title["content"]
  else:
    title_el = soup.select_one("h1") or soup.title
    title = title_el.get_text().strip() if title_el else "Truyện"

  if "|" in title:
    title = title.split("|")[0].strip()

  # 2. Lấy ảnh bìa tối ưu toàn diện cho WordPress
  cover_url = None
  og_img = (
      soup.find("meta", property="og:image")
      or soup.find("meta", property="twitter:image")
      or soup.find("meta", attrs={"name": "twitter:image:src"})
      or soup.find("meta", attrs={"itemprop": "image"})
  )
  if og_img and og_img.get("content"):
    cover_url = og_img["content"]

  if not cover_url:
    img_el = soup.select_one(
        ".entry-content img, .post-content img, article img,"
        " .wp-block-image img, .book img, .info-image img, .story-image img,"
        " .product-image img, img.cover, .detail img"
    )
    if img_el:
      cover_url = (
          img_el.get("data-src") or img_el.get("data-original") or img_el.get("src")
      )

  if cover_url:
    cover_url = urljoin(url, cover_url)

  # 3. Quét danh sách chương (Bao gồm trang Giới thiệu dạng "person" và các chương chuẩn)
  chapters = []
  for a in soup.find_all("a", href=True):
    href = urldefrag(urljoin(url, a.get("href")))[0]
    text = a.get_text().strip()

    is_chap = re.match(
        r"^(chương|chuong|hồi|hoi|quyển|quyen|c\s*\d+|\d+$|phần|phan|pn\s*\d+|nt\s*\d+|ngoại"
        r" truyện|.*person.*)",
        text,
        flags=re.IGNORECASE,
    )

    if is_chap and len(text) < 80:
      if href.startswith("http") and not any(
          x in href for x in ["#", "wp-login", "author", "category", "tag", "feed"]
      ):
        if not any(c["url"] == href for c in chapters):
          chapters.append({"name": text, "url": href})

  # SẮP XẾP LẠI THỨ TỰ: Giới thiệu -> Chương chính -> Ngoại truyện
  chapters.sort(key=lambda x: extract_chapter_number(x["name"]))

  return chapters, title, cover_url


def download_chap(url):
  soup = get_content(url)
  if not soup:
    return None

  container = (
      soup.select_one(".entry-content")
      or soup.select_one(".elementor-widget-theme-post-content")
      or soup.select_one(".post-content")
      or soup.select_one(".chapter-content")
      or soup.select_one("#chapter-c")
      or soup.select_one("article")
      or soup.select_one(".post-body")
      or soup.body
  )

  if not container:
    container = soup

  # XÓA TRIỆT ĐỂ CÁC KHỐI RÁC
  for garbage in container.select(
      ".entry-header, .post-info, .breadcrumbs, .breadcrumb, nav, footer,"
      " header, script, style, form, aside"
  ):
    garbage.decompose()

  paragraphs = container.find_all("p")
  valid_p = []

  ignore_keywords = [
      "bỏ qua nội dung",
      "trang chủ",
      "lượt xem:",
      "cập nhật:",
      "chia sẻ",
      "thích",
      "đang tải",
      "có liên quan",
      "báo lỗi",
      "khám phá thêm",
      "đăng nhập",
      "bình luận",
      "viết:",
      "lúc",
      "danh sách",
  ]

  for p in paragraphs:
    text = p.get_text().strip()
    if not text:
      continue
    lower_text = text.lower()

    if any(kw in lower_text for kw in ignore_keywords) and len(text) < 80:
      continue

    valid_p.append(str(p))

  if valid_p:
    return "".join(valid_p)

  return str(container)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
  url_match = re.findall(r"https?://[^\s]+", update.message.text or "")
  if not url_match:
    return

  status = await update.message.reply_text(
      "⏳ Đang kết nối và quét danh sách chương..."
  )
  story_url = url_match[0]

  chapters, title, cover_url = get_chapters(story_url)

  if not chapters:
    await status.edit_text(
        "❌ Không tìm thấy chương nào. Hãy kiểm tra lại đường dẫn trang chính"
        " của truyện."
    )
    return

  await status.edit_text(
      f"📚 {title}\n✅ Tìm thấy {len(chapters)} chương. Đang tải nội dung..."
  )

  results = {}
  for i, c in enumerate(chapters):
    results[i] = download_chap(c["url"])
    if i % 15 == 0 or i == len(chapters) - 1:
      pct = int(((i + 1) / len(chapters)) * 100)
      try:
        await status.edit_text(
            f"📚 {title}\n⏳ Đang tải: {pct}%\n({i+1}/{len(chapters)})"
        )
      except:
        pass
    time.sleep(0.3)

  # Tạo file EPUB chuẩn Kindle
  book = epub.EpubBook()
  book.set_identifier("truyen_" + re.sub(r"\W+", "", title))
  book.set_title(title)
  book.set_language("vi")

  # Thêm ảnh bìa
  if cover_url:
    try:
      img_res = scraper.get(cover_url, timeout=15)
      if img_res.status_code == 200:
        book.set_cover("cover.jpg", img_res.content)
    except Exception as e:
      print(f"Lỗi tải ảnh bìa: {e}")

  chapters_list = []
  for i, c in enumerate(chapters):
    if results.get(i):
      chap = epub.EpubHtml(title=c["name"], file_name=f"chap_{i+1}.xhtml")
      chap.content = f"<h2>{c['name']}</h2>{results[i]}"
      book.add_item(chap)
      chapters_list.append(chap)

  if not chapters_list:
    await status.edit_text("❌ Không tải được nội dung chương nào.")
    return

  # Cấu hình Mục lục (TOC) chuẩn Kindle
  book.toc = tuple(chapters_list)
  book.spine = ["nav"] + chapters_list

  book.add_item(epub.EpubNcx())
  book.add_item(epub.EpubNav())

  safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "Truyen"
  file_out = f"{safe_title}.epub"
  epub.write_epub(file_out, book)

  await status.edit_text(f"⬆️ Đang gửi file EPUB...")
  with open(file_out, "rb") as f:
    await update.message.reply_document(
        document=f,
        caption=(
            f"✅ Hoàn tất: {title}\n📖 Trọn bộ {len(chapters_list)} chương (Đã"
            " sắp xếp Giới thiệu lên đầu, Ngoại truyện xuống cuối!)"
        ),
    )

  await status.delete()
  if os.path.exists(file_out):
    os.remove(file_out)


def main():
  threading.Thread(target=run_web_server, daemon=True).start()

  if not BOT_TOKEN:
    print("❌ Lỗi: Thiếu BOT_TOKEN!")
    return

  app = Application.builder().token(BOT_TOKEN).build()
  app.add_handler(
      MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
  )
  app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
  main()

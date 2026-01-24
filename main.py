import os
import feedparser
import time
import logging
import re
import requests
import configparser
import xml.etree.ElementTree as ET
from xml.dom import minidom
import sys
import html

def _mask(s, keep_start=4, keep_end=4):
    try:
        if not s:
            return "<empty>"
        if len(s) <= keep_start + keep_end:
            return "*" * len(s)
        return s[:keep_start] + "*" * (len(s) - keep_start - keep_end) + s[-keep_end:]
    except Exception:
        return "<hidden>"

# إعداد logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("rss_translator.log"),
        logging.StreamHandler()
    ]
)

# ************ قراءة المتغيرات الأساسية من config.ini ************
BASE_DIR = os.getcwd()
CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")
config = configparser.ConfigParser()
config.read(CONFIG_FILE, encoding='utf-8')

# قراءة الإعدادات
GEMINI_API_KEY = config.get('credentials', 'gemini_api_key', fallback=None)

GEMINI_MODELS_STR = config.get('settings', 'gemini_models', 
    fallback='gemini-2.5-flash,gemini-2.5-flash-lite,gemini-3-flash')
GEMINI_MODELS = [model.strip() for model in GEMINI_MODELS_STR.split(',') if model.strip()]

RSS_URLS_STR = config.get('settings', 'rss_urls', 
    fallback='https://feed.alternativeto.net/news/all')
RSS_URLS = [url.strip() for url in RSS_URLS_STR.split(',') if url.strip()]

TRACKER_FILE = "last_post_id.txt"
FEED_FILE = "feed.xml"

# برومبت الترجمة
TRANSLATION_PROMPT = """ترجم النص التالي إلى العربية بسلاسة ووضوح:

{text}"""

TRANSLATION_TEMPERATURE = 0.2
TRANSLATION_TOP_P = 0.8

class GeminiModelSwitcher:
    def __init__(self, models):
        self.models = models
        self.current_index = 0

    def get_current_model(self):
        return self.models[self.current_index]

    def get_next_model(self):
        if self.current_index < len(self.models) - 1:
            self.current_index += 1
            return self.models[self.current_index]
        return None

    def reset(self):
        self.current_index = 0

def validate_config():
    missing = []
    if not os.path.exists(CONFIG_FILE):
        logging.error(f"config.ini not found at: {CONFIG_FILE}")
        return False

    if not GEMINI_API_KEY:
        missing.append("gemini_api_key")
    if not GEMINI_MODELS:
        missing.append("gemini_models")
    if not RSS_URLS:
        missing.append("rss_urls")

    if missing:
        logging.error(f"Missing: {', '.join(missing)}")
        return False

    logging.info("✅ Config verified")
    return True

def normalize_url(url):
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    return url

def get_last_post_id():
    try:
        if not os.path.exists(TRACKER_FILE):
            return ""

        with open(TRACKER_FILE, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            return normalize_url(content)
    except Exception as e:
        logging.error(f"❌ Error reading tracker: {e}")
        return ""

def set_last_post_id(post_id):
    try:
        with open(TRACKER_FILE, 'w', encoding='utf-8') as f:
            f.write(normalize_url(post_id))
    except Exception as e:
        logging.error(f"❌ Failed to write tracker: {e}")

def clean_html(raw_html):
    """تنظيف HTML وإزالة الوسوم"""
    if not raw_html:
        return ""
    
    # إزالة وسوم HTML
    text = re.sub(r'<[^>]+>', ' ', raw_html)
    
    # إزالة مسافات زائدة
    text = re.sub(r'\s+', ' ', text)
    
    # إزالة رموز HTML
    text = html.unescape(text)
    
    return text.strip()

def translate_with_gemini(text, model_switcher, content_type="text"):
    """ترجمة النص باستخدام Gemini"""
    if not GEMINI_API_KEY:
        logging.error("API key missing")
        return text

    if not text or len(text.strip()) < 5:
        return text

    start_index = model_switcher.current_index
    attempted_models = 0

    while attempted_models < len(model_switcher.models):
        current_model = model_switcher.get_current_model()
        attempted_models += 1

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={GEMINI_API_KEY}"
        
        # تقصير النص إذا كان طويلاً جداً
        if len(text) > 3000:
            text = text[:3000] + "..."

        prompt = TRANSLATION_PROMPT.format(text=text)

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": TRANSLATION_TEMPERATURE,
                "topP": TRANSLATION_TOP_P,
                "maxOutputTokens": 1000
            }
        }
        
        headers = {"Content-Type": "application/json"}

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            
            result = response.json()['candidates'][0]['content']['parts'][0]['text'].strip()
            
            # تنظيف النتيجة
            result = re.sub(r'^(?:الترجمة|النص المترجم|المحتوى المترجم)[:\s]*', '', result, flags=re.IGNORECASE)
            result = result.strip()
            
            if result:
                logging.info(f"✅ Translated {content_type}")
                return result
            else:
                return text

        except Exception as e:
            logging.warning(f"⚠️ Translation failed with {current_model}: {str(e)[:100]}...")
            
            next_model = model_switcher.get_next_model()
            if next_model:
                time.sleep(1)
                continue
            else:
                break

    model_switcher.current_index = start_index
    return text

def ensure_feed_file():
    """تأكد من وجود feed.xml صالح"""
    if not os.path.exists(FEED_FILE):
        create_empty_feed()
        return None
    
    try:
        # محاولة تحليل الملف
        tree = ET.parse(FEED_FILE)
        root = tree.getroot()
        
        # تحقق من أن الملف يحتوي على هيكل RSS صحيح
        if root.tag != 'rss':
            logging.warning("feed.xml doesn't have proper RSS structure, recreating...")
            create_empty_feed()
            return None
        
        channel = root.find('channel')
        if channel is None:
            logging.warning("feed.xml missing channel, recreating...")
            create_empty_feed()
            return None
            
        return root
    except Exception as e:
        logging.warning(f"Could not parse feed.xml: {e}, recreating...")
        create_empty_feed()
        return None

def create_empty_feed():
    """إنشاء ملف feed.xml فارغ"""
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    
    ET.SubElement(channel, "title").text = "ترجمة RSS"
    ET.SubElement(channel, "description").text = "منشورات مترجمة من مصادر RSS"
    ET.SubElement(channel, "link").text = "https://github.com"
    ET.SubElement(channel, "language").text = "ar"
    ET.SubElement(channel, "lastBuildDate").text = time.strftime('%a, %d %b %Y %H:%M:%S %z')
    
    save_feed(rss)
    return rss

def save_feed(feed_root):
    """حفظ feed.xml"""
    try:
        # تحديث تاريخ البناء
        last_build = feed_root.find(".//lastBuildDate")
        if last_build is not None:
            last_build.text = time.strftime('%a, %d %b %Y %H:%M:%S %z')
        
        # تحويل إلى XML منظم
        rough_string = ET.tostring(feed_root, encoding='utf-8')
        reparsed = minidom.parseString(rough_string)
        pretty_xml = reparsed.toprettyxml(indent="  ", encoding='utf-8')
        
        with open(FEED_FILE, 'wb') as f:
            f.write(pretty_xml)
        
        item_count = len(feed_root.find('.//channel').findall('item'))
        logging.info(f"💾 Saved feed.xml with {item_count} items")
        return True
    except Exception as e:
        logging.error(f"❌ Failed to save feed.xml: {e}")
        return False

def add_item_to_feed(feed_root, entry, translated_title, translated_content):
    """إضافة عنصر جديد إلى feed.xml"""
    try:
        channel = feed_root.find(".//channel")
        if channel is None:
            return False
        
        item = ET.SubElement(channel, "item")
        
        # العنوان المترجم
        ET.SubElement(item, "title").text = translated_title[:500]  # الحد من الطول
        
        # الرابط
        link = entry.get('link', '')
        if link:
            ET.SubElement(item, "link").text = link
        
        # المحتوى المترجم
        ET.SubElement(item, "description").text = translated_content[:2000]  # الحد من الطول
        
        # المعرف
        post_id = entry.get('id') or link
        if post_id:
            guid = ET.SubElement(item, "guid")
            guid.text = post_id
            guid.set('isPermaLink', 'false')
        
        # التاريخ
        published = entry.get('published', entry.get('updated', ''))
        if published:
            ET.SubElement(item, "pubDate").text = published
        
        # المصدر
        feed_source = entry.get('feed_source', '')
        if feed_source:
            ET.SubElement(item, "source").text = feed_source
        
        return True
    except Exception as e:
        logging.error(f"❌ Failed to add item: {e}")
        return False

def get_feed_entries():
    """جلب المنشورات من جميع مصادر RSS"""
    all_entries = []
    
    for rss_url in RSS_URLS:
        try:
            logging.info(f"📥 Fetching: {rss_url}")
            feed = feedparser.parse(rss_url)
            
            if feed.entries:
                for entry in feed.entries:
                    entry['feed_source'] = rss_url
                    all_entries.append(entry)
                logging.info(f"  Found {len(feed.entries)} entries")
            else:
                logging.warning(f"  No entries found")
                
        except Exception as e:
            logging.error(f"❌ Failed to parse {rss_url}: {e}")
    
    # ترتيب حسب التاريخ
    all_entries.sort(key=lambda e: e.get('published_parsed') or e.get('updated_parsed') or (0,))
    
    logging.info(f"📊 Total entries: {len(all_entries)}")
    return all_entries

def process_entries(entries, model_switcher, feed_root):
    """معالجة المنشورات"""
    last_id = get_last_post_id()
    
    if not last_id:
        # إذا لم يكن هناك آخر معرف، معالجة آخر 3 منشورات
        logging.info("No last ID found, processing latest 3 posts")
        entries_to_process = entries[-3:] if len(entries) >= 3 else entries
    else:
        # البحث عن آخر معرف
        last_index = -1
        for i, entry in enumerate(entries):
            current_id = entry.get('id') or entry.get('link')
            if normalize_url(current_id) == last_id:
                last_index = i
                break
        
        if last_index >= 0:
            entries_to_process = entries[last_index + 1:]
        else:
            logging.warning("Last ID not found, processing latest 3 posts")
            entries_to_process = entries[-3:] if len(entries) >= 3 else entries
    
    if not entries_to_process:
        logging.info("⏭️ No new posts to process")
        return False
    
    logging.info(f"🔄 Processing {len(entries_to_process)} new posts")
    
    processed_count = 0
    for entry in entries_to_process:
        if processed_count >= 5:  # الحد الأقصى 5 منشورات في كل تشغيل
            logging.info("⚠️ Reached maximum posts per run (5)")
            break
        
        post_id = entry.get('id') or entry.get('link')
        logging.info(f"\n🎯 Processing: {post_id}")
        
        # العنوان الأصلي
        original_title = clean_html(entry.get('title', 'No Title'))
        if not original_title or original_title == 'No Title':
            continue
        
        # المحتوى الأصلي
        original_content = clean_html(
            entry.get('summary', '') or 
            entry.get('description', '') or 
            entry.get('content', '') or 
            original_title
        )
        
        # ترجمة العنوان
        translated_title = translate_with_gemini(original_title, model_switcher, "title")
        if translated_title == original_title:
            translated_title = original_title  # استخدام الأصل إذا فشلت الترجمة
        
        # ترجمة المحتوى
        translated_content = translate_with_gemini(original_content, model_switcher, "content")
        if translated_content == original_content:
            translated_content = original_content
        
        # إضافة إلى feed.xml
        if add_item_to_feed(feed_root, entry, translated_title, translated_content):
            set_last_post_id(post_id)
            save_feed(feed_root)
            processed_count += 1
            
            # انتظار بين المنشورات
            time.sleep(2)
    
    return processed_count > 0

def main():
    logging.info("🚀 Starting RSS Translator")
    
    # التحقق من الإعدادات
    if not validate_config():
        return
    
    # إنشاء أو تحميل feed.xml
    feed_root = ensure_feed_file()
    if feed_root is None:
        feed_root = create_empty_feed()
    
    # إنشاء مدير النماذج
    model_switcher = GeminiModelSwitcher(GEMINI_MODELS)
    
    # جلب المنشورات
    entries = get_feed_entries()
    if not entries:
        logging.info("📭 No entries found in any RSS feed")
        return
    
    # معالجة المنشورات
    if process_entries(entries, model_switcher, feed_root):
        logging.info("✅ Processing completed successfully")
    else:
        logging.info("⏭️ Nothing processed")
    
    logging.info("🏁 RSS Translator finished")

if __name__ == "__main__":
    main()
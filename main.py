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
        logging.FileHandler(os.path.join(os.getcwd(), "rss_translator.log")),
        logging.StreamHandler()
    ]
)

# ************ قراءة المتغيرات الأساسية من config.ini ************
BASE_DIR = os.getcwd()  # المجلد الجذر
CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")
config = configparser.ConfigParser()
config.read(CONFIG_FILE, encoding='utf-8')

# قراءة الإعدادات من config.ini
GEMINI_API_KEY = config.get('credentials', 'gemini_api_key', fallback=None)

# قراءة قائمة النماذج من config.ini
GEMINI_MODELS_STR = config.get('settings', 'gemini_models', fallback='gemini-2.5-flash,gemini-2.5-flash-lite,gemini-3-flash,gemini-robotics-er-1.5-preview')
GEMINI_MODELS = [model.strip() for model in GEMINI_MODELS_STR.split(',') if model.strip()]

# قراءة قائمة روابط RSS
RSS_URLS_STR = config.get('settings', 'rss_urls', 
    fallback='https://feed.alternativeto.net/news/all,https://www.elbilad.net/rss')
RSS_URLS = [url.strip() for url in RSS_URLS_STR.split(',') if url.strip()]

TRACKER_FILE = os.path.join(BASE_DIR, "last_post_id.txt")
FEED_FILE = os.path.join(BASE_DIR, "feed.xml")

# برومبتات الترجمة (بدون عبارات إضافية)
TRANSLATION_PROMPT = """ترجم النص التالي إلى العربية بدقة وحافظ على المعنى الأصلي:

{text}"""

TRANSLATION_TEMPERATURE = 0.2
TRANSLATION_TOP_P = 0.8

# تحقق تفصيلي من تحميل الإعدادات
try:
    logging.info(f"=== RSS Translator Started ===")
    logging.info(f"Python version: {sys.version}")
    logging.info(f"Working directory: {BASE_DIR}")
    logging.info(f"Config file: {CONFIG_FILE} (exists: {os.path.exists(CONFIG_FILE)})")
    logging.info(f"GEMINI_API_KEY: {_mask(GEMINI_API_KEY)}")
    logging.info(f"Number of RSS feeds: {len(RSS_URLS)}")
    logging.info(f"RSS feeds: {RSS_URLS}")
except Exception as _e:
    logging.error(f"Failed to log config diagnostics: {_e}")

class GeminiModelSwitcher:
    """يدير تبديل نماذج Gemini"""
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
    """يتحقق من أن config.ini والحقول الأساسية محمّلة"""
    missing = []
    if not os.path.exists(CONFIG_FILE):
        logging.error(f"config.ini not found at: {CONFIG_FILE}")
        return False

    if not GEMINI_API_KEY:
        missing.append("gemini_api_key")
    if not GEMINI_MODELS:
        missing.append("gemini_models (at least one model required)")
    if not RSS_URLS:
        missing.append("rss_urls (at least one RSS feed required)")

    if missing:
        logging.error(f"Missing required config keys: {', '.join(missing)}")
        return False

    logging.info("✅ Config verified successfully.")
    return True

def normalize_url(url):
    """تطبيع الروابط"""
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    while url.endswith('//'):
        url = url[:-1]
    if not url.startswith('http'):
        return url
    if not url.endswith('/'):
        url = url + '/'
    return url

def get_last_post_id():
    """يقرأ آخر معرّف منشور تمت معالجته."""
    try:
        if not os.path.exists(TRACKER_FILE):
            logging.info("📂 Tracker file not found. Starting fresh.")
            with open(TRACKER_FILE, 'w', encoding='utf-8') as f:
                f.write("")
            return ""

        with open(TRACKER_FILE, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            normalized = normalize_url(content)
            logging.info(f"📖 Last processed ID: '{normalized}'")
            return normalized
    except Exception as e:
        logging.error(f"❌ Error reading tracker file: {e}")
        return ""

def set_last_post_id(post_id):
    """يكتب معرّف المنشور الحالي بعد معالجته."""
    try:
        normalized_id = normalize_url(post_id)
        with open(TRACKER_FILE, 'w', encoding='utf-8') as f:
            f.write(normalized_id)
        logging.info(f"💾 Saved to tracker: '{normalized_id}'")
    except Exception as e:
        logging.error(f"❌ Failed to write to tracker file: {e}")

def clean_html(raw_html):
    """ينظف النص من وسوم HTML."""
    return re.sub(r'<[^>]+>', '', raw_html).strip()

def translate_with_gemini(text, model_switcher, content_type="text"):
    """يترجم النص إلى العربية باستخدام Gemini."""
    if not GEMINI_API_KEY:
        logging.error("GEMINI_API_KEY is not set.")
        return None

    if not text or len(text.strip()) < 3:
        logging.warning(f"Text is too short to translate ({content_type}).")
        return text

    # محاولة جميع النماذج
    start_index = model_switcher.current_index
    attempted_models = 0

    while attempted_models < len(model_switcher.models):
        current_model = model_switcher.get_current_model()
        attempted_models += 1

        logging.info(f"🔧 Translating {content_type} with model: {current_model}")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={GEMINI_API_KEY}"

        # برومبت بسيط بدون عبارات إضافية
        prompt = TRANSLATION_PROMPT.format(text=text)

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": TRANSLATION_TEMPERATURE,
                "topP": TRANSLATION_TOP_P
            }
        }
        headers = {"Content-Type": "application/json"}

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            
            result = response.json()['candidates'][0]['content']['parts'][0]['text'].strip()
            
            # إزالة أي عبارات مثل "النص المترجم:" أو "الترجمة:"
            result = re.sub(r'^(?:النص\s*المترجم|الترجمة|المحتوى\s*المترجم)[:\s]*', '', result, flags=re.IGNORECASE)
            result = result.strip()
            
            logging.info(f"✅ Translated {content_type} successfully")
            return result

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else None

            if status_code == 429:
                logging.warning(f"⚠️ Rate limit (429) with model: {current_model}")
            elif status_code in [400, 404, 500, 503]:
                logging.warning(f"⚠️ API error {status_code} with model: {current_model}")
            else:
                logging.error(f"❌ Gemini HTTP error {status_code} with model {current_model}: {e}")

            # محاولة النموذج التالي
            next_model = model_switcher.get_next_model()
            if next_model:
                logging.info(f"🔄 Switching to next model: {next_model}")
                time.sleep(2)
                continue
            else:
                logging.error(f"🚨 All {len(model_switcher.models)} models exhausted")
                break

        except Exception as e:
            logging.error(f"❌ Gemini API call failed with model {current_model}: {e}")

            # محاولة النموذج التالي
            next_model = model_switcher.get_next_model()
            if next_model:
                logging.info(f"🔄 Switching to next model: {next_model}")
                time.sleep(1)
                continue
            else:
                logging.error(f"🚨 All {len(model_switcher.models)} models failed")
                break

    # إعادة تعيين المؤشر
    model_switcher.current_index = start_index
    logging.error(f"🚨 Failed to translate {content_type} with all models")
    return text  # إرجاع النص الأصلي في حالة الفشل

def create_or_load_feed():
    """ينشئ أو يحمل ملف feed.xml"""
    if os.path.exists(FEED_FILE):
        try:
            tree = ET.parse(FEED_FILE)
            root = tree.getroot()
            item_count = len(root.findall('.//item'))
            logging.info(f"✅ Loaded existing feed.xml with {item_count} items")
            return root
        except Exception as e:
            logging.error(f"Failed to parse existing feed.xml: {e}")
            logging.info("Creating new feed.xml file...")
    
    # إنشاء ملف RSS جديد
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    
    # معلومات القناة الأساسية
    ET.SubElement(channel, "title").text = "ترجمة RSS"
    ET.SubElement(channel, "description").text = "منشورات مترجمة من مصادر RSS متعددة"
    ET.SubElement(channel, "link").text = "https://github.com"
    ET.SubElement(channel, "language").text = "ar"
    ET.SubElement(channel, "lastBuildDate").text = time.strftime('%a, %d %b %Y %H:%M:%S %z')
    
    logging.info("✅ Created new feed.xml structure")
    return rss

def save_feed(feed_root):
    """يحفظ ملف feed.xml"""
    try:
        # تحديث تاريخ البناء الأخير
        last_build_date = feed_root.find(".//lastBuildDate")
        if last_build_date is not None:
            last_build_date.text = time.strftime('%a, %d %b %Y %H:%M:%S %z')
        
        # تحويل XML إلى نص منظم
        rough_string = ET.tostring(feed_root, encoding='utf-8')
        reparsed = minidom.parseString(rough_string)
        pretty_xml = reparsed.toprettyxml(indent="  ", encoding='utf-8')
        
        # إزالة الأسطر الفارغة الزائدة
        pretty_xml_str = pretty_xml.decode('utf-8')
        lines = [line for line in pretty_xml_str.split('\n') if line.strip()]
        final_xml = '\n'.join(lines)
        
        with open(FEED_FILE, 'w', encoding='utf-8') as f:
            f.write(final_xml)
        
        item_count = len(feed_root.find('.//channel').findall('item'))
        logging.info(f"💾 Saved feed.xml with {item_count} items")
        return True
    except Exception as e:
        logging.error(f"❌ Failed to save feed.xml: {e}")
        return False

def add_item_to_feed(feed_root, entry, translated_title, translated_content, feed_source=None):
    """يضيف عنصراً جديداً إلى feed.xml"""
    try:
        channel = feed_root.find(".//channel")
        if channel is None:
            logging.error("Channel element not found in feed.xml")
            return False
        
        # إنشاء عنصر جديد
        item = ET.SubElement(channel, "item")
        
        # العنوان المترجم (بدون أي عبارات إضافية)
        ET.SubElement(item, "title").text = translated_title
        
        # الرابط الأصلي
        link = entry.get('link', '')
        if link:
            ET.SubElement(item, "link").text = link
        
        # المحتوى المترجم (بدون أي عبارات إضافية)
        ET.SubElement(item, "description").text = translated_content
        
        # معرف المنشور
        post_id = entry.get('id') or link
        if post_id:
            guid_elem = ET.SubElement(item, "guid")
            guid_elem.text = post_id
            guid_elem.set('isPermaLink', 'false')
        
        # التاريخ
        published = entry.get('published', entry.get('updated', ''))
        if published:
            ET.SubElement(item, "pubDate").text = published
        
        # مصدر RSS (اختياري)
        if feed_source:
            ET.SubElement(item, "source").text = feed_source
        
        logging.info(f"➕ Added item to feed.xml")
        return True
    except Exception as e:
        logging.error(f"❌ Failed to add item to feed.xml: {e}")
        return False

def get_all_entries_from_feeds(rss_urls):
    """يحصل على جميع المنشورات من مصادر RSS المختلفة"""
    all_entries = []
    
    for rss_url in rss_urls:
        try:
            logging.info(f"📥 Fetching RSS feed: {rss_url}")
            feed = feedparser.parse(rss_url)
            
            if hasattr(feed, 'entries') and feed.entries:
                logging.info(f"  Found {len(feed.entries)} entries")
                
                for entry in feed.entries:
                    entry['feed_source'] = rss_url
                    all_entries.append(entry)
            else:
                logging.warning(f"  No entries found")
                
        except Exception as e:
            logging.error(f"❌ Failed to parse RSS feed {rss_url}: {e}")
            continue
    
    # دمج وترتيب جميع المنشورات حسب التاريخ
    all_entries_sorted = sorted(
        all_entries, 
        key=lambda e: e.get('published_parsed') or e.get('updated_parsed') or (0,)
    )
    
    logging.info(f"📊 Total entries from all feeds: {len(all_entries_sorted)}")
    return all_entries_sorted

def process_post(entry, model_switcher, feed_root):
    """معالجة المنشور وترجمة العنوان والمحتوى"""
    post_id = entry.get('id') or entry.get('link')
    feed_source = entry.get('feed_source', 'Unknown Source')
    
    logging.info(f"\n🎯 Processing post from {feed_source}")
    logging.info(f"📄 Post ID: {post_id}")
    
    # 1. الحصول على العنوان الأصلي وتنظيفه
    original_title = entry.get('title', 'No Title')
    original_title_clean = clean_html(original_title)
    
    # 2. الحصول على المحتوى الأصلي وتنظيفه
    desc = entry.get('summary', '') or entry.get('description', '') or entry.get('content', '')
    original_content = clean_html(desc)
    
    # إذا كان المحتوى فارغاً، استخدم العنوان
    if not original_content or len(original_content.strip()) < 10:
        original_content = original_title_clean
    
    logging.info(f"📝 Original title: {original_title_clean[:100]}...")
    logging.info(f"📄 Original content: {original_content[:100]}...")
    
    # 3. ترجمة العنوان
    translated_title = translate_with_gemini(original_title_clean, model_switcher, "title")
    if not translated_title or translated_title == original_title_clean:
        logging.error(f"❌ Failed to translate title")
        translated_title = original_title_clean
    
    # 4. ترجمة المحتوى
    translated_content = translate_with_gemini(original_content, model_switcher, "content")
    if not translated_content or translated_content == original_content:
        logging.warning(f"⚠️ Using original content (translation failed)")
        translated_content = original_content
    
    logging.info(f"📝 Translated title: {translated_title[:100]}...")
    logging.info(f"📄 Translated content: {translated_content[:100]}...")
    
    # 5. إضافة العنصر إلى feed.xml
    success = add_item_to_feed(feed_root, entry, translated_title, translated_content, feed_source)
    if not success:
        logging.error(f"❌ Failed to add item to feed.xml")
        return False
    
    # 6. تحديث الملف بعد النجاح
    set_last_post_id(post_id)
    
    return True

def main():
    """الدالة الرئيسية"""
    logging.info("🚀 Starting RSS Translator")
    
    # إنشاء مدير النماذج
    model_switcher = GeminiModelSwitcher(GEMINI_MODELS)

    # تأكيد أن الإعدادات محمّلة قبل البدء
    if not validate_config():
        logging.error("Aborting due to invalid configuration.")
        return

    # تحميل أو إنشاء feed.xml
    feed_root = create_or_load_feed()

    # جلب جميع المنشورات من جميع مصادر RSS
    all_entries = get_all_entries_from_feeds(RSS_URLS)
    
    if not all_entries:
        logging.info("No posts found in any RSS feed.")
        return

    # جلب آخر معرف محفوظ
    last_id = get_last_post_id()
    
    # إذا لم يكن هناك آخر معرف، نعالج أحدث منشور فقط
    if not last_id:
        logging.info("No last post ID found. Processing the latest post only.")
        latest_entry = all_entries[-1]
        
        if process_post(latest_entry, model_switcher, feed_root):
            save_feed(feed_root)
            logging.info(f"🎉 Run completed successfully.")
        else:
            logging.error("❌ Failed to process post.")
        return

    # البحث عن موقع آخر منشور محفوظ في القائمة
    last_index = -1
    for i, entry in enumerate(all_entries):
        current_id = entry.get('id') or entry.get('link')
        current_id_normalized = normalize_url(current_id)

        if current_id_normalized == last_id:
            last_index = i
            break

    # إذا وجدنا آخر منشور محفوظ، نعالج كل المنشورات التي بعده
    if last_index >= 0:
        new_entries = all_entries[last_index + 1:]
        if new_entries:
            logging.info(f"Found {len(new_entries)} new posts to process")

            for entry in new_entries:
                if process_post(entry, model_switcher, feed_root):
                    save_feed(feed_root)
                    time.sleep(3)  # انتظار بين المنشورات
                else:
                    logging.error(f"🚨 Failed to process post. Stopping.")
                    break

        else:
            logging.info("No new posts found since last processed post.")
    else:
        # إذا لم نجد آخر منشور محفوظ، نعالج أحدث منشور فقط
        logging.warning("Last processed post not found. Processing latest post only.")
        latest_entry = all_entries[-1]

        if process_post(latest_entry, model_switcher, feed_root):
            save_feed(feed_root)
            logging.info(f"🎉 Run completed successfully.")
        else:
            logging.error("❌ Failed to process post.")

    logging.info("✅ RSS Translator finished!")

if __name__ == "__main__":
    main()
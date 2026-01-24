import os
import feedparser
import time
import logging
import re
import requests
import configparser
import xml.etree.ElementTree as ET
from xml.dom import minidom

def _mask(s, keep_start=4, keep_end=4):
    try:
        if not s:
            return "<empty>"
        if len(s) <= keep_start + keep_end:
            return "*" * len(s)
        return s[:keep_start] + "*" * (len(s) - keep_start - keep_end) + s[-keep_end:]
    except Exception:
        return "<hidden>"

# إعداد logging - الملفات ستحفظ في نفس المجلد الجذر
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(os.getcwd(), "rss_translator.log")),
        logging.StreamHandler()
    ]
)

# ************ تحديد المسارات في المجلد الجذر (Root) ************
# استخدام المجلد الحالي (المجلد الجذر للمستودع)
BASE_DIR = os.getcwd()  # هذا سيعطي المجلد الجذر حيث يتم تشغيل البرنامج
CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")  # في المجلد الجذر
TRACKER_FILE = os.path.join(BASE_DIR, "last_post_id.txt")  # في المجلد الجذر
FEED_FILE = os.path.join(BASE_DIR, "feed.xml")  # في المجلد الجذر
LOG_FILE = os.path.join(BASE_DIR, "rss_translator.log")  # في المجلد الجذر

logging.info(f"=== RSS Translator Started ===")
logging.info(f"Working directory (BASE_DIR): {BASE_DIR}")
logging.info(f"Config file path: {CONFIG_FILE}")
logging.info(f"Tracker file path: {TRACKER_FILE}")
logging.info(f"Feed file path: {FEED_FILE}")
logging.info(f"Log file path: {LOG_FILE}")

# قراءة config.ini
config = configparser.ConfigParser()
config.read(CONFIG_FILE, encoding='utf-8')

# قراءة الإعدادات من config.ini
GEMINI_API_KEY = config.get('credentials', 'gemini_api_key', fallback=None)

# قراءة قائمة النماذج من config.ini
GEMINI_MODELS_STR = config.get('settings', 'gemini_models', fallback='gemini-2.5-flash,gemini-2.5-flash-lite,gemini-3-flash,gemini-robotics-er-1.5-preview')
GEMINI_MODELS = [model.strip() for model in GEMINI_MODELS_STR.split(',') if model.strip()]

# قراءة قائمة روابط RSS (متعددة مفصولة بفاصلة)
RSS_URLS_STR = config.get('settings', 'rss_urls', 
    fallback='https://feed.alternativeto.net/news/all,https://www.elbilad.net/rss')
RSS_URLS = [url.strip() for url in RSS_URLS_STR.split(',') if url.strip()]

# البرومبت الثابت (لم يعد في config.ini)
TRANSLATION_PROMPT = "ترجم النص التالي إلى العربية بدقة وأمانة مع الحفاظ على المعنى:\n\n{text}"

# إعدادات Generation Config ثابتة
TRANSLATION_TEMPERATURE = 0.2  # أقل لتحقيق ترجمة أدق
TRANSLATION_TOP_P = 0.8

# تحقق تفصيلي من تحميل الإعدادات
try:
    logging.info(f"Config file exists: {os.path.exists(CONFIG_FILE)}")
    logging.info(f"GEMINI_API_KEY: {_mask(GEMINI_API_KEY)}")
    logging.info(f"RSS_URLS from config: {RSS_URLS}")
    logging.info(f"Number of RSS feeds: {len(RSS_URLS)}")
    logging.info(f"GEMINI_MODELS from config: {GEMINI_MODELS}")
    logging.info(f"TRANSLATION_PROMPT: {TRANSLATION_PROMPT[:50]}...")
    logging.info(f"Translation config: temperature={TRANSLATION_TEMPERATURE}, top_p={TRANSLATION_TOP_P}")
except Exception as _e:
    logging.error(f"Failed to log config diagnostics: {_e}")

class GeminiModelSwitcher:
    """يدير تبديل نماذج Gemini"""
    def __init__(self, models):
        self.models = models
        self.current_index = 0

    def get_current_model(self):
        """يرجع النموذج الحالي"""
        return self.models[self.current_index]

    def get_next_model(self):
        """يرجع النموذج التالي أو None إذا انتهت القائمة"""
        if self.current_index < len(self.models) - 1:
            self.current_index += 1
            return self.models[self.current_index]
        return None

    def reset(self):
        """إعادة تعيين المؤشر للنموذج الأول"""
        self.current_index = 0

def validate_config():
    """يتحقق من أن config.ini والحقول الأساسية محمّلة قبل أي استدعاءات API."""
    missing = []
    if not os.path.exists(CONFIG_FILE):
        logging.error(f"config.ini not found at: {CONFIG_FILE}")
        logging.error("Please create config.ini file in the root directory.")
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

# ************ دوال إدارة ملف التكرار ************

def normalize_url(url):
    """تطبيع الروابط - التأكد من تناسق التنسيق"""
    if not url or not isinstance(url, str):
        return ""

    # تنظيف المسافات البيضاء
    url = url.strip()

    # إزالة / متعددة في النهاية
    while url.endswith('//'):
        url = url[:-1]

    # التأكد من أن الرابط يبدأ بـ http/https
    if not url.startswith('http'):
        return url

    # إضافة / واحدة في النهاية إذا لم تكن موجودة
    if not url.endswith('/'):
        url = url + '/'

    return url

def get_last_post_id():
    """يقرأ آخر معرّف منشور تمت معالجته."""
    try:
        if not os.path.exists(TRACKER_FILE):
            logging.info("📂 Tracker file not found. Starting fresh.")
            # إنشاء ملف التتبع إذا لم يكن موجوداً
            with open(TRACKER_FILE, 'w', encoding='utf-8') as f:
                f.write("")
            return ""

        with open(TRACKER_FILE, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            normalized = normalize_url(content)
            logging.info(f"📖 Read from tracker file: '{content}' -> normalized: '{normalized}'")
            return normalized
    except Exception as e:
        logging.error(f"❌ Error reading tracker file: {e}")
        return ""

def set_last_post_id(post_id):
    """يكتب معرّف المنشور الحالي بعد معالجته بنجاح."""
    try:
        normalized_id = normalize_url(post_id)

        with open(TRACKER_FILE, 'w', encoding='utf-8') as f:
            f.write(normalized_id)

        # تحقق فوري من الكتابة
        with open(TRACKER_FILE, 'r', encoding='utf-8') as f:
            saved_content = f.read().strip()
            if saved_content == normalized_id:
                logging.info(f"💾 SUCCESS: Saved to tracker: '{normalized_id}'")
            else:
                logging.error(f"❌ FAILED: Tried to save '{normalized_id}', but file contains '{saved_content}'")

    except Exception as e:
        logging.error(f"❌ Failed to write to tracker file: {e}")
        logging.error(f"File path: {TRACKER_FILE}")

# ************ دوال Gemini والترجمة ************

def clean_html(raw_html):
    """ينظف النص من وسوم HTML."""
    return re.sub(r'<[^>]+>', '', raw_html).strip()

def translate_with_gemini(text, model_switcher):
    """يترجم النص إلى العربية باستخدام Gemini."""
    if not GEMINI_API_KEY:
        logging.error("GEMINI_API_KEY is not set.")
        return None

    if not text or len(text.strip()) < 3:
        logging.warning("Text is too short to translate.")
        return text

    # محاولة جميع النماذج حسب الترتيب في config.ini
    start_index = model_switcher.current_index
    attempted_models = 0

    while attempted_models < len(model_switcher.models):
        current_model = model_switcher.get_current_model()
        attempted_models += 1

        logging.info(f"🔧 Attempt {attempted_models}/{len(model_switcher.models)}: Using Gemini model: {current_model}")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={GEMINI_API_KEY}"

        # استخدام البرومبت الثابت مع استبدال النص
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
            r = requests.post(url, json=payload, headers=headers, timeout=30)
            r.raise_for_status()

            result = r.json()['candidates'][0]['content']['parts'][0]['text'].strip()
            logging.info(f"✅ Translation successful with model: {current_model}")
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

    # إعادة تعيين المؤشر لنقطة البداية للجلسة التالية
    model_switcher.current_index = start_index
    logging.error("🚨 Failed to translate with all available Gemini models")
    return text  # إرجاع النص الأصلي في حالة فشل الترجمة

# ************ دوال إدارة ملف feed.xml ************

def create_or_load_feed():
    """ينشئ أو يحمل ملف feed.xml الحالي"""
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
    ET.SubElement(channel, "title").text = "ترجمة RSS - من مصادر متعددة"
    ET.SubElement(channel, "description").text = "ترجمة منشورات RSS من مصادر مختلفة إلى العربية"
    ET.SubElement(channel, "link").text = "https://example.com"
    ET.SubElement(channel, "language").text = "ar"
    ET.SubElement(channel, "lastBuildDate").text = time.strftime('%a, %d %b %Y %H:%M:%S %z')
    
    logging.info("✅ Created new feed.xml structure")
    return rss

def save_feed(feed_root):
    """يحفظ ملف feed.xml بشكل منظم"""
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
        
        # العنوان المترجم
        ET.SubElement(item, "title").text = translated_title
        
        # الرابط الأصلي
        link = entry.get('link', '')
        if link:
            ET.SubElement(item, "link").text = link
        
        # المحتوى المترجم
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
            source_elem = item.find("source")
            if source_elem is not None and link:
                source_elem.set('url', link)
        
        logging.info(f"➕ Added new item to feed.xml: {translated_title[:50]}...")
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
                logging.info(f"  Found {len(feed.entries)} entries from {rss_url}")
                
                # إضافة مصدر RSS لكل منشور
                for entry in feed.entries:
                    entry['feed_source'] = rss_url
                    all_entries.append(entry)
            else:
                logging.warning(f"  No entries found in {rss_url}")
                
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
    """معالجة المنشور وترجمته وإضافته إلى feed.xml"""
    # 1. تحديد ID المنشور
    post_id = entry.get('id') or entry.get('link')
    feed_source = entry.get('feed_source', 'Unknown Source')
    
    logging.info(f"\n🎯 START Processing post from {feed_source}")
    logging.info(f"📄 Post ID: {post_id}")
    
    # 2. الحصول على العنوان الأصلي وتنظيفه
    original_title = entry.get('title', 'No Title')
    original_title_clean = clean_html(original_title)
    
    # 3. الحصول على المحتوى الأصلي وتنظيفه
    desc = entry.get('summary', '') or entry.get('description', '') or entry.get('content', '')
    original_content = clean_html(desc)
    
    logging.info(f"📝 Original title: {original_title_clean[:100]}...")
    logging.info(f"📄 Original content length: {len(original_content)} characters")
    
    # 4. ترجمة العنوان
    translated_title = translate_with_gemini(original_title_clean, model_switcher)
    if not translated_title or translated_title == original_title_clean:
        logging.error(f"❌ Failed to translate title for post {post_id}")
        return False
    
    # 5. ترجمة المحتوى (مع تقطيع إذا كان طويلاً)
    translated_content = ""
    if len(original_content) > 3000:
        logging.info("⚠️ Content is long, splitting for translation...")
        # تقطيع المحتوى إلى أجزاء
        chunks = [original_content[i:i+3000] for i in range(0, len(original_content), 3000)]
        for i, chunk in enumerate(chunks):
            logging.info(f"Translating chunk {i+1}/{len(chunks)}...")
            translated_chunk = translate_with_gemini(chunk, model_switcher)
            if translated_chunk and translated_chunk != chunk:
                translated_content += translated_chunk + " "
            else:
                translated_content += chunk + " "  # استخدام النص الأصلي إذا فشلت الترجمة
            time.sleep(1)  # انتظار بين الأجزاء
    else:
        translated_content = translate_with_gemini(original_content, model_switcher)
        if not translated_content or translated_content == original_content:
            translated_content = original_content  # استخدام النص الأصلي إذا فشلت الترجمة
    
    logging.info(f"📝 Translated title: {translated_title[:100]}...")
    logging.info(f"📄 Translated content length: {len(translated_content)} characters")
    
    # 6. إضافة العنصر إلى feed.xml
    success = add_item_to_feed(feed_root, entry, translated_title, translated_content, feed_source)
    if not success:
        logging.error(f"❌ Failed to add item to feed.xml for post {post_id}")
        return False
    
    # 7. تحديث الملف بعد النجاح
    logging.info(f"💾 SAVING ID to tracker: {post_id}")
    set_last_post_id(post_id)
    
    logging.info("----------------\n")
    return True

# ************ الدالة الرئيسية ************

def main():
    logging.info("🚀 Starting RSS Translator")
    logging.info(f"📁 Working directory: {os.getcwd()}")
    
    # التحقق من وجود الملفات في المجلد الجذر
    logging.info("🔍 Checking files in root directory:")
    logging.info(f"  config.ini: {'✅ Found' if os.path.exists(CONFIG_FILE) else '❌ Missing'}")
    logging.info(f"  last_post_id.txt: {'✅ Exists' if os.path.exists(TRACKER_FILE) else '⚠️ Will be created'}")
    logging.info(f"  feed.xml: {'✅ Exists' if os.path.exists(FEED_FILE) else '⚠️ Will be created'}")
    
    # إنشاء مدير النماذج
    model_switcher = GeminiModelSwitcher(GEMINI_MODELS)

    # تأكيد أن الإعدادات محمّلة قبل البدء
    if not validate_config():
        logging.error("Aborting run due to invalid configuration.")
        return

    # تحميل أو إنشاء feed.xml
    logging.info(f"🔍 Loading/Creating feed.xml...")
    feed_root = create_or_load_feed()

    # تحقق من الملف قبل البدء
    logging.info(f"🔍 Starting with Gemini model: {model_switcher.get_current_model()}")

    # تحقق من الملف قبل البدء
    logging.info("🔍 === CHECKING TRACKER FILE BEFORE START ===")
    last_id = get_last_post_id()
    logging.info(f"Last processed post ID from tracker: '{last_id}'")
    logging.info("============================================")

    # جلب جميع المنشورات من جميع مصادر RSS
    all_entries = get_all_entries_from_feeds(RSS_URLS)
    
    if not all_entries:
        logging.info("No posts found in any RSS feed.")
        return

    # إذا لم يكن هناك آخر معرف، نعالج أحدث منشور فقط
    if not last_id:
        logging.info("No last post ID found. Processing the latest post only.")
        latest_entry = all_entries[-1]

        if process_post(latest_entry, model_switcher, feed_root):
            # حفظ feed.xml بعد إضافة المنشور
            if save_feed(feed_root):
                logging.info(f"🎉 Run completed successfully.")
            else:
                logging.error("❌ Failed to save feed.xml")
        else:
            logging.error("❌ CRITICAL: Failed to process post. Stopping.")
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
                current_id = entry.get('id') or entry.get('link')

                if process_post(entry, model_switcher, feed_root):
                    # ✅ المعالجة نجحت - الملف تم تحديثه داخل process_post()
                    logging.info(f"✅ Post processed successfully: {current_id}")
                    
                    # حفظ feed.xml بعد كل منشور
                    save_feed(feed_root)

                    # انتظار بين المنشورات
                    time.sleep(5)
                else:
                    # فشل في الترجمة - توقف البرنامج بالكامل
                    logging.error(f"🚨 CRITICAL: Failed to process post. Stopping entire run.")
                    break

        else:
            logging.info("No new posts found since last processed post.")
    else:
        # إذا لم نجد آخر منشور محفوظ، نعالج أحدث منشور فقط
        logging.warning("Last processed post not found in current feed. Processing latest post only.")
        latest_entry = all_entries[-1]

        if process_post(latest_entry, model_switcher, feed_root):
            # حفظ feed.xml بعد إضافة المنشور
            if save_feed(feed_root):
                logging.info(f"🎉 Run completed successfully.")
            else:
                logging.error("❌ Failed to save feed.xml")
        else:
            logging.error("❌ CRITICAL: Failed to process post. Stopping.")

    # تحقق من الملف بعد النهاية
    logging.info("🔍 === CHECKING FILES AFTER PROCESSING ===")
    logging.info(f"Tracker file exists: {os.path.exists(TRACKER_FILE)}")
    if os.path.exists(TRACKER_FILE):
        with open(TRACKER_FILE, 'r', encoding='utf-8') as f:
            final_content = f.read().strip()
            logging.info(f"Final tracker content: '{final_content}'")
    
    logging.info(f"Feed file exists: {os.path.exists(FEED_FILE)}")
    if os.path.exists(FEED_FILE):
        file_size = os.path.getsize(FEED_FILE)
        logging.info(f"Feed file size: {file_size} bytes ({file_size/1024:.2f} KB)")
    
    logging.info("===============================================")
    logging.info("✅ RSS Translator finished successfully!")

if __name__ == "__main__":
    main()

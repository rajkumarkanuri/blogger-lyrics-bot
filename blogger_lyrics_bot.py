import os
import re
import json
import urllib.parse
import time
from datetime import datetime
import requests
import feedparser
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from pydantic import BaseModel
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# =====================================================================
# 1. CONFIGURATION & KEYS
# =====================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
BLOG_ID = "2717892436749933785"

# Primary high-authority feed source for new Telugu releases
MASTER_FEED = "https://www.telugulyricsworld.in/feeds/posts/default?alt=rss"

# =====================================================================
# 2. STRUCTURED SEO SCHEMA FOR GEMINI
# =====================================================================
class FAQItem(BaseModel):
    question: str
    answer: str

class SongPostData(BaseModel):
    song_name: str
    movie_name: str
    singer: str
    composer: str
    music: str
    lyricist: str
    actors: str
    director: str
    label: str
    seo_meta_description: str
    about_paragraphs: list[str]
    faqs: list[FAQItem]
    labels: list[str]

# =====================================================================
# 3. SCRAPE LYRICS & YOUTUBE VIDEO ID
# =====================================================================
def extract_lyrics_and_video(entry) -> dict:
    raw_html = ""
    if "content" in entry and len(entry.content) > 0:
        raw_html = entry.content[0].value
    elif "summary" in entry:
        raw_html = entry.summary

    if len(raw_html) < 500:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        resp = requests.get(entry.link, headers=headers, timeout=15)
        raw_html = resp.text

    soup = BeautifulSoup(raw_html, "html.parser")

    yt_id = ""
    yt_match = re.search(
        r"(?:youtube\.com/embed/|youtube\.com/watch\?v=|youtu\.be/|img\.youtube\.com/vi/)([a-zA-Z0-9_-]{11})",
        raw_html
    )
    if yt_match:
        yt_id = yt_match.group(1)

    for a_tag in soup.find_all("a"):
        a_tag.unwrap()

    telugu_blocks = []
    english_blocks = []

    for p in soup.find_all(["p", "div"]):
        if p.name == "div" and p.find(["p", "div", "table"]):
            continue

        text = p.get_text(separator="\n", strip=True)
        if not text or len(text) < 15:
            continue

        lower_text = text.lower()
        if any(skip in lower_text for skip in ["faq", "who performed", "who wrote", "who composed", "about ", "watch video", "director"]):
            continue

        inner_html = "<br />".join([line.strip() for line in text.splitlines() if line.strip()])

        if re.search(r"[\u0C00-\u0C7F]", text):
            telugu_blocks.append(f"<p>{inner_html}</p>")
        else:
            english_blocks.append(f"<p>{inner_html}</p>")

    return {
        "title": entry.title,
        # Truncate plain text sharply to save AI input tokens
        "plain_text": soup.get_text(separator="\n", strip=True)[:1200],
        "youtube_id": yt_id,
        "telugu_html": "<p><br /></p>".join(telugu_blocks),
        "english_html": "<p><br /></p>".join(english_blocks)
    }

# =====================================================================
# 4. TOKEN-EFFICIENT GEMINI SEO GENERATION
# =====================================================================
def generate_unique_seo_data(title: str, plain_text: str) -> SongPostData:
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    Extract song credits and generate structured JSON for: {title}
    Text snippet: {plain_text}
    
    Requirements:
    - Identify exact 'song_name', 'movie_name', 'singer', 'composer', 'music', 'lyricist', 'actors', 'director', 'label'.
    - 'seo_meta_description': 140-155 characters SEO description for "{title}".
    - 'about_paragraphs': Exactly 2 concise paragraphs discussing the song and movie context.
    - 'faqs': 3 concise high-intent FAQs (who sang, who wrote lyrics, who composed).
    - 'labels': 3 short category labels.
    """

    final_res = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SongPostData,
        )
    )
    return SongPostData.model_validate_json(final_res.text)

# =====================================================================
# 5. FETCH RECENT BLOG POSTS TO PREVENT DUPLICATES (LIVE API CHECK)
# =====================================================================
def get_existing_blog_titles() -> set:
    try:
        if os.path.exists("token.json"):
            creds = Credentials.from_authorized_user_file("token.json")
        else:
            creds = Credentials.from_authorized_user_info(json.loads(os.environ["BLOGGER_TOKEN_JSON"]))

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())

        service = build("blogger", "v3", credentials=creds)
        posts = service.posts().list(blogId=BLOG_ID, maxResults=100).execute()
        
        existing_titles = set()
        if "items" in posts:
            for item in posts["items"]:
                existing_titles.add(item["title"].lower().strip())
        return existing_titles
    except Exception as e:
        print(f" -> Warning fetching existing blog posts: {e}")
        return set()

# =====================================================================
# 6. YOUTUBE FALLBACK & HTML BUILDER
# =====================================================================
def find_youtube_video_id(query: str) -> str:
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://www.youtube.com/results?search_query={encoded}"
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        resp = requests.get(url, headers=headers, timeout=10)
        matches = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
        if matches:
            return matches[0]
    except Exception as e:
        print(f" -> YouTube search warning: {e}")
    return ""

def build_template_html(scraped: dict, data: SongPostData) -> str:
    s_name = data.song_name
    m_name = data.movie_name

    yt_id = scraped["youtube_id"]
    if not yt_id:
        search_query = f"{s_name} {m_name} Telugu lyrical song"
        yt_id = find_youtube_video_id(search_query)

    thumb_url = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"
    linked_song = f'<a href="{{{{POST_URL}}}}" title="{s_name} Lyrics">{s_name}</a>'

    pattern = re.compile(re.escape(s_name), re.IGNORECASE)
    english_html = pattern.sub(linked_song, scraped["english_html"], count=3)
    telugu_html = scraped["telugu_html"]
    about_html = "".join([f"<p>{p}</p>" for p in data.about_paragraphs])

    faq_html = ""
    faq_schema_list = []
    for faq in data.faqs:
        faq_html += f"<p><strong>{faq.question}</strong></p><p>{faq.answer}</p><p><br /></p>"
        faq_schema_list.append({
            "@type": "Question",
            "name": faq.question,
            "acceptedAnswer": {"@type": "Answer", "text": faq.answer}
        })

    json_ld_data = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "FAQPage", "mainEntity": faq_schema_list},
            {
                "@type": "VideoObject",
                "name": f"{s_name} Lyrical Video - {m_name}",
                "description": data.seo_meta_description,
                "thumbnailUrl": [thumb_url],
                "uploadDate": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "embedUrl": f"https://www.youtube.com/embed/{yt_id}"
            }
        ]
    }
    json_ld_script = f'<script type="application/ld+json">{json.dumps(json_ld_data, ensure_ascii=False)}</script>'

    return f"""{json_ld_script}
<div itemscope="" itemtype="http://schema.org/MusicComposition">
<meta itemprop="inLanguage" content="te" />
<p><em>{data.seo_meta_description}</em></p>
<h2 itemprop="name"><a href="{{{{POST_URL}}}}">{s_name} Lyrics - {m_name} (Telugu &amp; English)</a></h2>
<hr />
<img alt="{s_name} Song Lyrics" itemprop="image" loading="lazy" src="{thumb_url}" width="100%" />
<hr />
<h3>{s_name} Song Credits &amp; Movie Details</h3>
<table border="1" style="width:100%; border-collapse:collapse; padding:10px;">
<tbody>
<tr><td><strong>Song Name</strong></td><td><b>{s_name}</b></td></tr>
<tr><td><strong>Movie / Album</strong></td><td><b>{m_name}</b></td></tr>
<tr><td><strong>Singer</strong></td><td>{data.singer}</td></tr>
<tr><td><strong>Composer</strong></td><td><b>{data.composer}</b></td></tr>
<tr><td><strong>Lyricist</strong></td><td><b>{data.lyricist}</b></td></tr>
<tr><td><strong>Star Cast</strong></td><td>{data.actors}</td></tr>
<tr><td><strong>Director</strong></td><td>{data.director}</td></tr>
<tr><td><strong>Music Label</strong></td><td>{data.label}</td></tr>
</tbody></table>
<hr />
<div itemprop="lyrics" style="text-align: center;">
<p><strong>About {linked_song} Song:</strong></p>
{about_html}
<h2><b><a href="{{{{POST_URL}}}}">{s_name} Telugu Lyrics:</a></b></h2>
{telugu_html}
<h2><b><a href="{{{{POST_URL}}}}">{s_name} English Lyrics:</a></b></h2>
{english_html}
<h2><strong>{s_name} FAQs</strong></h2>
{faq_html}
</div>
<iframe frameborder="0" height="360" src="https://www.youtube.com/embed/{yt_id}" width="100%"></iframe>
</div>"""

# =====================================================================
# 7. NOTIFICATIONS & PUBLISHING
# =====================================================================
def notify_search_engines(post_url: str):
    try:
        parsed = urllib.parse.urlparse(post_url)
        sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
        requests.get(f"https://www.google.com/ping?sitemap={urllib.parse.quote(sitemap_url)}", timeout=5)
        requests.get(f"https://www.bing.com/ping?sitemap={urllib.parse.quote(sitemap_url)}", timeout=5)
    except Exception:
        pass

def send_telegram_notification(song_name: str, movie_name: str, post_url: str, yt_id: str):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return

    message = f"🎵 <b>New Telugu Song Lyrics Published!</b>\n\n" \
              f"✨ <b>{song_name}</b> - <i>{movie_name}</i>\n\n" \
              f"📖 Read & Watch here:\n<a href='{post_url}'>Click here to view lyrics</a>"

    thumb_url = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"
    payload = {"chat_id": chat_id, "photo": thumb_url, "caption": message, "parse_mode": "HTML"}

    try:
        resp = requests.post(f"https://api.telegram.org/bot{bot_token}/sendPhoto", json=payload, timeout=10)
        if resp.status_code != 200:
            requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception:
        pass

def publish_to_blogspot(blog_id: str, url_slug_title: str, display_title: str, raw_html: str, labels: list[str]):
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json")
    else:
        creds = Credentials.from_authorized_user_info(json.loads(os.environ["BLOGGER_TOKEN_JSON"]))

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    service = build("blogger", "v3", credentials=creds)
    created = service.posts().insert(
        blogId=blog_id,
        body={"kind": "blogger#post", "title": url_slug_title, "content": raw_html, "labels": labels},
        isDraft=False
    ).execute()

    post_id = created["id"]
    post_url = created["url"]
    parsed_url = urllib.parse.urlparse(post_url)
    blog_origin = f"{parsed_url.scheme}://{parsed_url.netloc}"

    final_html = raw_html.replace("{{POST_URL}}", post_url).replace("{{BLOG_ORIGIN}}", blog_origin)
    service.posts().patch(
        blogId=blog_id,
        postId=post_id,
        body={"title": display_title, "content": final_html}
    ).execute()

    notify_search_engines(post_url)
    return post_id, post_url

# =====================================================================
# 8. CONTINUOUS HOURLY CHECKER WORKER LOOP
# =====================================================================
if __name__ == "__main__":
    while True:
        print("Checking live Blogger API for existing posts...")
        existing_blog_titles = get_existing_blog_titles()
        print(f" -> Found {len(existing_blog_titles)} existing posts live on Blogger.")

        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        
        print(f"Checking master feed for new releases: {MASTER_FEED} ...")
        try:
            resp = requests.get(MASTER_FEED, headers=headers, timeout=15)
            feed = feedparser.parse(resp.content)
            print(f" -> Found {len(feed.entries)} total entries in feed.")
        except Exception as e:
            print(f" -> Error fetching feed: {e}")
            feed = None

        if feed:
            new_song_published = False
            for entry in feed.entries:
                clean_title = entry.title.lower().strip()
                
                # Strict check against your live blog posts (Zero duplicates)
                if clean_title in existing_blog_titles:
                    continue  # Skip already posted songs quietly

                print(f"Found brand new trending release: {entry.title}")
                scraped = extract_lyrics_and_video(entry)
                
                # Generate token-efficient SEO content
                seo_data = generate_unique_seo_data(scraped["title"], scraped["plain_text"])

                html_template = build_template_html(scraped, seo_data)
                url_slug_title = f"{seo_data.song_name} Lyrics {seo_data.movie_name} Telugu"
                display_title = f"{seo_data.song_name} Lyrics in Telugu & English - {seo_data.movie_name} | {seo_data.composer}"

                post_id, post_url = publish_to_blogspot(
                    BLOG_ID, url_slug_title, display_title, html_template, seo_data.labels
                )
                print(f"Published Trending Song! Post ID: {post_id} | Live URL: {post_url}")

                send_telegram_notification(seo_data.song_name, seo_data.movie_name, post_url, scraped['youtube_id'])

                new_song_published = True
                break  # Publish one trending song per hourly check

            if not new_song_published:
                print("No new trending songs found in this hourly check.")

        print("Waiting 1 hour before next release check...")
        time.sleep(3600)  # Checks every 1 hour (3600 seconds)

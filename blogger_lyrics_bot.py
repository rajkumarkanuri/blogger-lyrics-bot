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
# 1. PUT YOUR KEYS & CORRECT BLOG ID RIGHT HERE:
# =====================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6JGn9CFHDuIhLbjvdqR8_rs68Fv9XocaP7UnHKyR_oHEg")
BLOG_ID = "2717892436749933785"

SOURCE_FEEDS = [
    "https://www.telugulyricsworld.in/feeds/posts/default?alt=rss",
    "https://telugulyricspage.blogspot.com/feeds/posts/default?alt=rss",
]

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
# 3. SCRAPE TELUGU & ENGLISH LYRICS + YOUTUBE VIDEO ID
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
        "plain_text": soup.get_text(separator="\n", strip=True)[:6000],
        "youtube_id": yt_id,
        "telugu_html": "<p><br /></p>".join(telugu_blocks),
        "english_html": "<p><br /></p>".join(english_blocks)
    }

# =====================================================================
# 4. GEMINI SINGLE-CALL FOR SEO METADATA, ABOUT SECTION & FAQs
# =====================================================================
def generate_unique_seo_data(title: str, plain_text: str) -> SongPostData:
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
    Extract the song credits from the scraped page text below and generate SEO-optimized structured JSON for a Telugu lyrics blog post.
    
    Song Title: {title}
    Scraped Page Text:
    {plain_text[:3000]}

    Instructions:
    1. Identify exact 'song_name', 'movie_name', 'singer', 'composer', 'music', 'lyricist', 'actors', 'director', and 'label'. (Use 'TBA' only if completely unknown).
    2. 'seo_meta_description': Write a 145-155 character SEO meta description starting with "{title.split()[0]} Song Lyrics in Telugu and English from..." that naturally includes the movie name, singer, composer, and lyricist.
    3. 'about_paragraphs': Write 3 to 4 original, keyword-rich paragraphs discussing the song's musical vibe, movie context, composer's style, and vocal performances. Naturally include search phrases like "[Song Name] lyrics in Telugu", "[Song Name] English meaning/transliteration", and "[Movie Name] songs".
    4. 'faqs': Write 6 high-search-intent FAQs covering:
       - Who sang the song [Song Name] from [Movie Name]?
       - Who wrote the lyrics for [Song Name]?
       - Who is the music director/composer of [Song Name]?
       - Which actors are featured in the [Song Name] lyrical video?
       - Who directed the movie [Movie Name]?
       - Which music label released [Song Name]?
    5. 'labels': Provide 4 short Blogger category labels (each under 20 characters, e.g., Movie Name, Composer, 'Telugu Lyrics').
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
# 5. AUTO-FIND YOUTUBE ID + BUILD SEO HTML & JSON-LD SCHEMA
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
    if not yt_id or yt_id == "L8s6qxQJKgk":
        search_query = f"{s_name} {m_name} Telugu lyrical song"
        print(f" -> Searching YouTube for: {search_query} ...")
        yt_id = find_youtube_video_id(search_query)
        print(f" -> Found YouTube ID: {yt_id}")

    thumb_url = f"https://img.youtube.com/vi/{yt_id}/maxresdefault.jpg"
    linked_song = f'<a href="{{{{POST_URL}}}}" title="{s_name} Lyrics in Telugu and English">{s_name}</a>'

    pattern = re.compile(re.escape(s_name), re.IGNORECASE)
    english_html = pattern.sub(linked_song, scraped["english_html"], count=3)
    telugu_html = scraped["telugu_html"]

    about_html = "".join([f"<p>{p}</p>" for p in data.about_paragraphs])

    faq_html = ""
    faq_schema_list = []
    for faq in data.faqs:
        q_linked = pattern.sub(linked_song, faq.question)
        a_linked = pattern.sub(linked_song, faq.answer)
        faq_html += f"<p><strong>{q_linked}</strong></p><p>{a_linked}</p><p><strong><br /></strong></p>"
        faq_schema_list.append({
            "@type": "Question",
            "name": faq.question,
            "acceptedAnswer": {
                "@type": "Answer",
                "text": faq.answer
            }
        })

    current_upload_date = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    json_ld_data = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "FAQPage",
                "mainEntity": faq_schema_list
            },
            {
                "@type": "VideoObject",
                "name": f"{s_name} Lyrical Video - {m_name}",
                "description": data.seo_meta_description,
                "thumbnailUrl": [thumb_url],
                "uploadDate": current_upload_date,
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
<img alt="{s_name} Song Lyrics in Telugu and English - {m_name}" title="{s_name} Lyrical - {m_name}" itemprop="image" loading="lazy" src="{thumb_url}" width="100%" />
<hr />
<style>
    table{{
        border-spacing: 0px;
        border: solid 1px black;
        padding:10px;
        width:100%;
    }}
    tr,td{{
        border-spacing: 0px;
        border: solid 1px black;
        padding:10px;
    }}
</style>
<h3>{s_name} Song Credits &amp; Movie Details</h3>
<table>
<tbody>
<tr><td><strong>Song Name</strong></td><td><b>{s_name}</b></td></tr>
<tr><td><strong>Movie / Album</strong></td><td><b>{m_name}</b></td></tr>
<tr><td><strong>Singer</strong></td><td>{data.singer}</td></tr>
<tr><td><strong>Composer</strong></td><td itemprop="composer" itemscope="" itemtype="http://schema.org/Person"><b itemprop="name">{data.composer}</b></td></tr>
<tr><td><strong>Music</strong></td><td><b>{data.music}</b></td></tr>
<tr><td><strong>Song Writer (Lyricist)</strong></td><td itemprop="lyricist" itemscope="" itemtype="http://schema.org/Person"><b itemprop="name">{data.lyricist}</b></td></tr>
<tr><td><strong>Star Cast</strong></td><td>{data.actors}</td></tr>
<tr><td><strong>Director</strong></td><td>{data.director}</td></tr>
<tr><td><strong>Music Label</strong></td><td>{data.label}</td></tr>
</tbody></table>
<hr />
<div itemprop="lyrics" itemscope="" itemtype="http://schema.org/CreativeWork" style="text-align: center;">
<p><strong>About {linked_song} Song:</strong></p><p><br /></p>
{about_html}
<p><b><br /></b></p>
<h2 id="telugu-lyrics"><b><a href="{{{{POST_URL}}}}">{s_name} Song Lyrics in Telugu Script:</a></b></h2><p><br /></p>
<div itemprop="text">
{telugu_html}
<p><br /></p><p><br /></p>
<h2 id="english-lyrics"><b><a href="{{{{POST_URL}}}}">{s_name} Song Lyrics in English (Transliteration):</a></b></h2>
{english_html}
</div>
<p>&nbsp;</p><p><strong><br /></strong></p>
<h2 id="song-faqs"><strong>{linked_song}&nbsp;Song FAQs</strong></h2><p><strong><br /></strong></p>
{faq_html}
</div>
<br />
<h2>{s_name} Lyrical - {m_name} Official Video</h2>
<iframe frameborder="0" height="360" loading="lazy" marginheight="0" marginwidth="0" scrolling="no" src="https://www.youtube.com/embed/{yt_id}?autoplay=0&amp;origin={{{{BLOG_ORIGIN}}}}" title="{s_name} YouTube Lyrical Video" type="text/html" width="100%"></iframe>
</div>"""

# =====================================================================
# 6. AUTO-PING GOOGLE & BING SITEMAPS UPON PUBLISH
# =====================================================================
def notify_search_engines(post_url: str):
    try:
        parsed = urllib.parse.urlparse(post_url)
        sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
        
        requests.get(f"https://www.google.com/ping?sitemap={urllib.parse.quote(sitemap_url)}", timeout=5)
        requests.get(f"https://www.bing.com/ping?sitemap={urllib.parse.quote(sitemap_url)}", timeout=5)
        print(" -> Successfully notified Google and Bing to crawl the sitemap!")
    except Exception as e:
        print(f" -> Sitemap ping warning: {e}")

# =====================================================================
# 7. TELEGRAM AUTO-BROADCASTER
# =====================================================================
def send_telegram_notification(song_name: str, movie_name: str, post_url: str, thumb_url: str):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    if not bot_token or not chat_id:
        print(" -> Telegram credentials not found. Skipping notification.")
        return

    message = f"🎵 *New Telugu Song Lyrics Published!*\n\n" \
              f"✨ *{song_name}* - *{movie_name}*\n\n" \
              f"📖 Read Telugu & English Lyrics, FAQs, and watch the official video here:\n{post_url}"

    payload = {
        "chat_id": chat_id,
        "photo": thumb_url,
        "caption": message,
        "parse_mode": "Markdown"
    }

    try:
        resp = requests.post(f"https://api.telegram.org/bot{bot_token}/sendPhoto", json=payload, timeout=10)
        if resp.status_code == 200:
            print(" -> Successfully sent Telegram notification!")
        else:
            print(f" -> Telegram error: {resp.text}")
    except Exception as e:
        print(f" -> Telegram warning: {e}")

# =====================================================================
# 8. TWO-STEP SEO PUBLISH & INDEX TRIGGER
# =====================================================================
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
# 9. CONTINUOUS WORKER LOOP FOR RAILWAY (RUNS 24/7)
# =====================================================================
if __name__ == "__main__":
    while True:
        history_file = "posted_links.txt"
        posted_links = set(open(history_file).read().splitlines()) if os.path.exists(history_file) else set()

        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        new_song_found = False

        for feed_url in SOURCE_FEEDS:
            print(f"Checking feed: {feed_url} ...")
            try:
                resp = requests.get(feed_url, headers=headers, timeout=15)
                feed = feedparser.parse(resp.content)
                print(f" -> Found {len(feed.entries)} songs in this feed.")
            except Exception as e:
                print(f" -> Error fetching feed: {e}")
                continue

            for entry in feed.entries:
                if entry.link not in posted_links:
                    print(f"Found new song post: {entry.title}")
                    scraped = extract_lyrics_and_video(entry)
                    seo_data = generate_unique_seo_data(scraped["title"], scraped["plain_text"])

                    html_template = build_template_html(scraped, seo_data)

                    url_slug_title = f"{seo_data.song_name} Lyrics {seo_data.movie_name} Telugu"
                    display_title = f"{seo_data.song_name} Lyrics in Telugu & English - {seo_data.movie_name} | {seo_data.composer}"

                    post_id, post_url = publish_to_blogspot(
                        BLOG_ID, url_slug_title, display_title, html_template, seo_data.labels
                    )
                    print(f"Published SEO Post! Post ID: {post_id} | Live URL: {post_url}")

                    # Trigger Telegram Broadcast Notification
                    thumb_url = f"https://img.youtube.com/vi/{scraped['youtube_id']}/maxresdefault.jpg"
                    send_telegram_notification(seo_data.song_name, seo_data.movie_name, post_url, thumb_url)

                    with open(history_file, "a", encoding="utf-8") as f:
                        f.write(entry.link + "\n")
                    
                    new_song_found = True
                    break
            if new_song_found:
                break

        if not new_song_found:
            print("No new unposted songs found in the feeds.")

        print("Waiting 12 hours before next feed check...")
        time.sleep(43200)  # Sleeps for 12 hours (43200 seconds)
# Combined Google Ads Transparency scraper
# Extracts video ads, image ads, and text ads in one run.
# Preserves the working video/text logic and uses active-frame locking for image ads.

from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs, unquote, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import difflib
import re
import time
import threading
import sheets


MAX_WORKERS = 2
SHEET_LOCK = threading.Lock()

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".m3u8")

INSTALL_SELECTORS = [
    "a.install-button-anchor.svg-anchor",
    "a.install-button-anchor",
    'a[data-asoch-targets-ad-objective-type]',
    'a:has-text("Install")',
    'a:has-text("Get")',
    'a:has-text("Download")',
]


def safe_update_scrape_result(
    row_num,
    combined_data,
    headline,
    description,
    image_url,
):
    """Write A:G and M:O atomically through the shared Sheets retry layer."""
    with SHEET_LOCK:
        sheets.update_scrape_result(
            row_index=row_num,
            combined_data=combined_data,
            headline=headline,
            description=description,
            image_url=image_url,
        )


def safe_add_log(row_number, status, log_type, url="", video_id="", app_link="", message=""):
    """
    Thread-safe log writing.
    """
    with SHEET_LOCK:
        sheets.add_log(
            row_number=row_number,
            status=status,
            log_type=log_type,
            url=url,
            video_id=video_id,
            app_link=app_link,
            message=message
        )


def get_exact_time():
    return datetime.now().strftime("%I:%M:%S %p")


def clean_text(value):
    if not value:
        return "N/A"
    return re.sub(r"\s+", " ", str(value)).strip() or "N/A"


def extract_package_name(app_link):
    """
    Extracts package name from app store link.
    For Google Play: extracts the 'id' parameter
    For App Store: extracts app ID from URL
    """
    if not app_link or app_link == "N/A":
        return "N/A"
    
    try:
        # Google Play Store format: ...?id=com.example.app
        if "play.google.com" in app_link.lower():
            parsed = urlparse(app_link)
            query = parse_qs(parsed.query)
            package_name = query.get("id", [None])[0]
            if package_name:
                return package_name
        
        # Apple App Store format: ...app/app-name/id123456789
        if "apps.apple.com" in app_link.lower():
            # Extract the ID from the URL path
            match = re.search(r"/id(\d+)", app_link)
            if match:
                return f"id{match.group(1)}"
        
        # If we can't extract, return N/A
        return "N/A"
    
    except Exception:
        return "N/A"


# =========================
# VIDEO ID LOGIC (REVERTED TO YOUR ORIGINAL WORKING LOGIC)
# =========================

def is_real_video_response(response):
    try:
        url = response.url.lower()
        headers = response.headers
        content_type = headers.get("content-type", "").lower()

        if content_type.startswith("video/"):
            return True

        if "application/vnd.apple.mpegurl" in content_type:
            return True

        if "application/x-mpegurl" in content_type:
            return True

        if "videoplayback" in url:
            return True

        if any(ext in url for ext in VIDEO_EXTENSIONS):
            return True

    except Exception:
        pass

    return False


def extract_video_id_from_url(req_url):
    """
    Extracts only clean video IDs or filenames.
    Does NOT return full video links.
    """
    try:
        url_lower = req_url.lower()
        parsed = urlparse(req_url)
        query = parse_qs(parsed.query)

        if "videoplayback" in url_lower:
            video_id = query.get("id", [None])[0]

            if video_id:
                return video_id

            for key in ["itag", "ei", "source"]:
                value = query.get(key, [None])[0]
                if value:
                    return value

            return None

        for ext in VIDEO_EXTENSIONS:
            if ext in url_lower:
                filename = parsed.path.split("/")[-1]
                filename = filename.split("?")[0].strip()

                if filename:
                    return filename

        if "youtube.com/embed/" in url_lower:
            return req_url.split("youtube.com/embed/")[1].split("?")[0].split("&")[0]

        if "youtube.com/watch" in url_lower:
            return query.get("v", [None])[0]

        if "youtu.be/" in url_lower:
            return req_url.split("youtu.be/")[1].split("?")[0].split("&")[0]

    except Exception:
        return None

    return None


def extract_video_from_dom(page):
    """
    Checks actual video elements on page and inside frames.
    """
    try:
        video_sources = page.evaluate("""
            () => Array.from(document.querySelectorAll('video'))
                .map(v => v.currentSrc || v.src || '')
                .filter(Boolean)
        """)

        for src in video_sources:
            video_id = extract_video_id_from_url(src)
            if video_id:
                return video_id

    except Exception:
        pass

    for frame in page.frames:
        try:
            video_sources = frame.evaluate("""
                () => Array.from(document.querySelectorAll('video'))
                    .map(v => v.currentSrc || v.src || '')
                    .filter(Boolean)
            """)

            for src in video_sources:
                video_id = extract_video_id_from_url(src)
                if video_id:
                    return video_id

        except Exception:
            continue

    return "N/A"


def scan_browser_performance_for_video(page):
    """
    Scans performance entries for real video URLs only.
    """
    try:
        urls = page.evaluate("""
            () => performance.getEntriesByType('resource').map(r => r.name)
        """)

        for u in urls:
            u_lower = u.lower()

            if (
                "videoplayback" in u_lower
                or ".mp4" in u_lower
                or ".webm" in u_lower
                or ".mov" in u_lower
                or ".m4v" in u_lower
                or ".m3u8" in u_lower
                or "youtube.com/embed/" in u_lower
                or "youtube.com/watch" in u_lower
                or "youtu.be/" in u_lower
            ):
                video_id = extract_video_id_from_url(u)

                if video_id:
                    return video_id

    except Exception:
        pass

    return "N/A"


def click_possible_video_targets(page):
    """
    Clicks possible video preview areas.
    Avoids install buttons/app links.
    """
    selectors = [
        "video",
        "iframe",
        "creative-preview",
        'button[aria-label*="Play"]',
        'button[title*="Play"]',
        'div[aria-label*="Play"]',
        'img[src*="play"]'
    ]

    for sel in selectors:
        try:
            elements = page.locator(sel)
            count = elements.count()

            for i in range(count):
                el = elements.nth(i)

                if not el.is_visible():
                    continue

                try:
                    el.scroll_into_view_if_needed(timeout=2000)
                    box = el.bounding_box()

                    if not box:
                        continue

                    if box["width"] < 120 or box["height"] < 80:
                        continue

                    x = box["x"] + box["width"] / 2
                    y = box["y"] + box["height"] / 2

                    page.mouse.click(x, y)
                    page.wait_for_timeout(1500)
                    return True

                except Exception:
                    continue

        except Exception:
            continue

    return False


def wait_for_video_id(page, captured, max_seconds=20):
    waited = 0

    while waited < max_seconds:
        if captured.get("video_id") and captured["video_id"] != "N/A":
            return captured["video_id"]

        dom_video_id = extract_video_from_dom(page)
        if dom_video_id != "N/A":
            return dom_video_id

        page.wait_for_timeout(500)
        waited += 0.5

    return "N/A"


def detect_video_id(page, captured):
    """
    Main video detection flow.
    """
    video_id = extract_video_from_dom(page)

    if video_id == "N/A":
        click_possible_video_targets(page)
        video_id = wait_for_video_id(page, captured, max_seconds=15)

    if video_id == "N/A":
        video_id = scan_browser_performance_for_video(page)

    if video_id == "N/A":
        page.mouse.wheel(0, 400)
        page.wait_for_timeout(1500)

        click_possible_video_targets(page)
        video_id = wait_for_video_id(page, captured, max_seconds=10)

    return video_id


# =========================
# APP LINK LOGIC
# =========================

def clean_googleadservices_link(href):
    if not href:
        return "N/A"

    href = href.strip()

    if href.startswith("//"):
        href = "https:" + href

    try:
        parsed = urlparse(href)
        query = parse_qs(parsed.query)

        possible_keys = [
            "adurl",
            "url",
            "q",
            "u",
            "ds_dest_url",
            "destination",
        ]

        for key in possible_keys:
            value = query.get(key, [None])[0]
            if value:
                return unquote(value)

    except Exception:
        pass

    return href


def is_good_app_link(href):
    if not href:
        return False

    href = href.lower()

    return (
        "googleadservices.com/pagead/aclk" in href
        or "play.google.com" in href
        or "apps.apple.com" in href
        or "itunes.apple.com" in href
    )


def get_visible_install_candidates_from_target(target):
    candidates = []

    for selector in INSTALL_SELECTORS:
        try:
            loc = target.locator(selector)
            count = loc.count()

            for i in range(count):
                try:
                    el = loc.nth(i)

                    href = el.get_attribute("href", timeout=1500)
                    data_href = el.get_attribute("data-href", timeout=1000)

                    final_href = href or data_href

                    if not final_href or not is_good_app_link(final_href):
                        continue

                    box = el.bounding_box(timeout=1500)

                    if not box:
                        continue

                    if box["width"] < 20 or box["height"] < 10:
                        continue

                    text = ""
                    try:
                        text = el.inner_text(timeout=1000).strip().lower()
                    except Exception:
                        pass

                    score = 0

                    try:
                        class_name = el.get_attribute("class", timeout=1000) or ""
                        if "install-button-anchor" in class_name:
                            score += 100
                    except Exception:
                        pass

                    if "install" in text:
                        score += 80
                    elif "get" in text or "download" in text:
                        score += 40

                    center_x = box["x"] + box["width"] / 2
                    center_y = box["y"] + box["height"] / 2

                    if 350 <= center_x <= 850:
                        score += 40

                    if 50 <= center_y <= 700:
                        score += 40

                    if center_y > 700:
                        score -= 100

                    candidates.append({
                        "href": final_href,
                        "score": score,
                        "box": box,
                        "text": text,
                    })

                except Exception:
                    continue

        except Exception:
            continue

    return candidates


def extract_visible_install_link(page):
    """
    Extracts only the visible install button from the active creative.
    Does not scan random adservice links.
    """
    all_candidates = []

    try:
        all_candidates.extend(get_visible_install_candidates_from_target(page))
    except Exception:
        pass

    for frame in page.frames:
        try:
            all_candidates.extend(get_visible_install_candidates_from_target(frame))
        except Exception:
            continue

    if not all_candidates:
        return "N/A"

    all_candidates.sort(key=lambda x: x["score"], reverse=True)

    best = all_candidates[0]

    if best["score"] <= 0:
        return "N/A"

    return clean_googleadservices_link(best["href"])


def extract_install_link_by_precise_js(page):
    """
    Strict JS fallback:
    only install-button-anchor / Install text links,
    not every googleadservices link.
    """
    js = r"""
    () => {
        const anchors = Array.from(document.querySelectorAll('a[href], a[data-href]'));
        const candidates = anchors.map(a => {
            const href = a.href || a.getAttribute('href') || a.getAttribute('data-href') || '';
            const text = (a.innerText || a.textContent || '').trim().toLowerCase();
            const cls = String(a.className || '').toLowerCase();
            const aria = String(a.getAttribute('aria-label') || '').toLowerCase();
            const rect = a.getBoundingClientRect();

            const goodLink =
                href.includes('googleadservices.com/pagead/aclk') ||
                href.includes('play.google.com') ||
                href.includes('apps.apple.com') ||
                href.includes('itunes.apple.com');

            const looksInstall =
                cls.includes('install-button-anchor') ||
                text.includes('install') ||
                text.includes('get') ||
                text.includes('download') ||
                aria.includes('install');

            const visible =
                rect.width > 20 &&
                rect.height > 10 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                rect.top < window.innerHeight &&
                rect.left < window.innerWidth;

            if (!goodLink || !looksInstall || !visible) {
                return null;
            }

            let score = 0;
            if (cls.includes('install-button-anchor')) score += 100;
            if (text.includes('install')) score += 80;
            if (text.includes('get') || text.includes('download')) score += 40;
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            if (cx >= 350 && cx <= 850) score += 40;
            if (cy >= 50 && cy <= 700) score += 40;
            if (cy > 700) score -= 100;
            return {
                href,
                score
            };
        }).filter(Boolean);

        candidates.sort((a, b) => b.score - a.score);

        return candidates.length ? candidates[0].href : null;
    }
    """

    try:
        href = page.evaluate(js)
        if href and is_good_app_link(href):
            return clean_googleadservices_link(href)
    except Exception:
        pass

    for frame in page.frames:
        try:
            href = frame.evaluate(js)
            if href and is_good_app_link(href):
                return clean_googleadservices_link(href)
        except Exception:
            continue

    return "N/A"


def wait_and_extract_install_link(page, max_wait_seconds=35):
    start = time.time()

    while time.time() - start < max_wait_seconds:
        app_link = extract_visible_install_link(page)

        if app_link != "N/A":
            return app_link

        app_link = extract_install_link_by_precise_js(page)

        if app_link != "N/A":
            return app_link

        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

        page.wait_for_timeout(1500)

    return "N/A"


# =========================
# IMAGE URL EXTRACTION LOGIC
# =========================

def _normalize_image_url(raw_url, base_url=""):
    """Normalize image URLs without changing your existing image-picking behavior."""
    if not raw_url:
        return "N/A"

    raw_url = str(raw_url).strip().strip('"\'')
    if not raw_url or raw_url.lower() in {"none", "null", "undefined"}:
        return "N/A"

    if raw_url.lower().startswith("data:image"):
        return "N/A"

    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url

    if base_url and not raw_url.lower().startswith(("http://", "https://", "blob:")):
        try:
            raw_url = urljoin(base_url, raw_url)
        except Exception:
            pass

    return raw_url or "N/A"


def extract_primary_image_data_from_target(target):
    """
    Same image-selection idea as your working image extractor, but also returns
    the image position and the exact Playwright target so text can be extracted
    from the SAME creative instead of another iframe/page.
    """
    js = r"""
    () => {
        const absUrl = (raw) => {
            if (!raw) return '';
            raw = String(raw).trim().replace(/^['"]|['"]$/g, '');
            if (!raw || raw === 'none') return '';
            if (raw.startsWith('data:image')) return '';
            try { return new URL(raw, location.href).href; } catch (e) { return raw; }
        };

        const pickBestFromSrcset = (srcset) => {
            if (!srcset) return '';
            let bestUrl = '';
            let bestScore = -1;
            for (const rawPart of String(srcset).split(',')) {
                const part = rawPart.trim();
                if (!part) continue;
                const pieces = part.split(/\s+/).filter(Boolean);
                const url = pieces[0] || '';
                const descriptor = pieces[1] || '';
                let score = 1;
                if (descriptor.endsWith('w')) score = parseFloat(descriptor) || 1;
                if (descriptor.endsWith('x')) score = (parseFloat(descriptor) || 1) * 1000;
                if (score >= bestScore) {
                    bestScore = score;
                    bestUrl = url;
                }
            }
            return bestUrl;
        };

        const isVisibleBox = (el, minW = 100, minH = 100) => {
            if (!el) return null;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            if (rect.width < minW || rect.height < minH) return null;
            if (rect.bottom <= 0 || rect.right <= 0 || rect.top >= window.innerHeight || rect.left >= window.innerWidth) return null;
            if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') return null;
            return rect;
        };

        const badImage = (url, el) => {
            const lower = String(url || '').toLowerCase();
            const alt = String(el?.getAttribute?.('alt') || '').toLowerCase();
            if (!lower) return true;
            if (lower.startsWith('data:image')) return true;
            if (lower.includes('googlelogo') || alt.includes('google')) return true;
            if (lower.includes('/branding/') && lower.includes('google')) return true;
            if (lower.includes('favicon') || lower.endsWith('/favicon.ico')) return true;
            if (lower.includes('doubleclick') && lower.includes('adchoices')) return true;
            // Skip small logos/icons. Your main image extraction was correct, so keep this conservative.
            if (alt.includes('logo') || alt.includes('icon') || lower.includes('logo') || lower.includes('icon')) return true;
            return false;
        };

        const candidates = [];

        const addCandidate = (rawUrl, el, kind, bonus = 0) => {
            const url = absUrl(rawUrl);
            if (!url || badImage(url, el)) return;
            const rect = isVisibleBox(el);
            if (!rect) return;

            let score = rect.width * rect.height;
            score += bonus;
            if (rect.top < 400) score += 5000;
            if (kind.includes('currentSrc')) score += 5000;
            if (kind.includes('srcset')) score += 3000;
            if (kind.includes('background')) score += 2000;
            if (url.startsWith('blob:')) score -= 50000;

            candidates.push({
                url,
                kind,
                score,
                top: rect.top,
                bottom: rect.bottom,
                left: rect.left,
                right: rect.right,
                width: rect.width,
                height: rect.height
            });
        };

        for (const img of Array.from(document.querySelectorAll('img'))) {
            addCandidate(img.currentSrc, img, 'img-currentSrc', 5000);
            addCandidate(img.getAttribute('src'), img, 'img-src', 4000);
            addCandidate(pickBestFromSrcset(img.getAttribute('srcset')), img, 'img-srcset', 4500);

            for (const attr of ['data-src', 'data-lazy-src', 'data-original', 'data-image', 'data-image-url', 'data-thumbnail-url', 'data-iurl']) {
                addCandidate(img.getAttribute(attr), img, `img-${attr}`, 2000);
            }
        }

        for (const source of Array.from(document.querySelectorAll('picture source[srcset], source[srcset]'))) {
            const picture = source.closest('picture');
            const visualEl = picture?.querySelector('img') || picture || source;
            addCandidate(pickBestFromSrcset(source.getAttribute('srcset')), visualEl, 'source-srcset', 3500);
        }

        for (const svgImage of Array.from(document.querySelectorAll('image'))) {
            addCandidate(svgImage.getAttribute('href') || svgImage.getAttribute('xlink:href'), svgImage, 'svg-image', 2500);
        }

        for (const el of Array.from(document.querySelectorAll('body *'))) {
            const rect = isVisibleBox(el, 120, 80);
            if (!rect) continue;
            const bg = window.getComputedStyle(el).backgroundImage || '';
            if (!bg || bg === 'none' || !bg.includes('url(')) continue;

            const matches = Array.from(bg.matchAll(/url\((['"]?)(.*?)\1\)/g));
            for (const match of matches) {
                addCandidate(match[2], el, 'background-image', 2500);
            }
        }

        if (!candidates.length) return null;

        const deduped = [];
        const seen = new Set();
        for (const c of candidates) {
            if (seen.has(c.url)) continue;
            seen.add(c.url);
            deduped.push(c);
        }

        deduped.sort((a, b) => b.score - a.score);
        return deduped[0] || null;
    }
    """
    try:
        data = target.evaluate(js)
        if not data:
            return None

        base_url = getattr(target, "url", "") or ""
        data["url"] = _normalize_image_url(data.get("url"), base_url=base_url)
        if data["url"] == "N/A":
            return None
        return data
    except Exception:
        return None


def wait_and_extract_image_url_with_target(page, max_wait_seconds=12):
    """
    Returns (image_url, target, image_box).
    Important: target is the same page/frame where the correct image was found.
    """
    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        # Keep the same behavior as your working image extractor: main page first, then frames.
        image_data = extract_primary_image_data_from_target(page)
        if image_data:
            return image_data.get("url", "N/A"), page, image_data

        for frame in page.frames:
            if frame == page.main_frame:
                continue
            image_data = extract_primary_image_data_from_target(frame)
            if image_data:
                return image_data.get("url", "N/A"), frame, image_data

        page.wait_for_timeout(1000)

    return "N/A", None, None




# =========================
# HEADLINE AND DESCRIPTION LOGIC
# =========================

def wait_and_extract_headline_description(page, max_wait_seconds=15):
    """
    Polls for Headline and Description inside iframes ONLY.
    Uses structural class patterns (-e-15, -e-67) and visibility checks 
    to avoid grabbing hidden template text.
    """
    js = r"""
    () => {
        let headText = "N/A";
        let descText = "N/A";

        // Helper to ensure we don't grab hidden/template elements
        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
        };

        // SEARCH HEADLINE: Matches any class containing '-e-15' OR 'headline'
        const headNodes = document.querySelectorAll('[class*="-e-15"], [class*="headline"]');
        for (let el of headNodes) {
            if (isVisible(el)) {
                let text = (el.innerText || el.textContent || "").replace(/\n/g, ' ').trim();
                // Ensure it's not a template placeholder like {{headline}}
                if (text.length > 1 && !text.includes('{{')) { 
                    headText = text; 
                    break; 
                }
            }
        }

        // SEARCH DESCRIPTION: Matches any class containing '-e-67' OR 'long-description'
        const descNodes = document.querySelectorAll('[class*="-e-67"], [class*="long-description"]');
        for (let el of descNodes) {
            if (isVisible(el)) {
                let text = (el.innerText || el.textContent || "").replace(/\n/g, ' ').trim();
                if (text.length > 1 && text !== headText && !text.includes('{{')) { 
                    descText = text; 
                    break; 
                }
            }
        }

        // If we found either one, return it
        if (headText !== "N/A" || descText !== "N/A") {
            return { headline: headText, description: descText };
        }

        return null;
    }
    """

    start = time.time()
    
    # Retry loop: Keeps trying for up to max_wait_seconds (15s)
    while time.time() - start < max_wait_seconds:
        
        # STRICTLY CHECK IFRAMES ONLY.
        for frame in page.frames:
            try:
                result = frame.evaluate(js)
                if result and (result.get("headline", "N/A") != "N/A" or result.get("description", "N/A") != "N/A"):
                    return result.get("headline", "N/A"), result.get("description", "N/A")
            except Exception:
                continue
        
        # Wait 1 second and loop again to let the ad iframe fully load
        page.wait_for_timeout(1000)

    # If the timer runs out, return N/A
    return "N/A", "N/A"

# =========================
# STRICT TEXT-AD PACKAGE MATCHER
# =========================

MIN_PACKAGE_MATCH_SCORE = 0.76

_GENERIC_PACKAGE_TOKENS = {
    "com", "net", "org", "co", "io", "app", "apps", "android", "mobile",
    "google", "play", "store", "free", "pro", "lite", "online", "official",
    "inc", "ltd", "llc", "studio", "studios", "company", "group", "digital",
    "ai", "all", "new", "best", "easy", "fast"
}


def clean_text_for_comparison(text):
    """Lowercase and remove punctuation/spaces for ad text vs package comparison."""
    if not text or text == "N/A":
        return ""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def split_words_for_comparison(text):
    if not text or text == "N/A":
        return []
    return re.findall(r"[a-z0-9]+", str(text).lower())


def package_tokens_for_matching(pkg):
    """Turn com.example.musicplayer into useful tokens like example/musicplayer."""
    if not pkg:
        return []

    raw_tokens = re.split(r"[._-]+", pkg.lower())
    tokens = []

    for token in raw_tokens:
        token = re.sub(r"[^a-z0-9]", "", token)
        if not token or token in _GENERIC_PACKAGE_TOKENS:
            continue
        if len(token) < 3 or token.isdigit():
            continue
        tokens.append(token)

    return tokens


def score_package_against_text(pkg, headline, description):
    """
    STRICT score for non-video ads: compare package ONLY with visible headline + description.
    This prevents image ads from using random hidden package names from the page HTML.
    """
    visible_raw = f"{headline or ''} {description or ''}"
    visible_clean = clean_text_for_comparison(visible_raw)
    visible_words = split_words_for_comparison(visible_raw)
    visible_word_set = set(visible_words)

    if not visible_clean or not visible_words:
        return 0.0

    tokens = package_tokens_for_matching(pkg)
    if not tokens:
        return 0.0

    package_core = "".join(tokens)
    score = 0.0

    # Very strong signal: useful package core appears directly in visible ad text.
    if package_core and len(package_core) >= 6 and package_core in visible_clean:
        score = max(score, 0.98)

    # Direct token hits only. Generic tokens were already removed by package_tokens_for_matching().
    exact_hits = []
    partial_hits = []

    for token in tokens:
        if token in visible_word_set:
            exact_hits.append(token)
            continue

        # Allow long tokens like musicplayer/pdfreader to match joined visible text.
        if len(token) >= 6 and token in visible_clean:
            exact_hits.append(token)
            continue

        for word in visible_words:
            if len(token) >= 5 and len(word) >= 5 and (token in word or word in token):
                partial_hits.append(token)
                break

    exact_hits = list(dict.fromkeys(exact_hits))
    partial_hits = list(dict.fromkeys(partial_hits))
    total_hits = len(set(exact_hits + partial_hits))

    # One weak/fuzzy word is NOT enough now. This is the main image-ad false-match fix.
    if len(exact_hits) >= 2:
        score = max(score, 0.92)
    elif len(exact_hits) == 1 and len(exact_hits[0]) >= 8:
        score = max(score, 0.78)
    elif total_hits >= 2:
        score = max(score, 0.76)

    # Fuzzy matching can only boost when the whole package core is extremely close.
    # It cannot pass alone on one random similar word.
    if package_core and len(package_core) >= 8:
        core_ratio = difflib.SequenceMatcher(None, visible_clean, package_core).ratio()
        if core_ratio >= 0.88:
            score = max(score, 0.82)

    return round(score, 4)


def get_best_matching_package(headline, description, package_list, min_score=MIN_PACKAGE_MATCH_SCORE):
    """
    Compare headline + description with every found package.
    Returns (package, score). If no package score is at least 0.76, returns (None, best_score).
    """
    if not package_list:
        return None, 0.0

    best_pkg = None
    best_score = 0.0

    for pkg in sorted(package_list):
        score = score_package_against_text(pkg, headline, description)
        if score > best_score:
            best_score = score
            best_pkg = pkg

    if best_pkg and best_score >= min_score:
        return best_pkg, best_score

    return None, best_score

def decode_all(text):
    """Decode every encoding variant so no package name is missed."""
    text = re.sub(r'\\x3[Dd]', '=', text)
    text = re.sub(r'\\x26',    '&', text)
    text = re.sub(r'\\x3[Ff]', '?', text)
    text = re.sub(r'\\x2[Ff]', '/', text)
    text = re.sub(r'\\u003[Dd]', '=', text)
    text = re.sub(r'\\u0026',    '&', text)
    text = re.sub(r'\\u003[Ff]', '?', text)
    text = re.sub(r'%3[Dd]', '=', text, flags=re.I)
    text = re.sub(r'%26',    '&', text, flags=re.I)
    text = re.sub(r'%3[Ff]', '?', text, flags=re.I)
    text = re.sub(r'%2[Ff]', '/', text, flags=re.I)
    text = re.sub(r'%3[Aa]', ':', text, flags=re.I)
    text = (text.replace('&amp;', '&').replace('&quot;', '"')
                .replace('&#38;', '&').replace('&#61;', '=')
                .replace('&#x3D;', '=').replace('&#x26;', '&'))
    return text


_SKIP_EXT = re.compile(
    r'\.(jpg|jpeg|png|gif|webp|svg|ico|css|js|json|xml|html|htm|'
    r'woff|woff2|ttf|otf|eot|pdf|zip|apk|mp4|mp3|ogg|m3u8)$', re.I)
_SKIP_PFX = re.compile(
    r'^(com\.google\.android\.(gms|vending|inputmethod|tts|webview)|'
    r'com\.android\.|android\.|androidx\.|kotlin\.|kotlinx\.|'
    r'com\.squareup\.|io\.reactivex\.|okhttp3\.|javax\.|java\.|'
    r'org\.json\.|org\.apache\.)', re.I)

def _is_valid_pkg(pkg):
    """Strict Android package validator; avoids JS symbols and web domains."""
    if not pkg:
        return False

    pkg = str(pkg).strip().rstrip('.,;\'"\\ ')
    parts = pkg.split('.')

    if len(parts) < 3 or len(pkg) < 8:
        return False

    # Android package IDs are normally lowercase. This removes JS symbols like Array.prototype.forEach.
    if pkg != pkg.lower():
        return False

    if _SKIP_EXT.search(pkg):
        return False

    if _SKIP_PFX.match(pkg):
        return False

    domain_prefixes = (
        'www.', 'tpc.', 'fonts.', 'play.', 'support.', 'adssettings.',
        'googleads.', 'pagead2.', 'lh3.', 'i1.', 'pnce.', 'apis.',
        'feedback-pa.', 'cm.', 'csi.'
    )
    domain_markers = (
        'google', 'googlesyndication', 'googleadservices', 'googleapis',
        'gstatic', 'doubleclick', 'w3.org', 'ytimg', 'youtube'
    )

    if pkg.startswith(domain_prefixes):
        return False

    if any(marker in pkg for marker in domain_markers):
        return False

    for p in parts:
        if not p or not re.match(r'^[a-z][a-z0-9_]*$', p):
            return False

    return True

def extract_packages_from_text(raw_text):
    """Returns a SET of all unique, valid package names found in the text."""
    text = decode_all(raw_text)
    candidates = set()   

    patterns = [
        r"""['"]appId['"]\s*:\s*['"]([A-Za-z][\w.]+)['"]""",
        r"""play\.google\.com/store/apps/details[^\s'"<>]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
        r"""market://[^\s'"]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
        r"""(?:destination_url|final_url|click_url|destUrl|clickUrl|landingUrl)['"\s]*:['"\s]*['"][^'"]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
        r"""[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
        r"""[?&]package=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})"""
    ]

    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            pkg = m.group(1).rstrip('.,;\'"\\ ')
            if _is_valid_pkg(pkg):
                candidates.add(pkg)

    return candidates

def extract_package_from_page(page):
    """
    Scans strictly the rendered DOM and visible links. 
    Removes the background network fetching that caused cross-contamination.
    """
    collected_texts = []

    for frame in page.frames:
        try:
            frame_html = frame.evaluate("() => document.documentElement.outerHTML")
            if frame_html and len(frame_html) > 200:
                collected_texts.append(frame_html)

            hrefs = frame.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]'))
                           .map(a => a.href).filter(Boolean)
            """)
            if hrefs:
                collected_texts.append('\n'.join(hrefs))

            visible = frame.evaluate("() => document.body ? document.body.innerText : ''")
            if visible:
                collected_texts.append(visible)

        except Exception:
            continue

    try:
        visible = page.evaluate("() => document.body ? document.body.innerText : ''")
        if visible:
            collected_texts.append(visible)
        
        hrefs = page.evaluate("""
            () => Array.from(document.querySelectorAll('a[href]'))
                       .map(a => a.href).filter(Boolean)
        """)
        if hrefs:
            collected_texts.append('\n'.join(hrefs))
            
        main_html = page.evaluate("() => document.documentElement.outerHTML")
        if main_html:
            collected_texts.append(main_html)
    except Exception:
        pass

    combined = '\n'.join(collected_texts)
    return extract_packages_from_text(combined)

def extract_advertiser_from_page(page):
    try:
        loc = page.locator('.advertiser-title, [data-test-id="advertiser-name"]').first
        loc.wait_for(timeout=4000)
        text = loc.inner_text().strip()
        if text and len(text) > 1 and "Sign in" not in text:
            return text
    except Exception:
        pass

    js = r"""
    () => {
        const badWords = ['sign in', 'log in', 'home', 'menu', 'search', 'help', 'privacy', 'terms', 'ad details', 'see more ads', 'ads transparency'];
        let maxFont = 0;
        let advertiserName = "N/A";

        for (let el of document.querySelectorAll('body *')) {
            if (el.childElementCount > 0) continue;
            let txt = (el.innerText || "").trim();
            let lower = txt.toLowerCase();
            if (txt.length < 2 || txt.length > 60 || badWords.some(b => lower.includes(b))) continue;

            let rect = el.getBoundingClientRect();
            // Strict visual bounds check
            if (rect.width === 0 || rect.height === 0 || rect.y < 0 || rect.y > 350 || rect.width < 10) continue;

            let style = window.getComputedStyle(el);
            if (style.opacity === '0' || style.display === 'none' || style.visibility === 'hidden') continue;

            let font = parseFloat(style.fontSize || '0');
            if (font > maxFont) {
                maxFont = font;
                advertiserName = txt;
            }
        }
        return advertiserName;
    }
    """
    try:
        if advertiser := page.evaluate(js): return advertiser
    except Exception:
        pass
    return "N/A"

def _frame_parent_box(frame):
    """
    Returns the iframe element box in the parent page.
    This is important because a hidden/stale iframe can still return text from inside itself.
    """
    try:
        iframe_el = frame.frame_element()
        box = iframe_el.bounding_box()
        if not box:
            return None
        return box
    except Exception:
        return None


def _score_non_video_target(target):
    """
    Scores a page/frame by checking whether it looks like the active ad creative.
    Higher score = more likely to be the current transparency URL preview.
    """
    js = r"""
    () => {
        const cleanText = (txt) => (txt || '').replace(/\n/g, ' ').replace(/\s+/g, ' ').trim();

        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (
                rect.width > 0 &&
                rect.height > 0 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                rect.top < window.innerHeight &&
                rect.left < window.innerWidth &&
                style.visibility !== 'hidden' &&
                style.display !== 'none' &&
                style.opacity !== '0'
            );
        };

        const visibleText = cleanText(document.body ? document.body.innerText : '');
        const lowerText = visibleText.toLowerCase();

        const headlineNodes = Array.from(document.querySelectorAll(
            '[class*="-e-15"], [class*="headline"], [aria-label*="Headline"], [aria-label*="headline"]'
        )).filter(el => {
            const txt = cleanText(el.innerText || el.textContent || '');
            return txt.length >= 4 && txt.length <= 180 && isVisible(el) && !txt.includes('{{');
        });

        const descNodes = Array.from(document.querySelectorAll(
            '[class*="-e-67"], [class*="long-description"], [class*="description"], [aria-label*="Description"], [aria-label*="description"]'
        )).filter(el => {
            const txt = cleanText(el.innerText || el.textContent || '');
            return txt.length >= 8 && txt.length <= 260 && isVisible(el) && !txt.includes('{{');
        });

        const installNodes = Array.from(document.querySelectorAll('a[href], a[data-href], button')).filter(el => {
            const txt = cleanText(el.innerText || el.textContent || '').toLowerCase();
            const cls = String(el.className || '').toLowerCase();
            const aria = String(el.getAttribute('aria-label') || '').toLowerCase();
            const href = String(el.href || el.getAttribute('href') || el.getAttribute('data-href') || '').toLowerCase();
            const looksInstall = cls.includes('install-button-anchor') || txt.includes('install') || txt === 'get' || txt.includes('download') || aria.includes('install');
            const goodHref = href.includes('googleadservices.com/pagead/aclk') || href.includes('play.google.com') || href.includes('apps.apple.com') || href.includes('itunes.apple.com');
            return isVisible(el) && (looksInstall || goodHref);
        });

        const imageNodes = Array.from(document.querySelectorAll('img, picture, canvas, svg')).filter(el => {
            const src = String(el.getAttribute('src') || '').toLowerCase();
            const alt = String(el.getAttribute('alt') || '').toLowerCase();
            if (src.includes('googlelogo') || alt.includes('google')) return false;
            const rect = el.getBoundingClientRect();
            return isVisible(el) && rect.width >= 80 && rect.height >= 50;
        });

        const leafTextNodes = Array.from(document.querySelectorAll('*')).filter(el => {
            if (el.childElementCount > 0) return false;
            const txt = cleanText(el.innerText || el.textContent || '');
            if (txt.length < 4 || txt.length > 220) return false;
            if (txt.includes('{{') || txt.includes('}}')) return false;
            return isVisible(el);
        });

        let score = 0;
        score += Math.min(headlineNodes.length, 2) * 120;
        score += Math.min(descNodes.length, 2) * 100;
        score += Math.min(installNodes.length, 2) * 80;
        score += Math.min(imageNodes.length, 3) * 25;
        score += Math.min(leafTextNodes.length, 8) * 8;

        // The Google transparency shell/page chrome should not beat the actual creative iframe.
        if (lowerText.includes('ads transparency center') || lowerText.includes('ads transparency centre')) score -= 180;
        if (lowerText.includes('see more ads') || lowerText.includes('report this ad')) score -= 90;
        if (lowerText.includes('last shown') || lowerText.includes('shown in')) score -= 50;

        return {
            score,
            headlineCount: headlineNodes.length,
            descriptionCount: descNodes.length,
            installCount: installNodes.length,
            imageCount: imageNodes.length,
            leafTextCount: leafTextNodes.length,
            visibleTextLength: visibleText.length
        };
    }
    """
    try:
        return target.evaluate(js) or {"score": 0}
    except Exception:
        return {"score": 0}


def get_ranked_non_video_targets(page):
    """
    Returns frames/page ordered by the most likely active creative.
    Old logic checked page.frames in browser order, which can be wrong for repeated ads.
    """
    ranked = []

    for frame in page.frames:
        if frame == page.main_frame:
            continue

        parent_bonus = 0
        box = _frame_parent_box(frame)

        if box:
            width = box.get("width", 0) or 0
            height = box.get("height", 0) or 0
            y = box.get("y", 99999) or 99999
            area = width * height

            # Active ad preview iframe is normally visible and reasonably large.
            if width >= 120 and height >= 70:
                parent_bonus += min(area / 8000, 80)
            else:
                parent_bonus -= 120

            # Prefer currently visible/near-top preview, not repeated ads farther down the page.
            if -50 <= y <= 900:
                parent_bonus += 80
            elif 900 < y <= 1400:
                parent_bonus += 20
            else:
                parent_bonus -= 80
        else:
            parent_bonus -= 40

        inner = _score_non_video_target(frame)
        final_score = float(inner.get("score", 0) or 0) + parent_bonus

        if final_score > 0:
            ranked.append((final_score, frame, "iframe", inner))

    # Main page is only a fallback. It contains Google page chrome, so keep it below real creative frames.
    main_inner = _score_non_video_target(page)
    main_score = float(main_inner.get("score", 0) or 0) - 60
    if main_score > 0:
        ranked.append((main_score, page, "main_page", main_inner))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def wait_and_extract_text_ad_details(page, max_wait_seconds=15, preferred_target=None, image_box=None):
    """
    Extract headline/description from the SAME creative target where the image URL was found.

    Updated rule for your current Google ad layout:
    - Use Install/Get/Download/Open/Learn more only as an ANCHOR, never as headline/description.
    - First usable visible text BELOW the install anchor = headline.
    - Next usable visible text BELOW headline = description.
    - Description is selected by vertical position, not by length.
    - If no install anchor is found, fallback picks first usable text below/near the image, then the next text below it.
    """
    js = r"""
    (imageBox) => {
        const cleanText = (txt) => (txt || "")
            .replace(/\u00a0/g, " ")
            .replace(/\n/g, " ")
            .replace(/\s+/g, " ")
            .trim();

        const directTextOnly = (el) => cleanText(
            Array.from(el.childNodes || [])
                .filter(n => n.nodeType === Node.TEXT_NODE)
                .map(n => n.textContent || "")
                .join(" ")
        );

        const rectObj = (el) => {
            const rect = el.getBoundingClientRect();
            return {
                top: rect.top,
                bottom: rect.bottom,
                left: rect.left,
                right: rect.right,
                width: rect.width,
                height: rect.height
            };
        };

        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (
                rect.width > 0 &&
                rect.height > 0 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                rect.top < window.innerHeight &&
                rect.left < window.innerWidth &&
                style.visibility !== "hidden" &&
                style.display !== "none" &&
                style.opacity !== "0"
            );
        };

        const horizontalOverlapRatio = (a, b) => {
            const overlap = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
            return overlap / Math.max(1, Math.min(a.width, b.width));
        };

        const centerX = (b) => b.left + b.width / 2;
        const img = imageBox && imageBox.url !== "N/A" ? imageBox : null;

        const isInstallAnchorText = (txt) => {
            const lower = cleanText(txt).toLowerCase();
            if (!lower) return false;
            if (["install", "get", "download", "open", "learn more", "try now"].includes(lower)) return true;
            // Button text sometimes includes spaces/newlines or localized extra symbols.
            if (lower.length <= 25 && /\b(install|get|download|open|learn more|try now)\b/i.test(lower)) return true;
            return false;
        };

        const isBadText = (txt) => {
            const original = cleanText(txt);
            const lower = original.toLowerCase();
            if (!lower) return true;

            // Button labels are anchors only; never save them as headline/description.
            if (isInstallAnchorText(lower)) return true;

            const exactBad = new Set([
                "play", "close", "menu", "search", "sign in", "log in",
                "privacy", "terms", "help", "ad", "ads", "skip", "next"
            ]);
            if (exactBad.has(lower)) return true;

            const badContains = [
                "ads transparency center",
                "ads transparency centre",
                "report this ad",
                "see more ads",
                "last shown",
                "shown in",
                "about this ad",
                "my ad center",
                "why this ad",
                "ad choices",
                "advertiser verified",
                "this advertiser",
                "more details",
                "play.google.com/store/apps/details"
            ];
            if (badContains.some(b => lower.includes(b))) return true;

            if (/^https?:\/\//i.test(lower)) return true;
            if (/^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,}$/i.test(lower)) return true;
            if (/^[a-z0-9_-]{20,}$/i.test(lower)) return true;

            return false;
        };

        const insideOrNearImage = (box) => {
            if (!img) return true;
            const imageBoxForOverlap = {
                top: img.top,
                bottom: img.bottom,
                left: img.left,
                right: img.right,
                width: img.width,
                height: img.height
            };
            const alignedWithImage = horizontalOverlapRatio(box, imageBoxForOverlap) >= 0.10;
            const verticalNearImage = box.top >= img.top - 40 && box.top <= img.bottom + 650;
            const overlayOrInsideImage = box.top >= img.top - 20 && box.bottom <= img.bottom + 120;
            return alignedWithImage && (verticalNearImage || overlayOrInsideImage);
        };

        const installAnchors = [];
        const textCandidates = [];

        for (const el of Array.from(document.querySelectorAll("body *"))) {
            if (!isVisible(el)) continue;

            const rawText = cleanText(el.innerText || el.textContent || "");
            if (!rawText) continue;

            const cls = String(el.className || "").toLowerCase();
            const aria = String(el.getAttribute("aria-label") || "").toLowerCase();
            const role = String(el.getAttribute("role") || "").toLowerCase();
            const href = String(el.getAttribute("href") || el.href || el.getAttribute("data-href") || "").toLowerCase();
            const box = rectObj(el);
            if (box.width < 10 || box.height < 6) continue;

            const looksInstallByClass =
                cls.includes("install-button") ||
                cls.includes("install") ||
                aria.includes("install") ||
                href.includes("googleadservices.com/pagead/aclk") ||
                href.includes("play.google.com/store/apps/details");

            if ((isInstallAnchorText(rawText) || looksInstallByClass) && insideOrNearImage(box)) {
                let score = 0;
                if (isInstallAnchorText(rawText)) score += 200;
                if (cls.includes("install-button")) score += 160;
                if (role === "link" || el.tagName.toLowerCase() === "a" || el.tagName.toLowerCase() === "button") score += 60;
                if (img) {
                    score -= Math.min(Math.abs(box.top - img.bottom), 500) / 2;
                    score -= Math.min(Math.abs(centerX(box) - centerX(img)), 500) / 5;
                }
                installAnchors.push({ text: rawText, ...box, score });
                continue;
            }

            if (rawText.length < 3 || rawText.length > 240) continue;
            if (rawText.includes("{{") || rawText.includes("}}")) continue;
            if (isBadText(rawText)) continue;

            const looksLikeTextNode =
                cls.includes("headline") ||
                cls.includes("description") ||
                cls.includes("long-description") ||
                cls.includes("-e-15") ||
                cls.includes("-e-67") ||
                aria.includes("headline") ||
                aria.includes("description") ||
                role === "heading";

            // Avoid wrapper containers that combine button + headline + description.
            const directText = directTextOnly(el);
            if (el.children.length > 0 && !looksLikeTextNode && directText.length < 3) continue;
            if (!insideOrNearImage(box)) continue;

            const style = window.getComputedStyle(el);
            const fontSize = parseFloat(style.fontSize || "0") || 0;
            const weightRaw = String(style.fontWeight || "400");
            const fontWeight = weightRaw === "bold" ? 700 : (parseInt(weightRaw, 10) || 400);

            textCandidates.push({
                text: rawText,
                ...box,
                fontSize,
                fontWeight,
                isHeadlineClass: cls.includes("headline") || cls.includes("-e-15") || aria.includes("headline"),
                isDescriptionClass: cls.includes("description") || cls.includes("long-description") || cls.includes("-e-67") || aria.includes("description")
            });
        }

        const unique = [];
        const seen = new Set();
        for (const c of textCandidates) {
            // Keep same text only once; prefer the visually top/left smaller element, not wrapper duplicates.
            const key = c.text.toLowerCase();
            if (seen.has(key)) continue;
            seen.add(key);
            unique.push(c);
        }

        if (!unique.length) {
            return { headline: "N/A", description: "N/A", mode: "no_text" };
        }

        unique.sort((a, b) => {
            if (Math.abs(a.top - b.top) > 6) return a.top - b.top;
            return a.left - b.left;
        });

        let headlineObj = null;
        let descriptionObj = null;
        let mode = "fallback_image_order";

        if (installAnchors.length) {
            installAnchors.sort((a, b) => b.score - a.score);
            const anchor = installAnchors[0];
            mode = "install_anchor";

            const belowInstall = unique
                .filter(c => c.top >= anchor.bottom - 12)
                .filter(c => c.top <= anchor.bottom + 360)
                .filter(c => Math.abs(centerX(c) - centerX(anchor)) <= Math.max(420, anchor.width * 3))
                .map(c => {
                    const distance = Math.max(0, c.top - anchor.bottom);
                    let score = 1000 - Math.min(distance, 1000);
                    if (c.isHeadlineClass) score += 180;
                    if (c.isDescriptionClass) score -= 90;
                    if (c.fontWeight >= 600) score += 50;
                    if (img) score -= Math.min(Math.abs(centerX(c) - centerX(img)), 500) / 8;
                    return { ...c, score };
                })
                .sort((a, b) => {
                    // Main rule: first usable visible text below install.
                    if (Math.abs(a.top - b.top) > 8) return a.top - b.top;
                    return b.score - a.score;
                });

            headlineObj = belowInstall.length ? belowInstall[0] : null;
        }

        // Fallback when install anchor is not available: first usable text below/near the image.
        if (!headlineObj) {
            let pool = unique;
            if (img) {
                pool = unique.filter(c => c.top >= img.bottom - 30 || (c.top >= img.top - 20 && c.bottom <= img.bottom + 120));
            }

            pool = pool.map(c => {
                let score = 0;
                if (img) score -= Math.min(Math.max(0, c.top - img.bottom), 600);
                if (c.isHeadlineClass) score += 150;
                if (c.isDescriptionClass) score -= 80;
                if (c.fontWeight >= 600) score += 30;
                return { ...c, score };
            }).sort((a, b) => {
                if (Math.abs(a.top - b.top) > 8) return a.top - b.top;
                return b.score - a.score;
            });

            headlineObj = pool.length ? pool[0] : unique[0];
        }

        if (headlineObj) {
            const belowHeadline = unique
                .filter(c => c.text !== headlineObj.text)
                .filter(c => c.top >= headlineObj.bottom - 10)
                .filter(c => c.top <= headlineObj.bottom + 320)
                .filter(c => Math.abs(centerX(c) - centerX(headlineObj)) <= Math.max(420, headlineObj.width * 2.5))
                .map(c => {
                    const distance = Math.max(0, c.top - headlineObj.bottom);
                    let score = 1000 - Math.min(distance, 1000);
                    if (c.isDescriptionClass) score += 180;
                    if (c.isHeadlineClass) score -= 80;
                    if (c.fontSize <= headlineObj.fontSize + 4) score += 30;
                    return { ...c, score };
                })
                .sort((a, b) => {
                    // Main rule: next usable visible text below headline.
                    if (Math.abs(a.top - b.top) > 8) return a.top - b.top;
                    return b.score - a.score;
                });

            descriptionObj = belowHeadline.length ? belowHeadline[0] : null;
        }

        return {
            headline: headlineObj ? headlineObj.text : "N/A",
            description: descriptionObj ? descriptionObj.text : "N/A",
            mode,
            textCount: unique.length,
            installCount: installAnchors.length
        };
    }
    """

    def read_target(target):
        try:
            data = target.evaluate(js, image_box or None)
            if not data:
                return None

            headline = clean_text(data.get("headline"))
            description = clean_text(data.get("description"))

            if headline != "N/A" or description != "N/A":
                return {"headline": headline, "description": description}
        except Exception:
            return None
        return None

    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        # Strong fix: if the image target is known, ONLY read text from that same target.
        if preferred_target is not None:
            data = read_target(preferred_target)
            if data:
                return data
            page.wait_for_timeout(1000)
            continue

        # Fallback for video/text-only ads where no image target exists.
        try:
            ranked_targets = get_ranked_non_video_targets(page)
        except Exception:
            ranked_targets = []

        for _, target, _, _ in ranked_targets:
            data = read_target(target)
            if data:
                return data

        page.wait_for_timeout(1000)

    return {"headline": "N/A", "description": "N/A"}



# =========================
# IMAGE AD ACTIVE-TARGET LOGIC (DEBUG-BASED FIX)
# =========================

IMAGE_AD_MIN_WAIT_SECONDS = 10
IMAGE_AD_MAX_WAIT_SECONDS = 25


def extract_image_ad_text_quick_from_target(target, image_box=None):
    """
    Reads IMAGE ad headline/description from the active creative frame only.
    This is optimized for Google UAC image previews like youtube_home.html:
    - headline/app title: landscape-app-title / app-title / title
    - description/app text: landscape-app-text / app-text / description
    It ignores INSTALL, Ad, NaN, [PRICE], and wrapper text.
    """
    js = r"""
    (imageBox) => {
        const cleanText = (txt) => (txt || "")
            .replace(/\u00a0/g, " ")
            .replace(/\n/g, " ")
            .replace(/\s+/g, " ")
            .trim();

        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (
                rect.width > 0 &&
                rect.height > 0 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                rect.top < window.innerHeight &&
                rect.left < window.innerWidth &&
                style.visibility !== "hidden" &&
                style.display !== "none" &&
                style.opacity !== "0"
            );
        };

        const rectObj = (el) => {
            const r = el.getBoundingClientRect();
            return {
                top: r.top,
                bottom: r.bottom,
                left: r.left,
                right: r.right,
                width: r.width,
                height: r.height
            };
        };

        const isBadText = (txt) => {
            const t = cleanText(txt);
            const l = t.toLowerCase();
            if (!l) return true;
            if (["install", "get", "download", "open", "learn more", "try now", "ad", "nan", "[price]", "adnan[price]"].includes(l)) return true;
            if (l.includes("nan") || l.includes("[price]")) return true;
            if (l.includes("ads transparency") || l.includes("report this ad") || l.includes("see more ads")) return true;
            if (l.includes("last shown") || l.includes("format:") || l.includes("shown anywhere")) return true;
            if (/^https?:\/\//i.test(l)) return true;
            if (/^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,}$/i.test(l)) return true;
            if (/^[·•.\s]+$/.test(t)) return true;
            return false;
        };

        const img = imageBox && imageBox.url !== "N/A" ? imageBox : null;

        const nearImage = (box) => {
            if (!img) return true;
            const horizontallyClose = box.left <= img.right + 80 && box.right >= img.left - 80;
            const verticallyClose = box.top >= img.top - 80 && box.top <= img.bottom + 260;
            return horizontallyClose && verticallyClose;
        };

        const getFirstVisible = (selectors) => {
            const nodes = Array.from(document.querySelectorAll(selectors));
            const out = [];
            for (const el of nodes) {
                if (!isVisible(el)) continue;
                const txt = cleanText(el.innerText || el.textContent || "");
                if (txt.length < 3 || txt.length > 160) continue;
                if (isBadText(txt)) continue;
                const box = rectObj(el);
                if (!nearImage(box)) continue;
                out.push({ text: txt, ...box });
            }
            out.sort((a,b) => (a.top - b.top) || (a.left - b.left));
            return out.length ? out[0] : null;
        };

        // Most reliable for this Google image ad template.
        let headline = getFirstVisible(
            '[class*="app-title"], [class*="title-bar"], [class*="headline"], [class*="-e-15"]'
        );

        let description = getFirstVisible(
            '[class*="app-text"], [class*="description"], [class*="long-description"], [class*="-e-67"]'
        );

        if (headline && description && headline.text === description.text) {
            description = null;
        }

        // Fallback: use visible leaf text below INSTALL / below image.
        if (!headline || !description) {
            const installNodes = [];
            const textNodes = [];

            for (const el of Array.from(document.querySelectorAll("body *"))) {
                if (!isVisible(el)) continue;
                const txt = cleanText(el.innerText || el.textContent || "");
                if (!txt) continue;
                const box = rectObj(el);
                const lower = txt.toLowerCase();

                if (
                    lower === "install" ||
                    lower === "get" ||
                    lower === "download" ||
                    lower === "open" ||
                    lower === "learn more"
                ) {
                    installNodes.push({ text: txt, ...box });
                    continue;
                }

                // Leaf/direct visible text only, avoid wrappers that combine all ad text.
                if (el.children.length > 0) continue;
                if (txt.length < 3 || txt.length > 160) continue;
                if (isBadText(txt)) continue;
                if (!nearImage(box)) continue;
                textNodes.push({ text: txt, ...box });
            }

            textNodes.sort((a,b) => (a.top - b.top) || (a.left - b.left));

            if (!headline) {
                let pool = textNodes;
                if (installNodes.length) {
                    installNodes.sort((a,b) => (a.top - b.top) || (a.left - b.left));
                    const anchor = installNodes[installNodes.length - 1];
                    pool = textNodes.filter(t => t.top >= anchor.bottom - 10);
                } else if (img) {
                    pool = textNodes.filter(t => t.top >= img.bottom - 20);
                }
                headline = pool.length ? pool[0] : null;
            }

            if (!description && headline) {
                const pool = textNodes.filter(t => t.text !== headline.text && t.top >= headline.bottom - 8);
                description = pool.length ? pool[0] : null;
            }
        }

        const bodyText = cleanText(document.body ? document.body.innerText : "");

        return {
            headline: headline ? headline.text : "N/A",
            description: description ? description.text : "N/A",
            bodyText: bodyText,
            hasImageAdText: !!headline || !!description
        };
    }
    """
    try:
        data = target.evaluate(js, image_box or None) or {}
        return {
            "headline": clean_text(data.get("headline")),
            "description": clean_text(data.get("description")),
            "body_text": clean_text(data.get("bodyText")),
            "has_image_ad_text": bool(data.get("hasImageAdText"))
        }
    except Exception:
        return {
            "headline": "N/A",
            "description": "N/A",
            "body_text": "N/A",
            "has_image_ad_text": False
        }


def extract_packages_from_target_and_ancestors(target, page=None, max_depth=4):
    """
    Critical fix:
    The visible image/text is often inside child youtube_home.html iframe,
    but the Play Store package is in its parent safeframe HTML.
    So search only the active target + its ancestors, never the whole page.
    """
    collected_texts = []
    current = target
    depth = 0
    seen = set()

    while current is not None and depth < max_depth:
        try:
            # Do NOT include the main Google Transparency page; it may contain unrelated stale frames.
            if page is not None and hasattr(page, "main_frame") and current == page.main_frame:
                break

            key = getattr(current, "url", "") or str(id(current))
            if key in seen:
                break
            seen.add(key)

            try:
                html = current.evaluate("() => document.documentElement ? document.documentElement.outerHTML : ''")
                if html:
                    collected_texts.append(html)
            except Exception:
                pass

            try:
                hrefs = current.evaluate("""
                    () => Array.from(document.querySelectorAll('a[href], a[data-href]'))
                        .map(a => a.href || a.getAttribute('href') || a.getAttribute('data-href') || '')
                        .filter(Boolean)
                """)
                if hrefs:
                    collected_texts.append("\n".join(hrefs))
            except Exception:
                pass

            try:
                body_text = current.evaluate("() => document.body ? document.body.innerText : ''")
                if body_text:
                    collected_texts.append(body_text)
            except Exception:
                pass

            # Move to parent frame only. This keeps the package scoped to the active creative.
            if hasattr(current, "parent_frame"):
                current = current.parent_frame
            else:
                break

            depth += 1

        except Exception:
            break

    packages = extract_packages_from_text("\n".join(collected_texts))

    # Keep only real Android package-looking values; avoid domains accidentally collected from JS.
    cleaned = set()
    for pkg in packages:
        if not pkg or pkg == "N/A":
            continue
        if _is_valid_pkg(pkg):
            cleaned.add(pkg)

    return cleaned


def _image_target_parent_box(target):
    try:
        if hasattr(target, "frame_element"):
            return _frame_parent_box(target)
    except Exception:
        pass
    return None


def _get_parent_frame(frame):
    """Playwright exposes parent_frame as a property in sync API; keep safe for variants."""
    try:
        parent = getattr(frame, "parent_frame", None)
        if callable(parent):
            parent = parent()
        return parent
    except Exception:
        return None


def _get_child_frames(frame):
    try:
        children = getattr(frame, "child_frames", [])
        if callable(children):
            children = children()
        return list(children or [])
    except Exception:
        return []


def _frame_descendants_including_self(frame, max_depth=5):
    """Return frame + descendants only inside one visible ad root."""
    out = []
    stack = [(frame, 0)]
    seen = set()

    while stack:
        current, depth = stack.pop(0)
        key = id(current)
        if key in seen:
            continue
        seen.add(key)
        out.append(current)

        if depth >= max_depth:
            continue

        for child in _get_child_frames(current):
            stack.append((child, depth + 1))

    return out


def _visible_top_level_ad_roots(page):
    """
    Strict root selection:
    only top-level iframes whose element is visibly placed in the current ad preview card.
    This prevents using stale/background iframes from other variations/ads.
    """
    roots = []

    try:
        frames = list(page.frames)
    except Exception:
        frames = []

    viewport_width = 1366
    viewport_height = 768
    try:
        vp = page.viewport_size or {}
        viewport_width = vp.get("width", viewport_width) or viewport_width
        viewport_height = vp.get("height", viewport_height) or viewport_height
    except Exception:
        pass

    main_frame = getattr(page, "main_frame", None)

    for frame in frames:
        try:
            if frame == main_frame:
                continue

            # Root must be directly embedded in the main Transparency page.
            # Child creative frames are evaluated only under this root.
            parent = _get_parent_frame(frame)
            if main_frame is not None and parent != main_frame:
                continue

            box = _frame_parent_box(frame)
            if not box:
                continue

            x = float(box.get("x", 0) or 0)
            y = float(box.get("y", 0) or 0)
            width = float(box.get("width", 0) or 0)
            height = float(box.get("height", 0) or 0)

            if width < 250 or height < 250:
                continue

            # Must be at least partially visible in/near current viewport.
            if x + width <= 0 or x >= viewport_width:
                continue
            if y + height <= 250 or y >= max(1800, viewport_height + 1200):
                continue

            url = getattr(frame, "url", "") or ""
            url_l = url.lower()
            if not (
                "safeframe" in url_l
                or "adframe" in url_l
                or "googlesyndication" in url_l
                or "doubleclick" in url_l
            ):
                continue

            # Prefer the central visible preview slot, not offscreen/repeated frames.
            center_x = x + width / 2
            center_y = y + height / 2
            page_center_x = viewport_width / 2

            score = 0.0
            score += min((width * height) / 4000.0, 180)
            score += max(0, 140 - abs(center_x - page_center_x) / 3)
            if 350 <= y <= 1250:
                score += 160
            elif 250 <= y <= 1500:
                score += 80
            else:
                score -= 80

            roots.append({
                "score": round(score, 2),
                "frame": frame,
                "box": box,
                "url": url,
            })
        except Exception:
            continue

    roots.sort(key=lambda r: r["score"], reverse=True)
    return roots


def extract_template_ad_records_from_target(target):
    """
    Extracts Google template adData from the visible safeframe/root.
    This is the most reliable source for appId/package and the intended image URL.
    """
    js = r"""
    () => {
        const clean = (v) => (typeof v === 'string' ? v.trim() : '');
        const abs = (u) => {
            u = clean(u);
            if (!u) return '';
            try { return new URL(u, location.href).href; } catch(e) { return u; }
        };
        const first = (obj, keys) => {
            if (!obj) return '';
            for (const k of keys) {
                const v = clean(obj[k]);
                if (v) return v;
            }
            return '';
        };
        const allImages = (obj) => {
            const keys = [
                'landscapeImage', 'portraitImage', 'squareImage', 'image', 'imageUrl',
                'marketingImage', 'thumbnailImage', 'mediaImage', 'appIcon', 'iconUrl'
            ];
            const out = [];
            for (const k of keys) {
                const u = abs(obj && obj[k]);
                if (u) out.push(u);
            }
            return Array.from(new Set(out));
        };
        const records = [];
        const configs = [];

        try { if (window.exitConfig) configs.push(window.exitConfig); } catch(e) {}
        try { if (window.google_template_data) configs.push({google_template_data: window.google_template_data}); } catch(e) {}
        try { if (window.adData) configs.push({google_template_data: {adData: Array.isArray(window.adData) ? window.adData : [window.adData]}}); } catch(e) {}

        for (const cfg of configs) {
            const gtd = cfg.google_template_data || cfg.googleTemplateData || {};
            let arr = gtd.adData || cfg.adData || [];
            if (!Array.isArray(arr)) arr = [arr];
            for (const d of arr) {
                if (!d || typeof d !== 'object') continue;
                const appId = first(d, ['appId', 'packageName', 'package', 'androidPackage']);
                const destinationUrl = first(d, ['destination_url', 'destinationUrl', 'final_url', 'finalUrl', 'click_url', 'clickUrl']) || clean(cfg.destination_url) || clean(cfg.final_url);
                const redirectUrl = first(d, ['redirect_url', 'redirectUrl']) || clean(cfg.redirect_url);
                const headline = first(d, ['headline', 'headline1', 'appTitle', 'appName', 'title', 'name', 'shortTitle']);
                const description = first(d, ['description1', 'description', 'longDescription', 'description2', 'body', 'subtitle', 'appText']);
                const images = allImages(d);
                if (!appId && !destinationUrl && !redirectUrl && !images.length && !headline && !description) continue;
                records.push({
                    appId,
                    destinationUrl,
                    redirectUrl,
                    headline,
                    description,
                    images,
                    rawKeys: Object.keys(d).slice(0, 80)
                });
            }
        }
        return records;
    }
    """
    try:
        records = target.evaluate(js) or []
        if not isinstance(records, list):
            return []
        return records
    except Exception:
        return []


def _extract_package_from_template_record(record):
    if not record:
        return "N/A"

    for key in ["appId", "packageName", "package"]:
        pkg = clean_text(record.get(key))
        if pkg != "N/A" and _is_valid_pkg(pkg):
            return pkg

    for key in ["destinationUrl", "redirectUrl"]:
        link = clean_text(record.get(key))
        pkg = extract_package_name(clean_googleadservices_link(link))
        if pkg != "N/A" and _is_valid_pkg(pkg):
            return pkg

    return "N/A"


def _urls_match(u1, u2):
    u1 = clean_text(u1)
    u2 = clean_text(u2)
    if u1 == "N/A" or u2 == "N/A":
        return False
    if u1 == u2:
        return True
    try:
        p1 = urlparse(u1)
        p2 = urlparse(u2)
        return p1.netloc == p2.netloc and p1.path.rstrip('/') == p2.path.rstrip('/')
    except Exception:
        return False


def _record_matches_image(record, image_url):
    for img in record.get("images", []) or []:
        if _urls_match(img, image_url):
            return True
    return False


def get_active_image_ad_candidate_once(page):
    """
    Strict image-ad extraction:
    1) choose a visible top-level ad root iframe from the current preview only
    2) read package/template data from that root
    3) read image/text only from children inside that same root
    No page-wide frame scan.
    """
    roots = _visible_top_level_ad_roots(page)
    if not roots:
        return None

    all_candidates = []

    for root in roots[:3]:
        root_frame = root["frame"]
        root_box = root["box"]
        root_records = extract_template_ad_records_from_target(root_frame)

        # Strict scoped packages only from this visible root.
        scoped_packages = extract_packages_from_target_and_ancestors(root_frame, page=page, max_depth=1)

        for target in _frame_descendants_including_self(root_frame, max_depth=5):
            try:
                image_data = extract_primary_image_data_from_target(target)
                if not image_data:
                    continue

                image_url = clean_text(image_data.get("url"))
                if image_url == "N/A":
                    continue

                text_data = extract_image_ad_text_quick_from_target(target, image_data)
                headline = clean_text(text_data.get("headline"))
                description = clean_text(text_data.get("description"))
                body_text = clean_text(text_data.get("body_text"))

                matching_records = [r for r in root_records if _record_matches_image(r, image_url)]
                best_record = matching_records[0] if matching_records else (root_records[0] if len(root_records) == 1 else None)

                template_package = _extract_package_from_template_record(best_record) if best_record else "N/A"
                if template_package != "N/A":
                    scoped_packages = set(scoped_packages or set())
                    scoped_packages.add(template_package)

                # Use visible text first. Template text is only fallback.
                if (headline == "N/A" or not headline) and best_record:
                    headline = clean_text(best_record.get("headline"))
                if (description == "N/A" or not description) and best_record:
                    description = clean_text(best_record.get("description"))

                score = 0.0
                score += float(image_data.get("width", 0) or 0) * float(image_data.get("height", 0) or 0) / 1000.0
                score += float(root.get("score", 0) or 0)

                if image_url.startswith("https://tpc.googlesyndication.com/simgad/"):
                    score += 150
                if headline != "N/A":
                    score += 120
                if description != "N/A":
                    score += 80
                if matching_records:
                    score += 220
                if template_package != "N/A":
                    score += 160
                if scoped_packages:
                    score += 80

                lower_body = body_text.lower()
                if "install" in lower_body or "get" in lower_body or "download" in lower_body:
                    score += 60
                if "ads transparency" in lower_body or "report this ad" in lower_body or "see more ads" in lower_body:
                    score -= 300

                all_candidates.append({
                    "score": round(score, 2),
                    "target": target,
                    "root_frame": root_frame,
                    "root_box": root_box,
                    "image_url": image_url,
                    "image_box": image_data,
                    "headline": headline,
                    "description": description,
                    "packages": scoped_packages,
                    "template_package": template_package,
                    "template_records_count": len(root_records),
                    "matched_template_record": bool(matching_records),
                    "body_text": body_text,
                    "parent_box": _image_target_parent_box(target),
                })
            except Exception:
                continue

    if not all_candidates:
        return None

    all_candidates.sort(key=lambda c: c["score"], reverse=True)
    return all_candidates[0]

def resolve_package_from_scoped_packages(headline, description, scoped_packages):
    """
    Resolve package only from active target/ancestor scoped packages.
    No whole-page package fallback.
    """
    if not scoped_packages:
        return "N/A", 0.0

    scoped_packages = sorted(scoped_packages)

    if len(scoped_packages) == 1:
        return scoped_packages[0], 1.0

    package_name, score = get_best_matching_package(headline, description, scoped_packages)

    if package_name:
        return package_name, score

    return "N/A", score


def wait_and_extract_active_image_ad_data(page, max_wait_seconds=IMAGE_AD_MAX_WAIT_SECONDS, min_wait_seconds=IMAGE_AD_MIN_WAIT_SECONDS):
    """
    Waits until active image ad has an image URL.
    Headline/description are optional because some ads are image-only.
    Debug showed the full image ad appears around step 0010, so this function does not
    return before min_wait_seconds unless timeout is reached.
    """
    start = time.time()
    best_seen = None

    while time.time() - start < max_wait_seconds:
        candidate = get_active_image_ad_candidate_once(page)

        if candidate and (best_seen is None or candidate["score"] > best_seen["score"]):
            best_seen = candidate

        elapsed = time.time() - start

        if candidate:
            has_image = candidate.get("image_url", "N/A") != "N/A"
            has_text = is_valid_text_ad(candidate.get("headline"), candidate.get("description"))

            # Wait at least 10 sec for Google creative hydration, then accept active image data.
            # IMPORTANT: image-only ads have NO headline/description in DOM.
            # If image_url exists, return the candidate even when has_text is False.
            if elapsed >= min_wait_seconds and has_image:
                package_name, package_score = resolve_package_from_scoped_packages(
                    candidate.get("headline", "N/A"),
                    candidate.get("description", "N/A"),
                    candidate.get("packages", set())
                )

                candidate["package_name"] = package_name
                candidate["package_score"] = package_score
                candidate["app_link"] = (
                    f"https://play.google.com/store/apps/details?id={package_name}"
                    if package_name != "N/A"
                    else "N/A"
                )
                return candidate

        page.wait_for_timeout(1000)

    if best_seen:
        package_name, package_score = resolve_package_from_scoped_packages(
            best_seen.get("headline", "N/A"),
            best_seen.get("description", "N/A"),
            best_seen.get("packages", set())
        )

        best_seen["package_name"] = package_name
        best_seen["package_score"] = package_score
        best_seen["app_link"] = (
            f"https://play.google.com/store/apps/details?id={package_name}"
            if package_name != "N/A"
            else "N/A"
        )
        return best_seen

    return None


# =========================
# MAIN COMBINED SCRAPER: VIDEO ADS + TEXT ADS
# =========================

def is_valid_text_ad(headline, description):
    if headline and headline != "N/A" and len(clean_text(headline)) >= 3:
        return True
    if description and description != "N/A" and len(clean_text(description)) >= 15:
        return True
    return False




def _looks_like_real_ad_image_url_for_fallback(image_url):
    """Keep fallback conservative: accept real ad image URLs, skip logos/icons/UI assets."""
    image_url = clean_text(image_url)
    if image_url == "N/A":
        return False

    lower = image_url.lower()
    if lower.startswith("data:image"):
        return False

    bad_parts = [
        "googlelogo", "favicon", "adchoices", "/branding/", "doubleclick.net/static",
        "gstatic.com/images/branding", "material-icons", "logo", "icon"
    ]

    # tpc simgad is the actual creative image in your debug, so always allow it.
    if "tpc.googlesyndication.com/simgad/" in lower or "/simgad/" in lower:
        return True

    if any(bad in lower for bad in bad_parts):
        return False

    return lower.startswith(("http://", "https://", "blob:"))


def fallback_extract_image_url_same_way(page, max_wait_seconds=20):
    """
    Last-resort IMAGE-ONLY fallback.
    Uses the same existing image extractor (wait_and_extract_image_url_with_target)
    and does NOT require headline/description. This is only for rows where text is missing.
    """
    try:
        image_url, image_target, image_box = wait_and_extract_image_url_with_target(
            page,
            max_wait_seconds=max_wait_seconds
        )
        image_url = clean_text(image_url)
        if _looks_like_real_ad_image_url_for_fallback(image_url):
            return image_url
    except Exception:
        pass

    # Extra backup: sometimes image-only creatives expose image URL only in template/adData,
    # not as readable text. Still keep it scoped to visible ad roots, not the whole page.
    try:
        for root in _visible_top_level_ad_roots(page)[:3]:
            root_frame = root.get("frame")
            if not root_frame:
                continue
            records = extract_template_ad_records_from_target(root_frame)
            for record in records or []:
                for img in record.get("images", []) or []:
                    img = _normalize_image_url(img, base_url=getattr(root_frame, "url", "") or "")
                    img = clean_text(img)
                    if _looks_like_real_ad_image_url_for_fallback(img):
                        return img
    except Exception:
        pass

    return "N/A"






def save_scrape_result(
    row_num,
    advertiser,
    transparency_url,
    package_name="N/A",
    app_link="N/A",
    media_value="N/A",
    headline="N/A",
    description="N/A",
    image_url="N/A",
    status="SUCCESS",
    log_type="COMBINED",
    message="",
    event_time=None,
):
    """Write one normalized result to the sheet and log it exactly once."""
    event_time = event_time or get_exact_time()
    package_name = clean_text(package_name)
    app_link = clean_text(app_link)
    media_value = clean_text(media_value)
    headline = clean_text(headline)
    description = clean_text(description)
    image_url = clean_text(image_url)

    safe_update_scrape_result(
        row_num=row_num,
        combined_data=[
            clean_text(advertiser),
            package_name,
            transparency_url,
            app_link,
            event_time,
            media_value,
            event_time,
        ],
        headline=headline,
        description=description,
        image_url=image_url,
    )
    safe_add_log(
        row_number=row_num,
        status=status,
        log_type=log_type,
        url=transparency_url,
        video_id=media_value,
        app_link=app_link,
        message=message,
    )


def resolve_text_ad_package(page, headline, description, max_link_wait=8):
    """
    Resolve a text-ad package in priority order:
    1) the visible Install/Get/Download link;
    2) strict matching against package IDs found in the rendered page.
    """
    app_link = wait_and_extract_install_link(page, max_wait_seconds=max_link_wait)
    package_name = extract_package_name(app_link)

    if package_name != "N/A":
        return package_name, app_link, 1.0, "visible install link"

    packages = extract_package_from_page(page)
    package_name, match_score = get_best_matching_package(
        headline,
        description,
        packages,
    )

    if package_name:
        app_link = f"https://play.google.com/store/apps/details?id={package_name}"
        return package_name, app_link, match_score, "strict headline/description match"

    return "N/A", "N/A", match_score, "not found"


def reset_page_after_video_probe(page, url):
    """
    Video probing may click or scroll a preview. Reload before non-video extraction
    so image and text logic starts from the original active creative.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)


def scrape_single_url(url_row):
    """Extract video, image, or text ad data from one Transparency Center URL."""
    row_num, source_url = url_row
    url = source_url

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            service_workers="block",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        captured = {"video_id": "N/A"}

        def handle_response(response):
            try:
                if not is_real_video_response(response):
                    return
                video_id = extract_video_id_from_url(response.url)
                if video_id and captured["video_id"] == "N/A":
                    captured["video_id"] = video_id
            except Exception:
                pass

        page.on("response", handle_response)

        try:
            if "region=" not in url:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}region=anywhere"

            print(f"🔍 Row {row_num}: opening transparency URL")
            safe_add_log(
                row_number=row_num,
                status="STARTED",
                log_type="COMBINED",
                url=url,
                message="Started combined video/image/text extraction",
            )

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            advertiser = extract_advertiser_from_page(page)

            # 1) VIDEO: preserve the original working video-first detection flow.
            video_id = detect_video_id(page, captured)
            if video_id != "N/A":
                print(f"🎬 Row {row_num}: video ID found: {video_id}")
                app_link = wait_and_extract_install_link(page, max_wait_seconds=35)
                package_name = extract_package_name(app_link)
                headline, description = wait_and_extract_headline_description(
                    page,
                    max_wait_seconds=15,
                )

                status = "SUCCESS" if app_link != "N/A" else "VIDEO_FOUND_APP_LINK_NOT_FOUND"
                message = (
                    "Video ID, app link and text saved"
                    if app_link != "N/A"
                    else "Video ID found, but visible app link was not found"
                )
                save_scrape_result(
                    row_num=row_num,
                    advertiser=advertiser,
                    transparency_url=url,
                    package_name=package_name,
                    app_link=app_link,
                    media_value=video_id,
                    headline=headline,
                    description=description,
                    image_url="N/A",
                    status=status,
                    log_type="VIDEO_AD",
                    message=message,
                )
                print(f"✅ Row {row_num}: saved VIDEO ad")
                return

            # Restore the unmodified creative before image/text extraction.
            print(f"📄 Row {row_num}: no video found; resetting preview for non-video extraction")
            reset_page_after_video_probe(page, url)
            advertiser = extract_advertiser_from_page(page)

            # 2) IMAGE: use the active-root/child-frame lock from the working image scraper.
            image_ad = wait_and_extract_active_image_ad_data(
                page,
                max_wait_seconds=IMAGE_AD_MAX_WAIT_SECONDS,
                min_wait_seconds=IMAGE_AD_MIN_WAIT_SECONDS,
            )

            if image_ad and clean_text(image_ad.get("image_url")) != "N/A":
                image_url = clean_text(image_ad.get("image_url"))
                headline = clean_text(image_ad.get("headline"))
                description = clean_text(image_ad.get("description"))
                package_name = clean_text(image_ad.get("package_name"))
                app_link = clean_text(image_ad.get("app_link"))
                package_score = image_ad.get("package_score", 0.0)

                # Safe fallback: only a visible install link, never a page-wide package guess.
                if package_name == "N/A":
                    visible_app_link = wait_and_extract_install_link(page, max_wait_seconds=5)
                    visible_package = extract_package_name(visible_app_link)
                    if visible_package != "N/A":
                        package_name = visible_package
                        app_link = visible_app_link
                        package_score = 1.0

                has_text = is_valid_text_ad(headline, description)
                if not has_text:
                    headline = "N/A"
                    description = "N/A"

                status = "SUCCESS" if package_name != "N/A" else "IMAGE_PACKAGE_NOT_FOUND"
                message = (
                    f"Image URL and active creative data saved; package score={package_score}"
                    if package_name != "N/A"
                    else "Image URL saved, but package was not found in the active creative scope"
                )
                save_scrape_result(
                    row_num=row_num,
                    advertiser=advertiser,
                    transparency_url=url,
                    package_name=package_name,
                    app_link=app_link,
                    media_value="image",
                    headline=headline,
                    description=description,
                    image_url=image_url,
                    status=status,
                    log_type="IMAGE_AD",
                    message=message,
                )
                print(f"✅ Row {row_num}: saved IMAGE ad")
                return

            # 3) TEXT: use the original ranked-target text extraction and strict package matching.
            print(f"📝 Row {row_num}: no active image creative found; checking text ad")
            text_data = wait_and_extract_text_ad_details(page, max_wait_seconds=15)
            headline = clean_text(text_data.get("headline"))
            description = clean_text(text_data.get("description"))

            if is_valid_text_ad(headline, description):
                package_name, app_link, match_score, source = resolve_text_ad_package(
                    page,
                    headline,
                    description,
                    max_link_wait=8,
                )
                status = "SUCCESS" if package_name != "N/A" else "TEXT_PACKAGE_NOT_FOUND"
                message = (
                    f"Text ad package resolved from {source}; score={match_score}"
                    if package_name != "N/A"
                    else f"Text ad found, but package was not resolved; best score={match_score}"
                )
                save_scrape_result(
                    row_num=row_num,
                    advertiser=advertiser,
                    transparency_url=url,
                    package_name=package_name,
                    app_link=app_link,
                    media_value="text",
                    headline=headline,
                    description=description,
                    image_url="N/A",
                    status=status,
                    log_type="TEXT_AD",
                    message=message,
                )
                print(f"✅ Row {row_num}: saved TEXT ad")
                return

            # Final conservative fallback for image-only ads whose active frame was late/unusual.
            fallback_image_url = fallback_extract_image_url_same_way(
                page,
                max_wait_seconds=12,
            )
            if fallback_image_url != "N/A":
                save_scrape_result(
                    row_num=row_num,
                    advertiser=advertiser,
                    transparency_url=url,
                    media_value="image",
                    image_url=fallback_image_url,
                    status="IMAGE_ONLY_URL_SAVED",
                    log_type="IMAGE_AD",
                    message="Image-only creative saved from conservative fallback",
                )
                print(f"✅ Row {row_num}: saved fallback IMAGE-only ad")
                return

            save_scrape_result(
                row_num=row_num,
                advertiser=advertiser,
                transparency_url=url,
                status="NO_SUPPORTED_AD_DATA",
                log_type="COMBINED",
                message="No video ID, active image URL, or valid text ad was found",
            )
            print(f"⏭ Row {row_num}: no supported ad data found")

        except Exception as exc:
            error_time = get_exact_time()
            print(f"❌ Row {row_num} error at {error_time}: {exc}")
            try:
                save_scrape_result(
                    row_num=row_num,
                    advertiser="N/A",
                    transparency_url=url,
                    app_link="ERROR",
                    media_value="ERROR",
                    status="ERROR",
                    log_type="COMBINED",
                    message=str(exc),
                    event_time=error_time,
                )
            except Exception:
                pass
        finally:
            page.close()
            context.close()
            browser.close()


def run_parallel_combined_scraper(max_workers=2):
    # Exact row numbers prevent blank input rows from shifting output writes.
    url_rows = [
        (row_num, url.strip())
        for row_num, url in sheets.get_url_rows_with_retry(only_unprocessed=False)
        if url and url.strip()
    ]

    if not url_rows:
        print("No transparency URLs found in column H.")
        return

    print(f"🚀 Starting combined VIDEO + IMAGE + TEXT scraper for {len(url_rows)} rows")
    print(f"⚡ Running parallel with max_workers={max_workers}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(scrape_single_url, url_row): url_row
            for url_row in url_rows
        }

        for future in as_completed(futures):
            row_num, _ = futures[future]

            try:
                future.result()
            except Exception as e:
                print(f"❌ Worker failed for row {row_num}: {e}")

                try:
                    safe_add_log(
                        row_number=row_num,
                        status="WORKER_ERROR",
                        log_type="COMBINED",
                        message=str(e)
                    )
                except Exception:
                    pass

    print("✅ Finished combined video + image + text scraping")


if __name__ == "__main__":
    run_parallel_combined_scraper(max_workers=MAX_WORKERS)

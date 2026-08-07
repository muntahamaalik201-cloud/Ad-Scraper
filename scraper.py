# Combined Google Ads Transparency scraper
# V8: preserve V6 text/image logic; validate YouTube IDs from the active visible player only.
# Video-ad detection logic is kept from the original scrapper.txt.
# Non-video ads use text/image extraction + package matching from the uploaded non-video files.

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from urllib.parse import urlparse, parse_qs, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo
import difflib
import re

def get_best_matching_package_for_text_ad(headline, description, package_list, min_score=0.70):
    """Matches package names with headline + description using character-level comparison."""
    import difflib
    def clean_text_for_comparison(text):
        if not text or text == "N/A":
            return ""
        return re.sub(r"[^a-z0-9]", "", text.lower())

    ad_text = clean_text_for_comparison(str(headline) + str(description))

    best_pkg = None
    best_score = 0.0

    for pkg in package_list:
        pkg_clean = clean_text_for_comparison(pkg)
        if not pkg_clean:
            continue
        ratio = difflib.SequenceMatcher(None, ad_text, pkg_clean).ratio()
        if ratio > best_score:
            best_score = ratio
            best_pkg = pkg

    if best_score >= min_score:
        return best_pkg, best_score
    return None, best_score

import time
import threading
import sheets


MAX_WORKERS = 2
SHEET_LOCK = threading.Lock()

PAKISTAN_TZ = ZoneInfo("Asia/Karachi")
TRANSIENT_HTTP_STATUSES = {403, 429, 500, 502, 503, 504}
NAVIGATION_MAX_ATTEMPTS = 4
NAVIGATION_BACKOFF_SECONDS = (3, 7, 15, 30)
CREATIVE_CLASSIFY_TIMEOUT_SECONDS = 8
IMAGE_TEXT_GRACE_SECONDS = 2.0
AMBIGUOUS_VIDEO_DETECTION_SECONDS = 8
VIDEO_PROBE_BEFORE_IMAGE_SECONDS = 5
VIDEO_PROBE_BEFORE_TEXT_SECONDS = 5
PLAYWRIGHT_ACTION_TIMEOUT_MS = 4000

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".m3u8")

INSTALL_SELECTORS = [
    "a.install-button-anchor.svg-anchor",
    "a.install-button-anchor",
    'a[data-asoch-targets-ad-objective-type]',
    'a:has-text("Install")',
    'a:has-text("Get")',
    'a:has-text("Download")',
]


def safe_update_combined_row(row_num, data):
    """
    Thread-safe Google Sheet row update.
    Browser scraping runs parallel, but sheet writing is protected.
    """
    with SHEET_LOCK:
        sheets.update_combined_row(row_num, data)


def safe_update_headline_desc(row_num, headline, description):
    """
    Thread-safe Google Sheet row update for Headline and Description in cols M and N.
    """
    with SHEET_LOCK:
        sheets.update_headline_and_description(row_num, headline, description)


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


def safe_mark_agent_retry(row_num, status):
    """Keep transient failures unprocessed so a later pass can retry them."""
    with SHEET_LOCK:
        retry_fn = getattr(sheets, "mark_agent_retry", None)
        if retry_fn:
            retry_fn(row_num, status)
        else:
            try:
                sheets.get_sheet().update_cell(row_num, 12, status)
            except Exception:
                pass


def get_exact_time():
    """Current Pakistan Standard Time (UTC+05:00)."""
    return datetime.now(PAKISTAN_TZ).strftime("%I:%M:%S %p")


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
    """True only for an actual media response, never for thumbnails/player JSON."""
    try:
        url = str(response.url or "").lower()
        content_type = str(response.headers.get("content-type", "") or "").lower()

        if content_type.startswith("video/"):
            return True
        if "application/vnd.apple.mpegurl" in content_type:
            return True
        if "application/x-mpegurl" in content_type:
            return True
        if "videoplayback" in url or "googlevideo.com" in url:
            return True
        if any(ext in url for ext in VIDEO_EXTENSIONS):
            return True
    except Exception:
        pass
    return False


def extract_video_id_from_url(req_url):
    """
    Extract a video ID only when the URL itself explicitly identifies the video.

    IMPORTANT:
    - YouTube thumbnails are NOT accepted here.
    - videoplayback id/itag/ei/source are NOT accepted as YouTube video IDs.
    - The active YouTube player is queried separately for its real 11-char ID.
    """
    try:
        raw_url = str(req_url or "")
        url_lower = raw_url.lower()
        parsed = urlparse(raw_url)
        query = parse_qs(parsed.query)

        patterns = [
            r"youtube(?:-nocookie)?\.com/embed/([A-Za-z0-9_-]{11})",
            r"youtube\.com/shorts/([A-Za-z0-9_-]{11})",
            r"youtube\.com/v/([A-Za-z0-9_-]{11})",
            r"youtu\.be/([A-Za-z0-9_-]{11})",
        ]
        for pattern in patterns:
            match = re.search(pattern, raw_url, re.IGNORECASE)
            if match:
                return match.group(1)

        if "youtube.com/watch" in url_lower:
            value = query.get("v", [None])[0]
            if value and re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
                return value

        # A googlevideo/videoplayback URL proves video activity but its `id`
        # parameter is often a playback identifier, not the public YouTube ID.
        if "videoplayback" in url_lower or "googlevideo.com" in url_lower:
            return None

        # Preserve direct-file behavior for non-YouTube hosted videos.
        for ext in VIDEO_EXTENSIONS:
            if ext in url_lower:
                filename = parsed.path.split("/")[-1].split("?")[0].strip()
                if filename:
                    return filename
    except Exception:
        return None

    return None


def extract_video_id_from_json_payload(payload):
    """Read only the top-level videoId from a YouTube PLAYER REQUEST."""
    try:
        if isinstance(payload, dict):
            for key in ("videoId", "video_id"):
                value = payload.get(key)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
                    return value
    except Exception:
        pass
    return None


def _frame_visible_for_video(frame, page, min_width=80, min_height=45):
    """Check that a frame is actually rendered as part of the current creative."""
    try:
        if frame == page.main_frame:
            return False
        element = frame.frame_element()
        box = element.bounding_box(timeout=1200)
        if not box:
            return False
        return (box.get("width", 0) or 0) >= min_width and (box.get("height", 0) or 0) >= min_height
    except Exception:
        return False


def _frame_has_video_player(frame):
    """Require a real player/video surface before trusting IDs inside a frame."""
    js = r"""
    () => {
        const visible = (el) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width >= 80 && r.height >= 45 &&
                   s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
        };

        const player = document.querySelector('#movie_player, .html5-video-player, [class*="video-player"], [class*="videoplayer"]');
        if (player && visible(player)) return true;

        if (Array.from(document.querySelectorAll('video')).some(visible)) return true;

        return Array.from(document.querySelectorAll('button, [role="button"], [aria-label], [title]')).some(el => {
            if (!visible(el)) return false;
            const aria = String(el.getAttribute('aria-label') || '').toLowerCase();
            const title = String(el.getAttribute('title') || '').toLowerCase();
            const cls = String(el.className || '').toLowerCase();
            return aria.includes('play') || title.includes('play') || cls.includes('play-button') || cls.includes('playbutton');
        });
    }
    """
    try:
        return bool(frame.evaluate(js))
    except Exception:
        return False


def extract_active_youtube_player_id(page):
    """
    Get the YouTube ID from the ACTIVE visible player only.

    This is the authoritative YouTube-ID path. It avoids unrelated thumbnails,
    hidden templates and recursive player-response metadata.
    """
    js = r"""
    () => {
        const valid = (v) => typeof v === 'string' && /^[A-Za-z0-9_-]{11}$/.test(v);

        try {
            const player = document.querySelector('#movie_player');
            if (player && typeof player.getVideoData === 'function') {
                const data = player.getVideoData();
                if (data && valid(data.video_id)) return data.video_id;
            }
        } catch (e) {}

        try {
            const r = window.ytInitialPlayerResponse;
            if (r && r.videoDetails && valid(r.videoDetails.videoId)) return r.videoDetails.videoId;
        } catch (e) {}

        try {
            const args = window.ytplayer && window.ytplayer.config && window.ytplayer.config.args;
            if (args && valid(args.video_id)) return args.video_id;
        } catch (e) {}

        // Poster/thumbnail is accepted ONLY inside this already-verified visible player frame.
        for (const el of document.querySelectorAll('video[poster], img[src]')) {
            const value = String(el.poster || el.src || el.getAttribute('poster') || el.getAttribute('src') || '');
            const m = value.match(/(?:i\.ytimg\.com|img\.youtube\.com)\/(?:vi|vi_webp)\/([A-Za-z0-9_-]{11})/i);
            if (m) return m[1];
        }

        return null;
    }
    """

    # Prefer explicit YouTube player frames, then any visible frame with a real video surface.
    candidates = []
    for frame in page.frames:
        if not _frame_visible_for_video(frame, page):
            continue
        url = str(frame.url or '').lower()
        priority = 0 if ('youtube.com' in url or 'youtube-nocookie.com' in url) else 1
        candidates.append((priority, frame))

    candidates.sort(key=lambda x: x[0])

    for _, frame in candidates:
        try:
            explicit = extract_video_id_from_url(frame.url)
            if explicit:
                return explicit
        except Exception:
            pass

        if not _frame_has_video_player(frame):
            continue

        try:
            value = frame.evaluate(js)
            if value and re.fullmatch(r"[A-Za-z0-9_-]{11}", str(value)):
                return str(value)
        except Exception:
            continue

    return "N/A"


def extract_validated_player_request_id(page, captured):
    """Use a player-request videoId only if its originating frame is still a visible video player."""
    candidates = captured.get("_youtube_player_requests", []) or []
    for video_id, frame in reversed(candidates):
        try:
            if frame not in page.frames:
                continue
            if not _frame_visible_for_video(frame, page):
                continue
            if not _frame_has_video_player(frame):
                continue
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id)):
                return str(video_id)
        except Exception:
            continue
    return "N/A"


def scan_embedded_video_metadata(page):
    """Only inspect the active visible player; never scan arbitrary page HTML."""
    return extract_active_youtube_player_id(page)


def extract_video_from_dom(page):
    """Read video IDs only from visible player/video DOM."""
    youtube_id = extract_active_youtube_player_id(page)
    if youtube_id != "N/A":
        return youtube_id

    for frame in page.frames:
        if frame != page.main_frame and not _frame_visible_for_video(frame, page):
            continue
        try:
            urls = frame.evaluate(r"""
                () => {
                    const visible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        return r.width > 0 && r.height > 0 &&
                               s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                    };
                    return Array.from(document.querySelectorAll('video, source, iframe'))
                        .filter(el => el.tagName.toLowerCase() === 'source' || visible(el))
                        .flatMap(el => [el.currentSrc || '', el.src || '', el.getAttribute('src') || ''])
                        .filter(Boolean);
                }
            """)
            for src in urls:
                found = extract_video_id_from_url(str(src))
                if found:
                    return found
        except Exception:
            continue

    return "N/A"


def scan_browser_performance_for_video(page):
    """Use resource entries as video evidence, but get YouTube ID from the active player."""
    saw_youtube_media = False

    for frame in page.frames:
        if frame == page.main_frame or not _frame_visible_for_video(frame, page):
            continue
        try:
            urls = frame.evaluate("() => performance.getEntriesByType('resource').map(r => r.name)")
            for u in urls:
                u = str(u)
                lower = u.lower()

                direct_id = extract_video_id_from_url(u)
                if direct_id:
                    return direct_id

                if "videoplayback" in lower or "googlevideo.com" in lower:
                    saw_youtube_media = True
        except Exception:
            continue

    if saw_youtube_media:
        found = extract_active_youtube_player_id(page)
        if found != "N/A":
            return found

    return "N/A"

def click_possible_video_targets(page):
    """
    Click a real play/video control first, including controls inside ad frames.
    If the creative hides the control, click the largest visible creative iframe
    once as a bounded fallback. This prevents video ads being saved as image.
    """
    selectors = [
        "video",
        'button[aria-label*="Play" i]',
        'button[title*="Play" i]',
        'div[aria-label*="Play" i]',
        '[role="button"][aria-label*="Play" i]',
        '[class*="play-button" i]',
        '[class*="playbutton" i]',
        'img[src*="play" i]',
    ]

    targets = [page] + [frame for frame in page.frames if frame != page.main_frame]

    for target in targets:
        for sel in selectors:
            try:
                elements = target.locator(sel)
                count = min(elements.count(), 10)

                for i in range(count):
                    el = elements.nth(i)
                    if not el.is_visible():
                        continue

                    try:
                        el.scroll_into_view_if_needed(timeout=1500)
                        box = el.bounding_box(timeout=1500)
                        if not box:
                            continue

                        # Play buttons can be small. Actual video surfaces must be larger.
                        if sel == "video":
                            if box["width"] < 120 or box["height"] < 80:
                                continue
                        elif box["width"] < 20 or box["height"] < 20:
                            continue

                        el.click(timeout=2000, force=True)
                        page.wait_for_timeout(1000)
                        return True
                    except Exception:
                        continue
            except Exception:
                continue

    # Fallback for creatives where the play control is hidden inside a cross-origin iframe.
    iframe_candidates = []
    try:
        iframes = page.locator("iframe")
        for i in range(min(iframes.count(), 20)):
            try:
                iframe = iframes.nth(i)
                if not iframe.is_visible():
                    continue
                box = iframe.bounding_box(timeout=1200)
                if not box:
                    continue
                width = box.get("width", 0) or 0
                height = box.get("height", 0) or 0
                y = box.get("y", 99999) or 99999
                if width < 160 or height < 100 or y < -100 or y > 1000:
                    continue
                iframe_candidates.append((width * height, iframe, box))
            except Exception:
                continue
    except Exception:
        iframe_candidates = []

    if iframe_candidates:
        iframe_candidates.sort(key=lambda item: item[0], reverse=True)
        _, iframe, box = iframe_candidates[0]
        try:
            page.mouse.click(
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
            )
            page.wait_for_timeout(1000)
            return True
        except Exception:
            pass

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



def probe_video_before_image(page, captured, max_seconds=VIDEO_PROBE_BEFORE_IMAGE_SECONDS):
    """
    One bounded video probe before a static creative is labelled ``image``.
    It preserves fast image handling but gives video ads enough time to start
    and expose their real video ID.
    """
    video_id = get_immediate_video_id(page, captured)
    if video_id != "N/A":
        return video_id

    click_possible_video_targets(page)
    video_id = wait_for_video_id(page, captured, max_seconds=max_seconds)
    if video_id != "N/A":
        return video_id

    return scan_browser_performance_for_video(page)


def probe_video_before_text(page, captured, max_seconds=VIDEO_PROBE_BEFORE_TEXT_SECONDS):
    """
    Confirm that a creative with visible ad copy is not actually a video ad.
    Video creatives commonly expose headline/description before playback starts.
    """
    started = time.time()

    video_id = get_immediate_video_id(page, captured)
    if video_id != "N/A":
        return video_id

    # When a play/video hint is visible, activate only the real play target.
    if has_video_hint(page):
        click_possible_video_targets(page)

    remaining = max_seconds - (time.time() - started)
    if remaining > 0:
        video_id = wait_for_video_id(page, captured, max_seconds=remaining)
        if video_id != "N/A":
            return video_id

    return get_immediate_video_id(page, captured)


class TransientHTTPError(RuntimeError):
    def __init__(self, status, url, message=None):
        self.status = status
        self.url = url
        label = f"HTTP {status}" if status else "temporary network failure"
        super().__init__(message or f"{label} while opening {url}")


def _is_transient_navigation_error(exc):
    text = str(exc).lower()
    markers = (
        "403", "429", "500", "502", "503", "504",
        "err_connection_reset", "err_connection_closed",
        "err_timed_out", "err_network_changed", "timeout"
    )
    return any(marker in text for marker in markers)


def navigate_with_retry(page, url, row_num, max_attempts=NAVIGATION_MAX_ATTEMPTS):
    """Retry temporary Google 403/503/429/5xx and browser network failures."""
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            status = response.status if response else None

            if status in TRANSIENT_HTTP_STATUSES:
                raise TransientHTTPError(status, url)

            if status is not None and status >= 400:
                raise RuntimeError(f"HTTP {status} while opening {url}")

            # Smaller initial wait; later extraction functions poll only when needed.
            page.wait_for_timeout(2500)
            return response

        except Exception as exc:
            last_error = exc
            transient = (
                isinstance(exc, (TransientHTTPError, PlaywrightTimeoutError))
                or _is_transient_navigation_error(exc)
            )

            if not transient:
                raise

            if attempt >= max_attempts:
                status = getattr(exc, "status", None)
                raise TransientHTTPError(status, url, str(exc)) from exc

            wait_seconds = NAVIGATION_BACKOFF_SECONDS[min(attempt - 1, len(NAVIGATION_BACKOFF_SECONDS) - 1)]
            print(
                f"⚠️ Row {row_num}: temporary page error on attempt "
                f"{attempt}/{max_attempts}: {exc}; retrying in {wait_seconds}s"
            )

            try:
                page.goto("about:blank", wait_until="commit", timeout=10000)
            except Exception:
                pass

            page.wait_for_timeout(wait_seconds * 1000)

    raise TransientHTTPError(None, url, str(last_error))



def get_immediate_video_id(page, captured):
    """Fast ID lookup, trusting only the active player or validated direct-media IDs."""
    # First query the current visible player. This prevents a stale request ID
    # from another hidden creative winning.
    video_id = extract_active_youtube_player_id(page)
    if video_id != "N/A":
        captured["video_id"] = video_id
        return video_id

    request_id = extract_validated_player_request_id(page, captured)
    if request_id != "N/A":
        captured["video_id"] = request_id
        return request_id

    captured_id = captured.get("video_id", "N/A")
    if captured_id and captured_id != "N/A":
        return captured_id

    video_id = extract_video_from_dom(page)
    if video_id != "N/A":
        return video_id

    return scan_browser_performance_for_video(page)


def has_video_hint(page):
    """Detect only visible player/video evidence. Hidden thumbnails do not count."""
    # An actual active YouTube player is the strongest hint.
    if extract_active_youtube_player_id(page) != "N/A":
        return True

    js = r"""
    () => {
        const visible = (el) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > 20 && r.height > 20 &&
                   s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
        };

        if (Array.from(document.querySelectorAll('video')).some(visible)) return true;

        for (const el of document.querySelectorAll('iframe[src]')) {
            if (!visible(el)) continue;
            const value = String(el.src || el.getAttribute('src') || '').toLowerCase();
            if (value.includes('youtube.com/embed/') || value.includes('youtube-nocookie.com/embed/')) return true;
        }

        for (const el of document.querySelectorAll('button, [role="button"], [aria-label], [title], source[type]')) {
            const tag = String(el.tagName || '').toLowerCase();
            const aria = String(el.getAttribute('aria-label') || '').toLowerCase();
            const title = String(el.getAttribute('title') || '').toLowerCase();
            const cls = String(el.className || '').toLowerCase();
            const type = String(el.getAttribute('type') || '').toLowerCase();
            const txt = String(el.innerText || el.textContent || '').trim().toLowerCase();
            if (type.startsWith('video/')) return true;
            if (!visible(el) && tag !== 'source') continue;
            if (aria.includes('play') || title.includes('play') ||
                cls.includes('play-button') || cls.includes('playbutton') ||
                cls.includes('video-player') || cls.includes('videoplayer') ||
                txt === 'play') return true;
        }
        return false;
    }
    """

    for target in [page] + [f for f in page.frames if f != page.main_frame]:
        try:
            if target.evaluate(js):
                return True
        except Exception:
            continue
    return False

def detect_video_id(page, captured, max_total_seconds=AMBIGUOUS_VIDEO_DETECTION_SECONDS):
    """
    Bounded video detection.

    Static image/text ads no longer wait 25+ seconds. Full clicking/waiting is
    used only when the creative exposes a real video/play hint.
    """
    started = time.time()

    video_id = get_immediate_video_id(page, captured)
    if video_id != "N/A":
        return video_id

    hint = has_video_hint(page)

    # No video evidence: allow only a short network grace period.
    if not hint:
        remaining = max_total_seconds - (time.time() - started)
        if remaining > 0:
            return wait_for_video_id(page, captured, max_seconds=min(2, remaining))
        return "N/A"

    clicked = click_possible_video_targets(page)
    remaining = max_total_seconds - (time.time() - started)
    if remaining > 0:
        video_id = wait_for_video_id(
            page,
            captured,
            max_seconds=min(5 if clicked else 3, remaining)
        )

    if video_id == "N/A":
        video_id = scan_browser_performance_for_video(page)

    remaining = max_total_seconds - (time.time() - started)
    if video_id == "N/A" and remaining > 1:
        page.mouse.wheel(0, 350)
        page.wait_for_timeout(500)
        clicked = click_possible_video_targets(page)
        remaining = max_total_seconds - (time.time() - started)
        if clicked and remaining > 0:
            video_id = wait_for_video_id(page, captured, max_seconds=min(3, remaining))

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
    parts = pkg.split('.')
    if len(parts) < 3 or len(pkg) < 8:  return False
    if _SKIP_EXT.search(pkg):            return False
    if _SKIP_PFX.match(pkg):             return False
    for p in parts:
        if not p or not re.match(r'^[A-Za-z][A-Za-z0-9_]*$', p):
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


def extract_text_ad_details_once(page):
    """
    Strict text-ad probe.

    A row is considered a text ad only when the active creative contains the
    same explicit text-ad structures used by the previous working scraper.
    Generic ``headline``/``description`` class matches are intentionally not
    used because static image creatives can contain those words in wrappers,
    metadata, accessibility nodes, or stale template frames.

    A dominant raster/canvas/background creative suppresses text detection, so
    text printed inside an image banner does not turn that image ad into a text
    ad.
    """
    js = r"""
    () => {
        const clean = (value) => String(value || '')
            .replace(/\n/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();

        const visible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 &&
                   style.display !== 'none' &&
                   style.visibility !== 'hidden' &&
                   style.opacity !== '0';
        };

        const bad = (text) => {
            const t = clean(text).toLowerCase();
            if (!t || t.length < 2 || t.includes('{{') || t.includes('}}')) {
                return true;
            }
            return [
                'ads transparency center', 'ads transparency centre',
                'see more ads', 'report this ad', 'sign in', 'last shown',
                'about this ad', 'why this ad', 'ad details'
            ].some(x => t === x || t.startsWith(x));
        };

        const viewportWidth = Math.max(
            document.documentElement ? document.documentElement.clientWidth : 0,
            window.innerWidth || 0
        );
        const viewportHeight = Math.max(
            document.documentElement ? document.documentElement.clientHeight : 0,
            window.innerHeight || 0
        );
        const viewportArea = Math.max(viewportWidth * viewportHeight, 1);

        // Static image ads normally contain one visual that occupies most of
        // the creative frame. Small app icons/logos do not trigger this.
        let dominantImage = false;

        for (const el of document.querySelectorAll('img, picture, canvas')) {
            if (!visible(el)) continue;
            const rect = el.getBoundingClientRect();
            const src = String(el.getAttribute('src') || '').toLowerCase();
            const alt = String(el.getAttribute('alt') || '').toLowerCase();
            if (src.includes('googlelogo') || alt.includes('google')) continue;

            const coverage = (rect.width * rect.height) / viewportArea;
            if (
                rect.width >= 180 && rect.height >= 90 &&
                (coverage >= 0.45 || (rect.width * rect.height) >= 90000)
            ) {
                dominantImage = true;
                break;
            }
        }

        if (!dominantImage) {
            for (const el of document.querySelectorAll('div, a, section')) {
                if (!visible(el)) continue;
                const rect = el.getBoundingClientRect();
                const bg = window.getComputedStyle(el).backgroundImage || '';
                if (!bg || bg === 'none' || !bg.includes('url(')) continue;
                if (bg.toLowerCase().includes('googlelogo')) continue;

                const coverage = (rect.width * rect.height) / viewportArea;
                if (
                    rect.width >= 180 && rect.height >= 90 &&
                    (coverage >= 0.45 || (rect.width * rect.height) >= 90000)
                ) {
                    dominantImage = true;
                    break;
                }
            }
        }

        const firstText = (selectors, minLen, maxLen, excludedText = '') => {
            for (const selector of selectors) {
                for (const el of document.querySelectorAll(selector)) {
                    if (!visible(el)) continue;
                    const text = clean(el.innerText || el.textContent || '');
                    if (text === excludedText) continue;
                    if (text.length < minLen || text.length > maxLen || bad(text)) {
                        continue;
                    }
                    return text;
                }
            }
            return 'N/A';
        };

        const descriptionBelowHeadline = (headlineText) => {
            if (!headlineText || headlineText === 'N/A') return 'N/A';

            let headlineEl = null;
            for (const selector of [
                '[class*="-e-15"]',
                'div[role="link"] > span',
                'div[role="link"] span',
                'div.cS4Vcb-vnv8ic'
            ]) {
                for (const el of document.querySelectorAll(selector)) {
                    if (!visible(el)) continue;
                    const text = clean(el.innerText || el.textContent || '');
                    if (text === headlineText) {
                        headlineEl = el;
                        break;
                    }
                }
                if (headlineEl) break;
            }

            if (!headlineEl) return 'N/A';
            const headRect = headlineEl.getBoundingClientRect();
            const headStyle = window.getComputedStyle(headlineEl);
            const headFont = parseFloat(headStyle.fontSize || '0') || 0;
            const candidates = [];

            for (const el of document.querySelectorAll('div, span, p')) {
                if (el === headlineEl || el.childElementCount > 0 || !visible(el)) continue;
                const text = clean(el.innerText || el.textContent || '');
                if (text === headlineText || text.length < 5 || text.length > 320 || bad(text)) continue;
                const lower = text.toLowerCase();
                if (lower === 'google play' || lower === 'install' || lower === 'download' || lower === 'get') continue;

                const rect = el.getBoundingClientRect();
                const verticalGap = rect.top - headRect.bottom;
                const horizontalOverlap = Math.min(rect.right, headRect.right) - Math.max(rect.left, headRect.left);
                if (verticalGap < -2 || verticalGap > 140 || horizontalOverlap < 10) continue;

                const font = parseFloat(window.getComputedStyle(el).fontSize || '0') || 0;
                // The description shown under the marked headline is normally
                // smaller or equal in size and spatially closest below it.
                if (headFont > 0 && font > headFont + 1) continue;
                candidates.push({text, gap: Math.max(verticalGap, 0), font});
            }

            candidates.sort((a, b) => a.gap - b.gap || b.text.length - a.text.length);
            return candidates.length ? candidates[0].text : 'N/A';
        };

        // Keep only the specific structures from the previous working logic.
        // Do not use broad selectors such as [class*="headline"] or
        // [class*="description"].
        let headline = firstText([
            '[class*="-e-15"]',
            'div[role="link"] > span',
            'div[role="link"] span',
            'div.cS4Vcb-vnv8ic'
        ], 3, 180);

        let description = firstText([
            '[class*="-e-67"]',
            'div.HFTpmd-WsjYwc-hgDUwe',
            'div.cS4Vcb-vnv8ic'
        ], 5, 320, headline);

        if (description === 'N/A') {
            description = descriptionBelowHeadline(headline);
        }
        if (description === headline) description = 'N/A';

        // A dominant image wins over text-like metadata/accessibility content.
        if (dominantImage) {
            return {
                headline: 'N/A',
                description: 'N/A',
                score: 0,
                dominantImage: true
            };
        }

        let score = 0;
        if (headline !== 'N/A') score += 120;
        if (description !== 'N/A') score += 100;

        return {headline, description, score, dominantImage: false};
    }
    """

    candidates = []

    # Inspect only visible, reasonably sized creative frames. Hidden/stale
    # frames are a common source of false text classifications.
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            box = _frame_parent_box(frame)
            if not box:
                continue
            width = box.get("width", 0) or 0
            height = box.get("height", 0) or 0
            y = box.get("y", 99999) or 99999
            if width < 100 or height < 60 or y > 1400:
                continue

            result = frame.evaluate(js)
            if result and result.get("score", 0) > 0:
                area_bonus = min((width * height) / 10000, 50)
                candidates.append((result.get("score", 0) + area_bonus, result))
        except Exception:
            continue

    # Main page fallback is allowed only for strict text-ad structures and only
    # when it is not dominated by an image. The Google shell is excluded.
    if not candidates:
        try:
            body_text = page.evaluate(
                "() => document.body ? document.body.innerText.toLowerCase() : ''"
            )
            shell_page = (
                "ads transparency center" in body_text
                or "ads transparency centre" in body_text
            )
            result = page.evaluate(js)
            if (
                not shell_page
                and result
                and result.get("score", 0) > 0
                and not result.get("dominantImage", False)
            ):
                candidates.append((result.get("score", 0), result))
        except Exception:
            pass

    if not candidates:
        return {"headline": "N/A", "description": "N/A"}

    candidates.sort(key=lambda item: item[0], reverse=True)
    best = candidates[0][1]
    return {
        "headline": clean_text(best.get("headline")),
        "description": clean_text(best.get("description")),
    }

def wait_and_extract_text_ad_details(page, max_wait_seconds=15):
    """Poll the fast text probe for a bounded amount of time."""
    deadline = time.time() + max(0, max_wait_seconds)

    while True:
        result = extract_text_ad_details_once(page)
        if is_valid_text_ad(result.get("headline"), result.get("description")):
            return result

        if time.time() >= deadline:
            return {"headline": "N/A", "description": "N/A"}

        page.wait_for_timeout(500)


# =========================
# MAIN COMBINED SCRAPER: VIDEO ADS + TEXT ADS
# =========================

def is_valid_text_ad(headline, description):
    if headline and headline != "N/A" and len(clean_text(headline)) >= 3:
        return True
    if description and description != "N/A" and len(clean_text(description)) >= 15:
        return True
    return False

def has_visible_image_creative(page):
    """
    Detect a real static image creative, not Google shell SVGs or small app icons.

    Only large raster/canvas/background artwork inside visible creative frames
    counts. Text extraction always gets priority before this result is used.
    """
    js = r"""
    () => {
        const visible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width >= 200 && rect.height >= 100 &&
                   (rect.width * rect.height) >= 40000 &&
                   style.display !== 'none' &&
                   style.visibility !== 'hidden' &&
                   style.opacity !== '0';
        };

        for (const el of document.querySelectorAll('img, picture, canvas')) {
            if (!visible(el)) continue;
            const src = String(el.getAttribute('src') || '').toLowerCase();
            const alt = String(el.getAttribute('alt') || '').toLowerCase();
            if (src.includes('googlelogo') || alt.includes('google')) continue;
            return true;
        }

        for (const el of document.querySelectorAll('div, a, section')) {
            if (!visible(el)) continue;
            const bg = window.getComputedStyle(el).backgroundImage || '';
            if (bg && bg !== 'none' && bg.includes('url(') && !bg.toLowerCase().includes('googlelogo')) {
                return true;
            }
        }

        return false;
    }
    """

    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            box = _frame_parent_box(frame)
            if not box:
                continue
            if (box.get("width", 0) or 0) < 120 or (box.get("height", 0) or 0) < 70:
                continue
            if frame.evaluate(js):
                return True
        except Exception:
            continue

    return False



def has_any_visible_image_creative(page):
    """Final fallback for image creatives that use smaller images/SVG/CSS artwork."""
    js = r"""
    () => {
        const visible = (el) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width >= 120 && r.height >= 80 &&
                   r.bottom > 0 && r.right > 0 &&
                   s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
        };

        for (const el of document.querySelectorAll('img, picture, canvas, svg')) {
            if (!visible(el)) continue;
            const src = String(el.getAttribute('src') || '').toLowerCase();
            const alt = String(el.getAttribute('alt') || '').toLowerCase();
            if (src.includes('googlelogo') || alt.includes('google')) continue;
            return true;
        }

        for (const el of document.querySelectorAll('div, a, section')) {
            if (!visible(el)) continue;
            const bg = String(window.getComputedStyle(el).backgroundImage || '');
            if (bg && bg !== 'none' && bg.includes('url(') && !bg.toLowerCase().includes('googlelogo')) return true;
        }
        return false;
    }
    """

    # Creative frames only; do not classify Google Transparency shell artwork as an ad.
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            box = _frame_parent_box(frame)
            if not box:
                continue
            if (box.get('width', 0) or 0) < 100 or (box.get('height', 0) or 0) < 60:
                continue
            if frame.evaluate(js):
                return True
        except Exception:
            continue
    return False

def classify_creative(page, captured, max_wait_seconds=CREATIVE_CLASSIFY_TIMEOUT_SECONDS):
    """Return (kind, video_id, headline, description) with video checked before text."""
    deadline = time.time() + max_wait_seconds
    first_image_seen_at = None
    video_probe_done = False
    text_video_probe_done = False
    last_headline = "N/A"
    last_description = "N/A"

    while time.time() < deadline:
        video_id = get_immediate_video_id(page, captured)
        if video_id != "N/A":
            return "video", video_id, "N/A", "N/A"

        text_data = extract_text_ad_details_once(page)
        headline = clean_text(text_data.get("headline"))
        description = clean_text(text_data.get("description"))
        has_text = is_valid_text_ad(headline, description)
        last_headline, last_description = headline, description

        # Headline/description can belong to a VIDEO ad. Never write "text"
        # until a bounded video-ID probe has completed.
        if has_text:
            if not text_video_probe_done:
                text_video_probe_done = True
                video_id = probe_video_before_text(page, captured)
                if video_id != "N/A":
                    return "video", video_id, "N/A", "N/A"

            # If a real visible player/play surface is still present, do NOT
            # downgrade the creative to text just because its ad copy loaded first.
            if has_video_hint(page):
                page.wait_for_timeout(500)
                continue

            return "text", "N/A", headline, description

        image_like = has_visible_image_creative(page)
        if image_like:
            if first_image_seen_at is None:
                first_image_seen_at = time.time()
            elif time.time() - first_image_seen_at >= IMAGE_TEXT_GRACE_SECONDS:
                if not video_probe_done:
                    video_probe_done = True
                    video_id = probe_video_before_image(page, captured)
                    if video_id != "N/A":
                        return "video", video_id, "N/A", "N/A"

                    text_data = extract_text_ad_details_once(page)
                    headline = clean_text(text_data.get("headline"))
                    description = clean_text(text_data.get("description"))
                    if is_valid_text_ad(headline, description):
                        if not text_video_probe_done:
                            text_video_probe_done = True
                            video_id = probe_video_before_text(page, captured)
                            if video_id != "N/A":
                                return "video", video_id, "N/A", "N/A"
                        return "text", "N/A", headline, description

                # Same protection for video creatives that visually look like
                # a poster/image before playback exposes the ID.
                if has_video_hint(page):
                    page.wait_for_timeout(500)
                    continue

                return "image", "N/A", "N/A", "N/A"
        else:
            first_image_seen_at = None

        page.wait_for_timeout(500)

    video_id = detect_video_id(page, captured, max_total_seconds=AMBIGUOUS_VIDEO_DETECTION_SECONDS)
    if video_id != "N/A":
        return "video", video_id, "N/A", "N/A"

    text_data = extract_text_ad_details_once(page)
    headline = clean_text(text_data.get("headline"))
    description = clean_text(text_data.get("description"))
    if is_valid_text_ad(headline, description):
        if not text_video_probe_done:
            video_id = probe_video_before_text(page, captured)
            if video_id != "N/A":
                return "video", video_id, "N/A", "N/A"
        if not has_video_hint(page):
            return "text", "N/A", headline, description

    if has_visible_image_creative(page):
        if not video_probe_done:
            video_id = probe_video_before_image(page, captured)
            if video_id != "N/A":
                return "video", video_id, "N/A", "N/A"
        if not has_video_hint(page):
            return "image", "N/A", "N/A", "N/A"

    # Last-resort image fallback: only after video and text have both failed.
    # This restores image ads that use SVG/smaller/CSS artwork instead of a large raster.
    if has_any_visible_image_creative(page) and not has_video_hint(page):
        return "image", "N/A", "N/A", "N/A"

    return "unknown", "N/A", last_headline, last_description

def save_fast_image_ad(page, row_num, url, advertiser):
    """
    Save an image row without processing its app link, package, or ad text.

    This is intentionally minimal: column F receives only ``image`` and the
    worker immediately moves to the next row.
    """
    process_time = get_exact_time()
    data = [
        advertiser,
        "N/A",
        url,
        "N/A",
        process_time,
        "image",
        process_time,
    ]

    safe_update_combined_row(row_num, data)
    safe_update_headline_desc(row_num, "N/A", "N/A")
    safe_add_log(
        row_number=row_num,
        status="SUCCESS",
        log_type="FAST_IMAGE_AD",
        url=url,
        video_id="image",
        app_link="N/A",
        message="Static image ad classified and saved without further processing",
    )
    print(f"✅ Row {row_num}: saved IMAGE ad without extra processing")


def scrape_single_url(url_row):
    row_num, url = url_row

    with sync_playwright() as p:
        browser = None
        context = None
        page = None

        try:
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
            context.set_default_timeout(PLAYWRIGHT_ACTION_TIMEOUT_MS)
            context.set_default_navigation_timeout(60000)

            # Fonts are unnecessary for extraction and can materially delay ads.
            try:
                context.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type == "font"
                    else route.continue_(),
                )
            except Exception:
                pass

            page = context.new_page()
            page.set_default_timeout(PLAYWRIGHT_ACTION_TIMEOUT_MS)
            page.set_default_navigation_timeout(60000)
            captured = {"video_id": "N/A", "_youtube_player_requests": []}

            def handle_request(request):
                try:
                    request_url = str(request.url or "")
                    lower = request_url.lower()

                    # Direct explicit IDs / direct video filenames are safe.
                    found_id = extract_video_id_from_url(request_url)
                    if found_id and captured.get("video_id", "N/A") == "N/A":
                        captured["video_id"] = found_id

                    # For YouTube, retain the PLAYER REQUEST's top-level videoId
                    # as a candidate. It is accepted later only if that request's
                    # frame is still a visible real video player.
                    if "youtubei/v1/player" in lower:
                        try:
                            player_id = extract_video_id_from_json_payload(request.post_data_json)
                        except Exception:
                            player_id = None
                        if player_id:
                            items = captured.setdefault("_youtube_player_requests", [])
                            items.append((player_id, request.frame))
                            if len(items) > 12:
                                del items[:-12]
                except Exception:
                    pass

            def handle_response(response):
                try:
                    # Player JSON, thumbnails and unrelated YouTube metadata are
                    # deliberately ignored. Only actual media/direct URLs matter.
                    if not is_real_video_response(response):
                        return
                    found_id = extract_video_id_from_url(response.url)
                    if found_id and captured.get("video_id", "N/A") == "N/A":
                        captured["video_id"] = found_id
                except Exception:
                    pass

            page.on("request", handle_request)
            page.on("response", handle_response)

            if "region=" not in url:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}region=anywhere"

            print(f"🔍 Row {row_num}: opening transparency URL")
            safe_add_log(
                row_number=row_num,
                status="STARTED",
                log_type="COMBINED",
                url=url,
                message="Started combined video/text/image ad extraction",
            )

            navigate_with_retry(page, url, row_num)
            advertiser = extract_advertiser_from_page(page)

            kind, video_id, headline, description = classify_creative(page, captured)

            # =========================
            # IMAGE AD: no extra processing
            # =========================
            if kind == "image":
                save_fast_image_ad(page, row_num, url, advertiser)
                return

            # =========================
            # VIDEO AD: column F = real video ID
            # =========================
            if kind == "video" and video_id != "N/A":
                print(f"🎬 Row {row_num}: video ID found: {video_id}")
                app_link = wait_and_extract_install_link(page, max_wait_seconds=25)
                app_link_time = get_exact_time()
                video_headline, video_description = wait_and_extract_headline_description(
                    page,
                    max_wait_seconds=10,
                )

                if app_link == "N/A":
                    status = "VIDEO_FOUND_APP_LINK_NOT_FOUND"
                    message = "Video ID found, but exact visible install link not found"
                else:
                    status = "SUCCESS"
                    message = "Video ID and app link saved"

                package_name = extract_package_name(app_link)
                data = [
                    advertiser,
                    package_name,
                    url,
                    app_link,
                    app_link_time,
                    video_id,
                    get_exact_time(),
                ]
                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(
                    row_num,
                    clean_text(video_headline),
                    clean_text(video_description),
                )
                safe_add_log(
                    row_number=row_num,
                    status=status,
                    log_type="VIDEO_AD",
                    url=url,
                    video_id=video_id,
                    app_link=app_link,
                    message=message,
                )
                print(f"✅ Row {row_num}: saved VIDEO ad")
                return

            # =========================
            # TEXT AD: column F = text
            # =========================
            if kind == "text" and is_valid_text_ad(headline, description):
                print(f"📄 Row {row_num}: text ad found -> {headline}")
                process_time = get_exact_time()

                visible_app_link = wait_and_extract_install_link(
                    page,
                    max_wait_seconds=5,
                )
                visible_package = extract_package_name(visible_app_link)

                if visible_package != "N/A":
                    package_name = visible_package
                    app_link = visible_app_link
                    status = "SUCCESS"
                    message = "Text ad package extracted from visible install link"
                else:
                    all_found_packages = extract_package_from_page(page)
                    package_name, match_score = get_best_matching_package(
                        headline,
                        description,
                        all_found_packages,
                    )

                    if package_name:
                        app_link = f"https://play.google.com/store/apps/details?id={package_name}"
                        status = "SUCCESS"
                        message = f"Text ad package strictly matched with score {match_score}"
                    else:
                        package_name = "N/A"
                        app_link = "N/A"
                        status = "NON_VIDEO_PACKAGE_NOT_FOUND"
                        message = (
                            "Text ad found, but package score below 0.76. "
                            f"Best score={match_score}"
                        )

                data = [
                    advertiser,
                    package_name,
                    url,
                    app_link,
                    process_time,
                    "text",
                    process_time,
                ]
                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, headline, description)
                safe_add_log(
                    row_number=row_num,
                    status=status,
                    log_type="TEXT_AD",
                    url=url,
                    video_id="text",
                    app_link=app_link,
                    message=message,
                )
                print(f"✅ Row {row_num}: saved TEXT ad")
                return

            # No supported creative could be verified. Keep the old N/A behavior.
            process_time = get_exact_time()
            data = [
                advertiser,
                "N/A",
                url,
                "N/A",
                process_time,
                "N/A",
                process_time,
            ]
            safe_update_combined_row(row_num, data)
            safe_update_headline_desc(row_num, "N/A", "N/A")
            safe_add_log(
                row_number=row_num,
                status="NO_VIDEO_NO_TEXT_IMAGE",
                log_type="COMBINED",
                url=url,
                video_id="N/A",
                app_link="N/A",
                message="No video ID and no valid text/image creative found",
            )
            print(f"⏭ Row {row_num}: no supported creative found")

        except TransientHTTPError as error:
            status_code = error.status or "NETWORK"
            retry_status = f"RETRY_{status_code}"
            print(
                f"⚠️ Row {row_num}: {error}. Leaving row unprocessed and continuing."
            )
            safe_mark_agent_retry(row_num, retry_status)
            safe_add_log(
                row_number=row_num,
                status=retry_status,
                log_type="TRANSIENT_ERROR",
                url=url,
                message=str(error),
            )

        except Exception as error:
            error_time = get_exact_time()
            print(f"❌ Row {row_num} error at {error_time}: {error}")

            # Do not write ERROR into column F. Leave it empty so another pass
            # can retry the row, while the current top/bottom agent continues.
            safe_mark_agent_retry(row_num, "RETRY_RUNTIME")
            safe_add_log(
                row_number=row_num,
                status="RETRY_RUNTIME",
                log_type="COMBINED",
                url=url,
                message=str(error),
            )

        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


def run_parallel_combined_scraper(max_workers=2):
    get_url_rows = getattr(sheets, "get_url_rows_with_retry", None)

    if get_url_rows:
        url_rows = get_url_rows(unprocessed_only=True)
    else:
        urls = sheets.get_urls_with_retry()
        url_rows = [
            (i + 2, u.strip())
            for i, u in enumerate(urls)
            if u and u.strip()
        ]

    if not url_rows:
        print("No transparency URLs found in column H.")
        return

    print(f"🚀 Starting optimized VIDEO + TEXT + FAST IMAGE scraper for {len(url_rows)} rows")
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

    print("✅ Finished optimized video + text + image scraping")


if __name__ == "__main__":
    run_parallel_combined_scraper(max_workers=MAX_WORKERS)

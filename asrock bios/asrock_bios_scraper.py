"""
ASRock BIOS Data Collector
crontab: 0 7 * * * python3 /path/to/asrock_bios_scraper.py

실행 흐름:
  1단계 — Playwright로 메인보드 목록 페이지 파싱 → 전체 모델 리스트 수집
  2단계 — 병렬 BIOS 데이터 수집 (각 모델 제품 페이지의 BIOS 탭 파싱)
  3단계 — 실패 모델 5분 대기 후 1회 재시도
  4단계 — 재시도 후에도 실패 = BIOS 없는 단종/특수 모델
           → asrock_no_bios_models.log 에 영구 기록
"""

import argparse
import json
import os
import re
import time
import random
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
try:
    from playwright_stealth import stealth_sync          # 구버전 (1.x)
except ImportError:
    try:
        from playwright_stealth import Stealth           # 신버전 (2.x)
        def stealth_sync(page):
            Stealth().apply_stealth_sync(page)
    except Exception:
        def stealth_sync(page):                          # 설치 안 됨 → no-op
            pass

try:
    from tqdm import tqdm
    USE_TQDM = True
except ImportError:
    USE_TQDM = False

# ══════════════════════════════════════════════════════════════════
#  설정값
# ══════════════════════════════════════════════════════════════════
CONFIG = {
    "workers":           4,
    "max_retries":       3,
    "delay_min":         0.5,
    "delay_max":         1.5,
    "save_interval":     20,
    "db_save_interval":  100,
    "retry_wait":        300,   # 실패 재시도 전 대기 (초)
    "page_timeout":      20000, # Playwright 페이지 로드 타임아웃 (ms)
    "bios_wait":         5000,  # BIOS 콘텐츠 최대 대기 (ms) — smart wait fallback
    "list_wait":         5000,  # 목록 페이지 JS 로드 대기 (ms)
    "headless":          True,  # False 로 변경 시 브라우저 창 표시
    "debug":             False, # True 시 HTML을 파일로 저장
}

# ══════════════════════════════════════════════════════════════════
#  경로
# ══════════════════════════════════════════════════════════════════
BASE_PATH       = os.path.dirname(os.path.abspath(__file__))
MASTER_FILE     = os.path.join(BASE_PATH, "asrock_motherboards_master.json")
FINAL_JSON      = os.path.join(BASE_PATH, "asrock_bios_data_final.json")
CHECKPOINT_FILE = os.path.join(BASE_PATH, "asrock_checkpoint.json")
NO_BIOS_LOG     = os.path.join(BASE_PATH, "asrock_no_bios_models.log")
DB_FILE         = os.path.join(BASE_PATH, "asrock_bios.db")

# ══════════════════════════════════════════════════════════════════
#  로거
# ══════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_PATH, "asrock_scraper.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  공통 락
# ══════════════════════════════════════════════════════════════════
save_lock       = Lock()
print_lock      = Lock()
checkpoint_lock = Lock()

# ASRock 제품 페이지 기본 URL
BASE_URL    = "https://www.asrock.com"
LISTING_URL = "https://www.asrock.com/mb/index.kr.asp"


# ──────────────────────────────────────────────────────────────────
#  Playwright 브라우저 컨텍스트 생성
# ──────────────────────────────────────────────────────────────────
def make_browser_context(playwright):
    """스텔스 Chromium 브라우저 + 컨텍스트 반환"""
    browser = playwright.chromium.launch(
        headless=CONFIG["headless"],
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="ko-KR",
        timezone_id="Asia/Seoul",
        extra_http_headers={
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer":         "https://www.asrock.com/",
        },
    )
    return browser, context


def new_stealth_page(context):
    """stealth 패치가 적용된 새 페이지 반환 (불필요한 리소스 차단)"""
    page = context.new_page()
    stealth_sync(page)

    # 광고/분석 서드파티 도메인만 차단 (captcha 등 필요한 리소스는 허용)
    BLOCK_DOMAINS = {
        "google-analytics.com", "googletagmanager.com", "doubleclick.net",
        "facebook.com", "twitter.com", "addthis.com", "scorecardresearch.com",
        "omtrdc.net", "2mdn.net", "googlesyndication.com",
    }
    def _block(route):
        if any(d in route.request.url for d in BLOCK_DOMAINS):
            route.abort()
        else:
            route.continue_()
    page.route("**/*", _block)

    return page


def _wait_for_incapsula(page, max_wait: int = 180):
    """
    Incapsula iframe 감지 시 사용자가 captcha를 해결할 때까지 대기.
    headless 모드에서는 경고만 출력.
    """
    def _is_incapsula():
        try:
            return page.evaluate("""() => {
                const iframe = document.querySelector('iframe#main-iframe');
                if (iframe && iframe.src && iframe.src.includes('_Incapsula_Resource')) return true;
                const title = document.title || '';
                return title.toLowerCase().includes('incapsula') ||
                       title.toLowerCase().includes('security check');
            }""")
        except Exception:
            return False

    if not _is_incapsula():
        return

    if CONFIG["headless"]:
        logger.warning("🔒 Incapsula 감지 — headless 모드에서는 해결 불가. --no-headless 로 실행하세요.")
        return

    print("\n" + "="*60)
    print("🔒  브라우저 창에서 captcha를 해결해 주세요.")
    print(f"    최대 {max_wait}초 대기합니다...")
    print("="*60 + "\n")
    logger.warning("🔒 Incapsula captcha 감지 — 브라우저에서 해결해 주세요!")

    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(10)
        if not _is_incapsula():
            logger.info("✅ Incapsula 통과 확인, 계속 진행합니다.")
            page.wait_for_timeout(2000)
            return

    logger.warning("⚠️  Incapsula 대기 시간 초과, 현재 HTML로 계속 진행")


def _wait_for_captcha_clear(page, url: str, max_wait: int = 120):
    """
    hCaptcha / Imperva 차단 감지 시, 사용자가 수동으로 해결할 때까지 대기.
    max_wait 초 안에 해결 안 되면 현재 HTML로 계속 진행.
    """
    CAPTCHA_SIGNALS = ["hcaptcha", "incapsula", "imperva", "additional security"]

    def _is_blocked():
        try:
            # 실제 차단 UI 요소가 화면에 보이는지 확인 (소스에 문자열만 있는 경우 제외)
            blocked = page.evaluate("""() => {
                const title = document.title || '';
                const h1 = document.querySelector('h1,h2');
                const h1text = h1 ? h1.innerText : '';
                const signals = ['additional security', 'security check', 'incapsula', 'imperva'];
                return signals.some(s =>
                    title.toLowerCase().includes(s) ||
                    h1text.toLowerCase().includes(s)
                );
            }""")
            return bool(blocked)
        except Exception:
            return False

    if not _is_blocked():
        return

    # captcha 감지됨
    if not CONFIG["headless"]:
        logger.warning("🔒 hCaptcha 감지 — 브라우저 창에서 체크박스를 클릭해 주세요!")
        print("\n" + "="*60)
        print("🔒  브라우저 창에서 '사람입니다' 체크박스를 클릭하세요.")
        print(f"    최대 {max_wait}초 대기합니다...")
        print("="*60 + "\n")
    else:
        logger.warning("🔒 hCaptcha 감지 — headless 모드에서는 해결 불가. --no-headless 로 실행하세요.")
        return

    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(2)
        if not _is_blocked():
            logger.info("✅ Captcha 통과 확인, 계속 진행합니다.")
            page.wait_for_timeout(2000)  # 페이지 로드 여유
            try:
                page.wait_for_selector("a[href*='/mb/']", timeout=10000)
            except PlaywrightTimeout:
                pass
            return

    logger.warning("⚠️  Captcha 대기 시간 초과, 현재 HTML로 계속 진행")


# ──────────────────────────────────────────────────────────────────
#  HTML 파싱: BIOS 테이블 추출
# ──────────────────────────────────────────────────────────────────
def parse_bios_table(html: str) -> list:
    """
    제품 페이지 HTML에서 BIOS 엔트리 목록 파싱.
    반환: [{"version", "date", "description", "link"}, ...]
    """
    soup = BeautifulSoup(html, "html.parser")
    bios_list = []

    # ── 전략 1: #BIOS 섹션 내의 테이블 파싱 ──
    bios_section = (
        soup.find(id="BIOS")
        or soup.find(id="bios")
        or soup.find("div", class_=re.compile(r"bios", re.I))
    )
    if bios_section:
        bios_list = _extract_from_section(bios_section)

    # ── 전략 2: 전체 페이지에서 download.asrock.com 링크 기반 추출 ──
    if not bios_list:
        bios_list = _extract_from_download_links(soup)

    return bios_list


def _extract_from_section(section) -> list:
    """테이블 행에서 버전/날짜/설명/링크 추출"""
    bios_list = []
    rows = section.find_all("tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        link_tag = row.find("a", href=re.compile(r"download\.asrock\.com.*BIOS", re.I))
        if not link_tag:
            link_tag = row.find("a", href=re.compile(r"\.zip$", re.I))
        if not link_tag:
            continue

        link = link_tag.get("href", "").strip()
        if link.startswith("/"):
            link = BASE_URL + link

        # 버전·날짜는 셀 위치 또는 data-* 속성으로 추출 시도
        texts = [c.get_text(strip=True) for c in cells]
        version     = _find_version(texts, link)
        date        = _find_date(texts)
        description = _find_description(texts, version, date)

        if link:
            bios_list.append({
                "version":     version,
                "date":        date,
                "description": description,
                "link":        link,
            })
    return bios_list


def _extract_from_download_links(soup) -> list:
    """페이지 전체에서 BIOS zip 링크 기반으로 추출 (fallback)"""
    bios_list = []
    seen = set()
    for a in soup.find_all("a", href=re.compile(r"download\.asrock\.com.*BIOS.*\.zip", re.I)):
        link = a.get("href", "").strip()
        if link in seen:
            continue
        seen.add(link)

        # 링크에서 버전 번호 추출: ModelName(3.16).zip 패턴
        m = re.search(r'\(([^)]+)\)\.zip', link, re.I)
        version = m.group(1) if m else ""

        # 부모 행에서 날짜·설명 찾기
        row = a.find_parent("tr")
        date, description = "", ""
        if row:
            texts = [c.get_text(strip=True) for c in row.find_all("td")]
            date        = _find_date(texts)
            description = _find_description(texts, version, date)

        bios_list.append({
            "version":     version,
            "date":        date,
            "description": description,
            "link":        link,
        })
    return bios_list


# ── 파싱 헬퍼 ──────────────────────────────────────────────────────
_VERSION_RE = re.compile(r'\b\d+\.\d+\b')
_DATE_RE    = re.compile(r'\b(20\d{2})[/\-\.](0?[1-9]|1[0-2])[/\-\.](0?[1-9]|[12]\d|3[01])\b')


def _find_version(texts: list, link: str) -> str:
    # 1) 링크에서 ModelName(ver).zip 추출
    m = re.search(r'\(([^)]+)\)\.zip', link, re.I)
    if m:
        return m.group(1)
    # 2) 셀 텍스트에서 x.xx 형태 숫자 찾기
    for t in texts:
        m = _VERSION_RE.search(t)
        if m:
            return m.group()
    return ""


def _find_date(texts: list) -> str:
    for t in texts:
        m = _DATE_RE.search(t)
        if m:
            return m.group()
    return ""


def _find_description(texts: list, version: str, date: str) -> str:
    candidates = [
        t for t in texts
        if t and t != version and t != date
        and not re.fullmatch(r'[\d./\-]+', t)
        and len(t) > 3
    ]
    # 가장 긴 텍스트를 설명으로 사용
    return max(candidates, key=len) if candidates else ""


# ──────────────────────────────────────────────────────────────────
#  1단계: 메인보드 모델 리스트 수집
# ──────────────────────────────────────────────────────────────────
def collect_model_list() -> list:
    """
    https://www.asrock.com/mb/index.asp 에서 전체 모델 목록 수집.
    반환: [{"series", "platform", "model_name", "product_url"}, ...]
    """
    logger.info("📡 [1단계] ASRock 메인보드 모델 리스트 수집 중...")
    motherboards = []

    with sync_playwright() as p:
        browser, ctx = make_browser_context(p)
        page = new_stealth_page(ctx)
        try:
            page.goto(LISTING_URL, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            # Incapsula iframe 감지 → captcha 해결 대기
            _wait_for_incapsula(page)

            # captcha 해결 후 allmodels 변수가 정의될 때까지 대기 (최대 60초)
            logger.info("⏳ allmodels 로드 대기 중...")
            try:
                page.wait_for_function("typeof allmodels !== 'undefined'", timeout=60000)
                logger.info("✅ allmodels 감지됨")
            except PlaywrightTimeout:
                logger.warning("⚠️  allmodels 60초 내 미감지 — 현재 HTML로 시도")

            html = page.content()

            if CONFIG["debug"]:
                debug_path = os.path.join(BASE_PATH, "debug_listing.html")
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(html)
                logger.info(f"🐛 디버그 HTML 저장: {debug_path}")

        except PlaywrightTimeout:
            logger.error("모델 리스트 페이지 로드 타임아웃")
            return []
        finally:
            ctx.close()
            browser.close()

    # ── 카테고리 목록 파싱 ──
    categories = _parse_categories_from_html(html)
    logger.info(f"📂 카테고리 {len(categories)}개 파싱: {[l for _,l in categories]}")

    # ── 썸네일 이미지 URL 매핑 파싱 ──
    # <div onmousedown="GetPage('모델명')"><img data-original="/mb/photo/..."> 구조
    soup_listing = BeautifulSoup(html, "html.parser")
    image_map: dict = {}
    for div in soup_listing.find_all("div", attrs={"onmousedown": True}):
        onmouse = div.get("onmousedown", "")
        gp = re.search(r"GetPage\('(.+?)'\)", onmouse)
        if not gp:
            continue
        model_key = gp.group(1).strip()
        img = div.find("img")
        if img:
            src = img.get("data-original") or img.get("src") or ""
            if src:
                image_map[model_key] = BASE_URL + src if src.startswith("/") else src
                image_map[model_key.lower()] = image_map[model_key]  # 소문자 키도 등록
    logger.info(f"🖼️  썸네일 이미지 {len(image_map)}개 파싱")

    # ── JS 변수 allmodels 직접 추출 ──
    # 구조: ['모델명', '소켓ID', 'Intel/AMD 칩셋', '폼팩터']
    import ast
    m = re.search(r'allmodels\s*=\s*(\[.+?\]);', html, re.DOTALL)
    if m:
        raw = m.group(1)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = ast.literal_eval(raw)
            except Exception:
                data = []

        for entry in data:
            if not isinstance(entry, list) or len(entry) < 3:
                continue
            model_name  = str(entry[0]).strip()
            chipset     = str(entry[2]).strip()
            form_factor = str(entry[3]).strip() if len(entry) > 3 else ""
            platform    = chipset.split()[0] if chipset else "Unknown"
            model_url   = model_name.replace("/", "")
            product_url = f"{BASE_URL}/mb/{platform}/{model_url}/index.asp"

            # 카테고리: 리스팅 페이지 Categories HTML에서 파싱한 목록으로 감지
            category = _detect_category(model_name, categories)

            # 썸네일: 목록 페이지 HTML에서 파싱한 실제 URL 사용 (대소문자 무시)
            image_url = image_map.get(model_name) or image_map.get(model_name.lower(), "")

            motherboards.append({
                "series":      chipset,
                "platform":    platform,
                "model_name":  model_name,
                "form_factor": form_factor,
                "product_url": product_url,
                "image_url":   image_url,
                "category":    category,
            })
    else:
        logger.warning("⚠️  allmodels 배열을 찾지 못했습니다. HTML 구조가 변경됐을 수 있습니다.")

    logger.info(f"✅ 모델 리스트 수집 완료: {len(motherboards)}개 → {MASTER_FILE}")
    with open(MASTER_FILE, "w", encoding="utf-8") as f:
        json.dump(motherboards, f, ensure_ascii=False, indent=4)

    return motherboards


def _parse_categories_from_html(html: str) -> list:
    """
    <ul class="Categories"> 에서 카테고리 목록 파싱.
    반환: [(value, label), ...] — 예: [("AQUA","AQUA"), ("Phantom","Phantom Gaming"), ...]
    """
    soup = BeautifulSoup(html, "html.parser")
    ul = soup.find("ul", class_="Categories")
    if not ul:
        return []
    categories = []
    for label_tag in ul.find_all("label"):
        inp = label_tag.find("input")
        if not inp:
            continue
        value = inp.get("value", "").strip()
        text  = label_tag.get_text(strip=True)
        if value and text:
            categories.append((value, text))
    return categories


def _detect_category(model_name: str, categories: list) -> str:
    """
    categories: [(value, label), ...] — 리스팅 페이지에서 파싱한 목록
    모델명에 value가 포함되면 label 반환.
    """
    name = model_name.upper()
    for value, label in categories:
        if value.upper() in name:
            return label
    return ""


def _decode_model(raw: str) -> str:
    """URL 인코딩된 모델명을 디코딩"""
    from urllib.parse import unquote
    return unquote(raw).replace("+", " ").strip()




# ──────────────────────────────────────────────────────────────────
#  단일 모델 처리 (BIOS 수집) — requests 버전
# ──────────────────────────────────────────────────────────────────


def process_model(mb: dict, page=None) -> dict:
    """
    - 썸네일: index.kr.asp 에서 파싱
    - BIOS:   Specification.kr.asp 에서 파싱
    page: 워커에서 전달받은 Playwright page (스텔스 브라우저)
    """
    model       = mb["model_name"]
    platform    = mb.get("platform", "")
    series      = mb.get("series", "")
    product_url = mb.get("product_url", "")

    index_kr_bios_url = product_url.replace("index.asp", "index.kr.asp#BIOS")
    image_url = mb.get("image_url", "")  # collect_model_list 에서 이미 생성됨

    # 모델명에서 특수문자 제거한 키워드 (이미지 매칭용)
    model_keyword = re.sub(r'[^a-zA-Z0-9]', '', model).lower()

    def _parse_fallback_image(html: str) -> str:
        """image_url 없을 때 product 페이지 HTML에서 메인 모델 이미지 추출"""
        soup = BeautifulSoup(html, "html.parser")
        # 1순위: alt="Product Photo" 태그
        for img in soup.find_all("img", alt="Product Photo"):
            src = img.get("src") or img.get("data-original") or ""
            if src and not src.startswith("data:"):
                return BASE_URL + src if src.startswith("/") else src
        # 2순위: src에 모델명 키워드 포함
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-original") or ""
            if not src or src.startswith("data:"):
                continue
            src_key = re.sub(r'[^a-zA-Z0-9]', '', src).lower()
            if model_keyword and model_keyword[:6] in src_key:
                return BASE_URL + src if src.startswith("/") else src
        return ""

    def _collect(p):
        # index.kr.asp#BIOS → BIOS 섹션 동적 로드 후 파싱
        p.goto(index_kr_bios_url, timeout=CONFIG["page_timeout"], wait_until="domcontentloaded")
        _wait_for_captcha_clear(p, index_kr_bios_url)
        try:
            p.wait_for_selector(
                "a[href*='download.asrock.com/BIOS']",
                timeout=CONFIG["bios_wait"]
            )
        except PlaywrightTimeout:
            pass
        html = p.content()
        bios = parse_bios_table(html)
        # 항상 product 페이지에서 이미지 파싱 (listing보다 정확)
        img  = _parse_fallback_image(html)
        return bios, img

    bios_list = []
    fallback_image = ""

    if page is not None:
        for attempt in range(1, CONFIG["max_retries"] + 1):
            try:
                bios_list, fallback_image = _collect(page)
                break
            except Exception as e:
                logger.warning(f"오류 [{model}] 시도 {attempt}: {e}")
                if attempt < CONFIG["max_retries"]:
                    time.sleep((2 ** attempt) + random.uniform(0.5, 1.5))
    else:
        with sync_playwright() as p:
            browser, ctx = make_browser_context(p)
            pg = new_stealth_page(ctx)
            try:
                bios_list, fallback_image = _collect(pg)
            finally:
                ctx.close()
                browser.close()

    time.sleep(random.uniform(CONFIG["delay_min"], CONFIG["delay_max"]))

    return {
        "series":      series,
        "platform":    platform,
        "model_name":  model,
        "product_url": product_url,
        "image_url":   fallback_image or image_url,
        "bios_list":   bios_list,
    }




# ──────────────────────────────────────────────────────────────────
#  체크포인트
# ──────────────────────────────────────────────────────────────────
def load_checkpoint() -> set:
    if not os.path.exists(CHECKPOINT_FILE):
        return set()
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_checkpoint(completed: set):
    with checkpoint_lock:
        with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            json.dump(list(completed), f, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────
#  SQLite 저장
# ──────────────────────────────────────────────────────────────────
def _migrate_db(conn):
    """기존 DB 스키마 마이그레이션: last_checked 컬럼 제거 (updated_at과 중복)."""
    cur = conn.cursor()
    cols = [row[1] for row in cur.execute("PRAGMA table_info(motherboards)").fetchall()]
    if "last_checked" not in cols:
        return
    logger.info("🔧 DB 마이그레이션: last_checked 컬럼 제거 중...")
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS motherboards_new (
            model_name      TEXT PRIMARY KEY,
            series          TEXT,
            platform        TEXT,
            form_factor     TEXT,
            product_url     TEXT,
            image_url       TEXT DEFAULT '',
            category        TEXT DEFAULT '',
            updated_at      TEXT DEFAULT (datetime('now','localtime')),
            last_valid_date TEXT
        );
        INSERT INTO motherboards_new
            SELECT model_name, series, platform, form_factor, product_url,
                   image_url, category, updated_at, last_valid_date
            FROM motherboards;
        DROP TABLE motherboards;
        ALTER TABLE motherboards_new RENAME TO motherboards;
    """)
    conn.commit()
    logger.info("✅ DB 마이그레이션 완료")


def save_to_sqlite(all_data: list):
    conn = sqlite3.connect(DB_FILE)
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS motherboards (
            model_name      TEXT PRIMARY KEY,
            series          TEXT,
            platform        TEXT,
            form_factor     TEXT,
            product_url     TEXT,
            image_url       TEXT DEFAULT '',
            category        TEXT DEFAULT '',
            updated_at      TEXT DEFAULT (datetime('now','localtime')),
            last_valid_date TEXT
        )
    """)
    _migrate_db(conn)
    # 기존 DB에 컬럼이 없을 경우 추가
    for col, default in [
        ("image_url",       "''"),
        ("category",        "''"),
        ("last_valid_date", "NULL"),
    ]:
        try:
            cur.execute(f"ALTER TABLE motherboards ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass  # 이미 존재
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bios_versions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name  TEXT,
            version     TEXT,
            date        TEXT,
            description TEXT,
            link        TEXT,
            UNIQUE(model_name, version)
        )
    """)
    for item in all_data:
        model = item["model_name"]
        has_bios = bool(item.get("bios_list"))
        cur.execute("""
            INSERT INTO motherboards
                (model_name, series, platform, form_factor, product_url,
                 image_url, category, updated_at, last_valid_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'),
                    CASE WHEN ? THEN datetime('now','localtime') ELSE NULL END)
            ON CONFLICT(model_name) DO UPDATE SET
                series          = excluded.series,
                platform        = excluded.platform,
                form_factor     = excluded.form_factor,
                product_url     = excluded.product_url,
                image_url       = CASE WHEN motherboards.image_url != '' THEN motherboards.image_url
                                       ELSE excluded.image_url END,
                category        = excluded.category,
                updated_at      = excluded.updated_at,
                last_valid_date = CASE WHEN excluded.last_valid_date IS NOT NULL
                                       THEN excluded.last_valid_date
                                       ELSE motherboards.last_valid_date
                                  END
        """, (model, item.get("series", ""), item.get("platform", ""),
               item.get("form_factor", ""), item.get("product_url", ""),
               item.get("image_url", ""), item.get("category", ""),
               1 if has_bios else 0))
        for b in item.get("bios_list", []):
            cur.execute("""
                INSERT INTO bios_versions (model_name, version, date, description, link)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(model_name, version) DO UPDATE SET
                    date        = excluded.date,
                    description = excluded.description,
                    link        = excluded.link
            """, (model, b["version"], b["date"], b["description"], b["link"]))
    conn.commit()
    conn.close()
    logger.info(f"💾 SQLite 저장 완료: {len(all_data)}개 모델 → {DB_FILE}")


def _save_results(all_data: list, completed: set):
    with open(FINAL_JSON, "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=4)
    save_checkpoint(completed)


# ──────────────────────────────────────────────────────────────────
#  영구 불가 모델 로그
# ──────────────────────────────────────────────────────────────────
def append_no_bios_log(model_names: list):
    existing = set()
    if os.path.exists(NO_BIOS_LOG):
        with open(NO_BIOS_LOG, "r", encoding="utf-8") as f:
            existing = {line.strip() for line in f if line.strip()}
    new_entries = [m for m in model_names if m not in existing]
    if new_entries:
        with open(NO_BIOS_LOG, "a", encoding="utf-8") as f:
            f.write("\n".join(new_entries) + "\n")
        logger.info(
            f"📝 영구 불가 모델 {len(new_entries)}개 → {NO_BIOS_LOG} "
            f"(누적 {len(existing) + len(new_entries)}개)"
        )


def load_no_bios_log() -> set:
    if not os.path.exists(NO_BIOS_LOG):
        return set()
    try:
        with open(NO_BIOS_LOG, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except Exception:
        return set()


# ──────────────────────────────────────────────────────────────────
#  병렬 수집 공통 함수
# ──────────────────────────────────────────────────────────────────
def _worker_collect(mbs: list, results: list, lock: Lock,
                    progress, counter: dict, total: int,
                    failed_mbs: list, completed_models: set):
    """워커 스레드: 스텔스 브라우저 1개를 유지하며 할당된 모델 순차 처리"""
    with sync_playwright() as p:
        browser, ctx = make_browser_context(p)
        page = new_stealth_page(ctx)
        _collect_with_page(page, mbs, results, lock, progress,
                           counter, total, failed_mbs, completed_models)
        ctx.close()
        browser.close()


def _collect_with_page(page, mbs, results, lock, progress,
                       counter, total, failed_mbs, completed_models):
    for mb in mbs:
        model = mb["model_name"]
        try:
            result = process_model(mb, page=page)
        except Exception as e:
            logger.error(f"🔥 예외 발생 [{model}]: {e}")
            result = None

        with lock:
            counter["n"] += 1
            n = counter["n"]
            if result:
                bios_count = len(result["bios_list"])
                results.append(result)
                if bios_count > 0:
                    completed_models.add(model)
                else:
                    failed_mbs.append(mb)
                if USE_TQDM and progress:
                    progress.set_postfix_str(f"{model[:20]} | 💾 {bios_count}개")
            else:
                failed_mbs.append(mb)

            if USE_TQDM and progress:
                progress.update(1)
            elif not USE_TQDM:
                bios_count = len(result["bios_list"]) if result else 0
                print(f"✅ [{n}/{total}] {model[:25].ljust(25)} | 💾 {bios_count}개")


def run_collection(pending_mbs: list, total: int, done_offset: int,
                   completed_models: set, all_data: list,
                   desc: str = "수집 중"):
    """
    pending_mbs 를 병렬 수집 (워커당 브라우저 1개).
    반환: (all_data, completed_models, 실패 mb 목록)
    """
    failed_mbs = []
    counter    = {"n": done_offset}
    lock       = Lock()

    progress = tqdm(total=total, initial=done_offset,
                    desc=desc, unit="모델") if USE_TQDM else None

    # 모델 목록을 워커 수만큼 균등 분할
    workers = CONFIG["workers"]
    chunks  = [pending_mbs[i::workers] for i in range(workers)]
    results = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _worker_collect, chunk, results, lock,
                progress, counter, total, failed_mbs, completed_models
            )
            for chunk in chunks if chunk
        ]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                logger.error(f"🔥 예외 발생: {e}")

    with save_lock:
        all_data.extend(results)

    if progress:
        progress.close()

    # 주기적 저장은 워커 내부에서 처리하기 어려우므로 수집 완료 후 일괄 저장
    _save_results(all_data, completed_models)

    return all_data, completed_models, failed_mbs



# ──────────────────────────────────────────────────────────────────
#  2단계 + 3단계: BIOS 수집 및 재시도
# ──────────────────────────────────────────────────────────────────
def collect_bios_data(motherboards: list):
    logger.info(f"\n🚀 [2단계] 상세 수집 시작 (workers={CONFIG['workers']})")

    completed_models = load_checkpoint()
    if completed_models:
        logger.info(f"⏩ Resume 모드: {len(completed_models)}개 이미 완료, 나머지만 수집")

    all_data = []
    if os.path.exists(FINAL_JSON):
        try:
            with open(FINAL_JSON, "r", encoding="utf-8") as f:
                all_data = json.load(f)
        except Exception:
            all_data = []

    pending = [mb for mb in motherboards if mb["model_name"] not in completed_models]
    total   = len(motherboards)
    done    = len(completed_models)

    # ── 2단계 ──
    all_data, completed_models, failed_mbs = run_collection(
        pending_mbs=pending,
        total=total,
        done_offset=done,
        completed_models=completed_models,
        all_data=all_data,
        desc="수집 중",
    )
    _save_results(all_data, completed_models)
    logger.info(
        f"\n📊 1차 수집 완료 | "
        f"성공: {len(completed_models)}개 | 실패: {len(failed_mbs)}개"
    )

    # ── 3단계: 실패 모델 재시도 ──
    if failed_mbs:
        logger.info(
            f"\n⏳ [3단계] 실패 모델 {len(failed_mbs)}개 → "
            f"{CONFIG['retry_wait'] // 60}분 후 재시도..."
        )
        for remaining in range(CONFIG["retry_wait"], 0, -30):
            logger.info(f"   재시도까지 {remaining}초 남음...")
            time.sleep(30)

        logger.info(f"\n🔄 재시도 시작 ({len(failed_mbs)}개)")
        retry_total = len(failed_mbs)
        all_data, completed_models, still_failed_mbs = run_collection(
            pending_mbs=failed_mbs,
            total=retry_total,
            done_offset=0,
            completed_models=completed_models,
            all_data=all_data,
            desc="재시도",
        )
        _save_results(all_data, completed_models)
        logger.info(
            f"\n📊 재시도 완료 | "
            f"성공: {retry_total - len(still_failed_mbs)}개 | "
            f"최종 실패: {len(still_failed_mbs)}개"
        )
        if still_failed_mbs:
            no_bios_names = [mb["model_name"] for mb in still_failed_mbs]
            append_no_bios_log(no_bios_names)
            logger.warning(
                f"🚫 BIOS 없는 모델 {len(no_bios_names)}개 → "
                f"{os.path.basename(NO_BIOS_LOG)} 에 영구 기록"
            )
    else:
        still_failed_mbs = []
        logger.info("✅ 실패 모델 없음, 재시도 생략")

    save_to_sqlite(all_data)
    logger.info(
        f"\n✨ 전체 완료!\n"
        f"   ✅ 수집 성공: {len(completed_models)}개\n"
        f"   🚫 BIOS 없음: {len(still_failed_mbs)}개"
    )


# ══════════════════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="ASRock BIOS 스크래퍼")
    parser.add_argument(
        "--full", action="store_true",
        help="no_bios_log 모델 포함 전체 재수집"
    )
    parser.add_argument(
        "--no-headless", action="store_true",
        help="브라우저 창을 표시하면서 실행 (디버깅용)"
    )
    parser.add_argument(
        "--workers", type=int, default=CONFIG["workers"],
        help=f"병렬 워커 수 (기본: {CONFIG['workers']})"
    )
    parser.add_argument(
        "--recollect", action="store_true",
        help="기존 마스터 파일 무시하고 모델 목록 재수집"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="페이지 HTML을 debug_*.html 파일로 저장 (파싱 문제 진단용)"
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="체크포인트·JSON·no_bios 로그 초기화 후 전체 재수집"
    )
    parser.add_argument(
        "--retry-db", action="store_true",
        help="DB에서 BIOS 없는 모델만 뽑아 재시도 (1단계 생략)"
    )
    parser.add_argument(
        "--data-dir", default=None,
        help="DB 저장 경로 (기본: 스크래퍼 폴더)"
    )
    args = parser.parse_args()

    if args.no_headless:
        CONFIG["headless"] = False
    if args.debug:
        CONFIG["debug"] = True
    CONFIG["workers"] = args.workers

    global DB_FILE
    if args.data_dir:
        DB_FILE = os.path.join(args.data_dir, os.path.basename(DB_FILE))

    # --retry-db: DB에서 BIOS 미수집 모델만 재시도
    if args.retry_db:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute("""
            SELECT m.model_name, m.series, m.platform, m.form_factor, m.product_url, m.image_url, m.category
            FROM motherboards m
            WHERE m.model_name NOT IN (SELECT DISTINCT model_name FROM bios_versions)
        """).fetchall()
        conn.close()
        retry_mbs = [
            {"model_name": r[0], "series": r[1], "platform": r[2],
             "form_factor": r[3], "product_url": r[4], "image_url": r[5], "category": r[6]}
            for r in rows
        ]
        logger.info(f"🔄 --retry-db: DB에서 BIOS 없는 모델 {len(retry_mbs)}개 재시도")
        collect_bios_data(retry_mbs)
        return

    # 1단계: 모델 리스트 수집 (항상 실행 — 카테고리/이미지 URL 포함)
    motherboards = collect_model_list()
    if not motherboards:
        logger.error("❌ 모델 리스트 수집 실패, 종료")
        logger.error("   → --debug 옵션으로 실행해 debug_listing.html 확인 권장")
        return

    # 체크포인트는 매 실행마다 초기화 (모든 모델 재방문 → 신규 BIOS 버전 감지)
    # DB·JSON·no_bios_log 는 유지 (upsert로 갱신)
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        logger.info("🗑️  체크포인트 초기화 (이번 실행 중 크래시 복구용으로만 사용)")

    # --reset: DB·JSON·no_bios_log 까지 완전 초기화
    if args.reset:
        for path in [FINAL_JSON, NO_BIOS_LOG]:
            if os.path.exists(path):
                os.remove(path)
                logger.info(f"🗑️  초기화: {os.path.basename(path)} 삭제")

    logger.info(f"📊 수집 시작 | 전체: {len(motherboards)}개")
    collect_bios_data(motherboards)


if __name__ == "__main__":
    main()

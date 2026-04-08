"""
GIGABYTE BIOS Data Collector
crontab: 0 7 * * * python3 /path/to/gigabyte_bios_scraper.py

실행 흐름:
  1단계 — 전체 메인보드 모델 리스트 수집
           GetSecondProperty (칩셋) → GetProducts (모델명)
  2단계 — 병렬 BIOS 데이터 수집
           제품 지원 페이지 HTML → __NUXT_DATA__ JSON 파싱
  3단계 — 실패 모델 5분 대기 후 1회 재시도
  4단계 — 재시도 후에도 실패 = BIOS 없는 단종/특수 모델
           → gigabyte_no_bios_models.log 에 영구 기록
"""

import argparse
import asyncio
import requests
import json
import os
import re
import time
import random
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import threading
import nodriver as uc

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
    "timeout":           30,
    "delay_min":         1.0,
    "delay_max":         2.5,
    "save_interval":     20,
    "db_save_interval":  100,
    "retry_wait":        300,   # 재시도 전 대기 (초)
    "timeout_cooldown":  30,
    "timeout_threshold": 5,
    "block_cooldown":    900,   # 15분
    "block_max_retry":   3,
    "block_threshold":   5,
}

# ══════════════════════════════════════════════════════════════════
#  경로
# ══════════════════════════════════════════════════════════════════
BASE_PATH       = os.path.dirname(os.path.abspath(__file__))
MASTER_FILE     = os.path.join(BASE_PATH, "gigabyte_motherboards_master.json")
FINAL_JSON      = os.path.join(BASE_PATH, "gigabyte_bios_data_final.json")
CHECKPOINT_FILE = os.path.join(BASE_PATH, "gigabyte_checkpoint.json")
NO_BIOS_LOG     = os.path.join(BASE_PATH, "gigabyte_no_bios_models.log")
DB_FILE         = os.path.join(BASE_PATH, "gigabyte_bios.db")

# ══════════════════════════════════════════════════════════════════
#  API / URL
# ══════════════════════════════════════════════════════════════════
BASE_URL     = "https://www.gigabyte.com"
API_BASE     = f"{BASE_URL}/iisApplicationNuxt/api/proxy/api/v1.0/Support/global/DownloadCenter/2"
CHIPSET_API  = f"{API_BASE}/GetSecondProperty"
PRODUCTS_API = f"{API_BASE}/GetProducts"

# 제품 지원 페이지 URL 후보 (순서대로 시도)
PRODUCT_URL_TEMPLATES = [
    f"{BASE_URL}/Motherboard/{{slug}}/support",
    f"{BASE_URL}/Server-Motherboard/{{slug}}/support",
    f"{BASE_URL}/Enterprise/{{slug}}/support",
]

# ══════════════════════════════════════════════════════════════════
#  로거
# ══════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_PATH, "gigabyte_scraper.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  헤더
# ══════════════════════════════════════════════════════════════════
PAGE_HEADERS = {
    "User-Agent":        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/122.0.0.0 Safari/537.36",
    "Referer":           "https://www.gigabyte.com/",
    "Accept":            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":   "en-US,en;q=0.9",
    "Accept-Encoding":   "gzip, deflate, br",
    "sec-ch-ua":         '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "sec-ch-ua-mobile":  "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest":    "document",
    "sec-fetch-mode":    "navigate",
    "sec-fetch-site":    "same-origin",
    "Connection":        "keep-alive",
}

API_HEADERS = {
    **PAGE_HEADERS,
    "Accept":            "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With":  "XMLHttpRequest",
    "sec-fetch-dest":    "empty",
    "sec-fetch-mode":    "cors",
    "sec-fetch-site":    "same-origin",
}

# ══════════════════════════════════════════════════════════════════
#  공유 상태 / 락
# ══════════════════════════════════════════════════════════════════
save_lock            = Lock()
print_lock           = Lock()
checkpoint_lock      = Lock()
timeout_lock         = Lock()
_thread_local        = threading.local()  # 스레드별 세션 보관
consecutive_timeouts = {"count": 0}
consecutive_blocks   = {"count": 0}
completed_models_ref = [set()]
block_cooldown_count = {"count": 0}


# ──────────────────────────────────────────────────────────────────
#  유틸: 재시도 포함 GET
# ──────────────────────────────────────────────────────────────────
def safe_get(session, url, params=None, headers=None, retries=CONFIG["max_retries"]):
    _headers = headers or PAGE_HEADERS
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, headers=_headers,
                               timeout=CONFIG["timeout"])
            if resp.status_code == 200:
                # 성공 시 카운터 초기화
                with timeout_lock:
                    consecutive_timeouts["count"] = 0
                    consecutive_blocks["count"]   = 0
                return resp
            if resp.status_code in (403, 404, 500):
                logger.debug(f"HTTP {resp.status_code} (skip) | {url}")
                return None
            if resp.status_code == 451:
                with timeout_lock:
                    consecutive_blocks["count"] += 1
                    block_count = consecutive_blocks["count"]
                # 연속 451이 임계값 초과 시 긴 쿨다운
                if block_count >= CONFIG["block_threshold"]:
                    with timeout_lock:
                        block_cooldown_count["count"] += 1
                        cooldown_n = block_cooldown_count["count"]
                    if cooldown_n > CONFIG["block_max_retry"]:
                        logger.error(
                            f"❌ 451 차단 쿨다운 {cooldown_n-1}회 초과 — 오늘 수집 종료, 내일 재시도"
                        )
                        raise SystemExit(1)
                    wait_min = CONFIG["block_cooldown"] // 60
                    logger.warning(
                        f"🚫 연속 451 차단 — {wait_min}분 대기 후 재개 "
                        f"({cooldown_n}/{CONFIG['block_max_retry']})"
                    )
                    with save_lock:
                        save_checkpoint(completed_models_ref[0])
                    time.sleep(CONFIG["block_cooldown"])
                    with timeout_lock:
                        consecutive_blocks["count"] = 0
                    logger.info("▶️  대기 완료, 수집 재개")
                else:
                    wait = 10 + random.uniform(3, 7)
                    logger.warning(f"HTTP 451 (일시 차단) {wait:.0f}초 대기 후 재시도... ({attempt}/{retries})")
                    time.sleep(wait)
                continue
            if resp.status_code == 429:
                wait = 2 ** attempt + random.uniform(1, 3)
                logger.warning(f"HTTP 429 {wait:.1f}초 대기 후 재시도... ({attempt}/{retries})")
                time.sleep(wait)
            else:
                logger.warning(f"HTTP {resp.status_code} | 시도 {attempt}/{retries} | {url}")
        except requests.exceptions.Timeout:
            with timeout_lock:
                consecutive_timeouts["count"] += 1
                count = consecutive_timeouts["count"]
            logger.warning(f"Timeout | 시도 {attempt}/{retries}")
            # 연속 타임아웃이 임계값 초과 시 쿨다운
            if count >= CONFIG["timeout_threshold"]:
                logger.warning(
                    f"⚠️  연속 타임아웃 {count}회 — {CONFIG['timeout_cooldown']}초 대기"
                )
                time.sleep(CONFIG["timeout_cooldown"])
                with timeout_lock:
                    consecutive_timeouts["count"] = 0
        except requests.exceptions.RequestException as e:
            logger.warning(f"RequestException: {e} | 시도 {attempt}/{retries}")

        if attempt < retries:
            time.sleep((2 ** attempt) + random.uniform(0.5, 1.5))

    return None


# ──────────────────────────────────────────────────────────────────
#  Nuxt devalue 파서
# ──────────────────────────────────────────────────────────────────
def resolve_nuxt(raw, idx, depth=0, visited=None):
    """
    Nuxt 3 __NUXT_DATA__ 의 devalue 직렬화 배열에서
    idx 번 요소를 재귀적으로 역참조하여 실제 값을 반환.
    """
    if visited is None:
        visited = set()
    if depth > 50:
        return None
    if not isinstance(idx, int) or idx < 0 or idx >= len(raw):
        return idx  # 범위 벗어나면 값 자체를 반환
    if idx in visited:
        return f"<circular:{idx}>"

    visited = visited | {idx}
    val = raw[idx]

    if isinstance(val, list):
        # ShallowReactive/Ref 등 Nuxt 래퍼 처리
        if (len(val) == 2
                and isinstance(val[0], str)
                and val[0] in ("ShallowReactive", "Reactive", "ShallowRef", "Ref")):
            return resolve_nuxt(raw, val[1], depth + 1, visited)
        return [resolve_nuxt(raw, v, depth + 1, visited) for v in val]
    elif isinstance(val, dict):
        return {k: resolve_nuxt(raw, v, depth + 1, visited) for k, v in val.items()}
    else:
        return val  # 문자열, 숫자, bool, None → 그대로 반환


def _extract_bios_entry(raw, file_idx):
    """BIOS 파일 인덱스 하나를 해석해 dict 반환. BIOS URL이 아니면 None."""
    resolved = resolve_nuxt(raw, file_idx)
    if not isinstance(resolved, dict):
        return None

    link = resolved.get("filePath", "")
    if not isinstance(link, str) or "download.gigabyte.com/FileList/BIOS/" not in link:
        return None

    desc_raw = resolved.get("fileDescription", "") or ""
    desc = re.sub(r"<[^>]+>", " ", str(desc_raw)).strip()
    desc = re.sub(r"\s+", " ", desc)

    return {
        "version":     str(resolved.get("fileVersion", "")),
        "date":        str(resolved.get("fileReleaseDate", ""))[:10],  # YYYY-MM-DD
        "size":        resolved.get("fileSize", ""),
        "description": desc,
        "link":        link,
        "file_name":   str(resolved.get("fileName", "")),
    }


def _parse_raw(raw):
    """
    devalue 배열(raw)에서 BIOS 파일 목록 추출.

    전략 1 (정확): key="bios" 섹션 객체를 찾아 data 배열만 파싱.
    전략 2 (폴백): filePath+fileVersion+fileReleaseDate 키를 가진 객체 전체 검색.
      URL 에 'FileList/BIOS/' 포함 여부로 BIOS 파일만 필터링.
    """
    if not isinstance(raw, list):
        return []

    # ── 전략 1: "bios" 섹션 객체 탐색 ─────────────────────────────
    bios_file_indices = []
    for val in raw:
        if not isinstance(val, dict):
            continue
        if not ("key" in val and "data" in val):
            continue
        # key 필드 역참조 → "bios" 인지 확인
        key_idx = val["key"]
        key_str = raw[key_idx] if (isinstance(key_idx, int) and 0 <= key_idx < len(raw)) else key_idx
        if key_str != "bios":
            continue
        # data 필드 역참조 → 파일 인덱스 리스트
        data_idx = val["data"]
        data_val = raw[data_idx] if (isinstance(data_idx, int) and 0 <= data_idx < len(raw)) else data_idx
        if isinstance(data_val, list):
            bios_file_indices = data_val
            break

    if bios_file_indices:
        seen = set()
        result = []
        for file_idx in bios_file_indices:
            entry = _extract_bios_entry(raw, file_idx)
            if entry and entry["link"] not in seen:
                seen.add(entry["link"])
                result.append(entry)
        if result:
            return result

    # ── 전략 2: 전체 검색 폴백 ──────────────────────────────────────
    seen = set()
    result = []
    for i, val in enumerate(raw):
        if not isinstance(val, dict):
            continue
        if not all(k in val for k in ("filePath", "fileVersion", "fileReleaseDate")):
            continue
        entry = _extract_bios_entry(raw, i)
        if entry and entry["link"] not in seen:
            seen.add(entry["link"])
            result.append(entry)
    return result


def _extract_nuxt_raw(text):
    """HTML에서 __NUXT_DATA__ 배열 추출. 실패 시 None."""
    match = re.search(
        r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',
        text, re.DOTALL
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return None


def parse_nuxt_bios(text, is_json=False):
    """HTML 또는 _payload.json 텍스트에서 BIOS 목록 추출."""
    if is_json:
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return []
        raw = obj.get("data") if isinstance(obj, dict) else obj
    else:
        raw = _extract_nuxt_raw(text)
        if raw is None:
            return []

    return _parse_raw(raw)


def _parse_gallery_raw(raw):
    """devalue 배열(raw)에서 갤러리 이미지 URL 목록 추출 (공용 헬퍼)."""
    images = []
    seen   = set()
    for val in raw:
        if not isinstance(val, dict) or "galleryItems" not in val:
            continue
        items_idx = val["galleryItems"]
        items = raw[items_idx] if (isinstance(items_idx, int) and items_idx < len(raw)) else items_idx
        if not isinstance(items, list):
            continue
        for item_idx in items:
            item = resolve_nuxt(raw, item_idx)
            if not isinstance(item, dict):
                continue
            # imageWithStaticDomain 우선, 없으면 image 필드
            url = item.get("imageWithStaticDomain") or item.get("image", "")
            if not isinstance(url, str) or not url.startswith("http"):
                if isinstance(url, str) and url.startswith("/"):
                    url = BASE_URL + url
                else:
                    continue
            if url not in seen:
                seen.add(url)
                images.append(url)
        break  # 첫 번째 galleryItems 섹션만 사용
    return images


def parse_nuxt_gallery(text):
    """
    HTML의 __NUXT_DATA__에서 갤러리 이미지 URL 목록 추출.
    galleryItems[*].imageWithStaticDomain 값 사용.
    반환: ["https://static.gigabyte.com/..."] 리스트
    """
    raw = _extract_nuxt_raw(text)
    if not isinstance(raw, list):
        return []
    return _parse_gallery_raw(raw)


# ──────────────────────────────────────────────────────────────────
#  URL 슬러그 생성
# ──────────────────────────────────────────────────────────────────
def make_slug(product_name):
    """
    'Z890 D PLUS'                      → 'Z890-D-PLUS'
    'Z790 AORUS MASTER (rev. 1.0)'     → 'Z790-AORUS-MASTER-rev-10'
    'Z790 EAGLE AX (rev. 1.x)'         → 'Z790-EAGLE-AX-rev-1x'
    'B760 DS3H AC (rev. 1.0/1.1)'      → 'B760-DS3H-AC-rev-10-11'
    'B760M C (rev. 1.1/1.2/1.3)'       → 'B760M-C-rev-11-12-13'
    'Z790 AORUS MASTER X 1.0'          → 'Z790-AORUS-MASTER-X-rev-10'
    """
    name = product_name.strip()

    def _rev(m):
        # "1.0 / 1.1 / 1.x" → "10-11-1x"
        parts = re.split(r'\s*/\s*', m.group(1).strip())
        return '-rev-' + '-'.join(p.replace('.', '') for p in parts)

    # "(rev. 1.0)" / "(rev. 1.x)" / "(rev. 1.0/1.1)" / "(rev. 1.0 / 1.1 / 1.2)"
    name = re.sub(
        r'\s*\(rev\.\s*([\d.x]+(?:\s*/\s*[\d.x]+)*)\)',
        _rev, name, flags=re.IGNORECASE
    )
    # 모델명 끝 "X 1.0", "X 1.1" 형식 (버전 숫자만, 괄호 없음)
    name = re.sub(
        r'\s+(\d+)\.(\d+)$',
        lambda m: f'-rev-{m.group(1)}{m.group(2)}',
        name
    )
    return name.replace(' ', '-')


# ──────────────────────────────────────────────────────────────────
#  브랜드 / 칩셋 분리
# ──────────────────────────────────────────────────────────────────
def split_brand_chipset(cs_name):
    """'Intel Z890' → ('Intel', 'Z890'), 'AMD B650E' → ('AMD', 'B650E')"""
    parts = cs_name.split(None, 1)
    if len(parts) == 2 and parts[0] in ("Intel", "AMD"):
        return parts[0], parts[1]
    return "", cs_name


# ──────────────────────────────────────────────────────────────────
#  페이지 HTML fetch (requests 빠른 경로 → nodriver 폴백)
# ──────────────────────────────────────────────────────────────────
async def _nodriver_fetch_async(url):
    """nodriver 비동기 fetch: 스레드별 브라우저/탭 재사용."""
    # 브라우저 초기화 (스레드당 1회)
    if not hasattr(_thread_local, "nd_browser"):
        browser = await uc.start()
        tab     = await browser.get("about:blank")
        _thread_local.nd_browser = browser
        _thread_local.nd_tab     = tab

    tab = _thread_local.nd_tab
    try:
        await tab.get(url)
        await asyncio.sleep(3)          # JS + __NUXT_DATA__ 렌더링 대기
        html = await tab.get_content()
        if html and len(html) > 1000 and "Access Denied" not in html:
            return html
        logger.warning(f"nodriver: 비정상 응답 ({len(html) if html else 0}bytes) | {url}")
        return None
    except Exception as e:
        logger.warning(f"nodriver 실패: {e} | {url}")
        # 탭 재생성 시도
        try:
            tab = await _thread_local.nd_browser.get("about:blank")
            _thread_local.nd_tab = tab
        except Exception:
            pass
        return None


def _requests_fetch(url):
    """
    requests로 빠른 HTML 수집 시도.
    SSR HTML에 __NUXT_DATA__가 포함된 경우 브라우저 없이 즉시 반환.
    차단(403/451) 또는 데이터 없으면 None 반환 → nodriver 폴백.
    """
    if not hasattr(_thread_local, "req_session"):
        s = requests.Session()
        s.headers.update(PAGE_HEADERS)
        _thread_local.req_session = s
    try:
        resp = _thread_local.req_session.get(url, timeout=CONFIG["timeout"])
        if resp.status_code == 200 and "__NUXT_DATA__" in resp.text:
            return resp.text
    except requests.exceptions.RequestException:
        pass
    return None


def fetch_page_html(url):
    """
    페이지 HTML 수집: requests 빠른 경로 우선 시도.
    __NUXT_DATA__가 SSR HTML에 없으면 nodriver(Chromium)로 폴백.
    """
    # 빠른 경로: requests (브라우저 불필요, 3초 sleep 없음)
    html = _requests_fetch(url)
    if html:
        return html
    # 폴백: nodriver (JS 렌더링이 필요한 경우)
    if not hasattr(_thread_local, "nd_loop"):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _thread_local.nd_loop = loop
    return _thread_local.nd_loop.run_until_complete(_nodriver_fetch_async(url))


# ──────────────────────────────────────────────────────────────────
#  단일 모델 처리
# ──────────────────────────────────────────────────────────────────
def process_model(mb, _session):
    model    = mb["model_name"]
    chipset  = mb.get("chipset", "")
    brand    = mb.get("brand", "")
    model_id = mb.get("model_id", "")
    slug     = make_slug(model)

    bios_list   = []
    images      = []
    product_url = "N/A"

    # ── BIOS: support 페이지 (nodriver) ──────────────────────────────
    for template in PRODUCT_URL_TEMPLATES:
        base_url = template.format(slug=slug)
        html = fetch_page_html(base_url)
        if html is None:
            continue
        if product_url == "N/A":
            product_url = base_url
        parsed = parse_nuxt_bios(html, is_json=False)
        if parsed:
            bios_list = parsed
            # support 페이지 HTML에서 갤러리 먼저 추출
            images = parse_nuxt_gallery(html)
            break

    # 갤러리 없으면 /gallery 페이지 별도 fetch
    if not images and product_url != "N/A":
        gallery_url = product_url.replace("/support", "/gallery")
        if gallery_url != product_url:
            gallery_html = fetch_page_html(gallery_url)
            if gallery_html:
                images = parse_nuxt_gallery(gallery_html)

    image_url = images[0] if images else ""
    time.sleep(random.uniform(CONFIG["delay_min"], CONFIG["delay_max"]))

    return {
        "brand":       brand,
        "chipset":     chipset,
        "model_name":  model,
        "model_id":    model_id,
        "product_url": product_url,
        "image_url":   image_url,
        "bios_list":   bios_list,
    }


# ──────────────────────────────────────────────────────────────────
#  1단계: 마스터 리스트 수집
# ──────────────────────────────────────────────────────────────────
def warmup_session(session):
    """메인 페이지를 먼저 방문해 쿠키/세션을 초기화한다."""
    try:
        resp = session.get(BASE_URL, headers=PAGE_HEADERS, timeout=CONFIG["timeout"])
        logger.info(f"🌐 세션 워밍업 완료 (status={resp.status_code}, "
                    f"cookies={list(session.cookies.keys())})")
        time.sleep(random.uniform(1.5, 2.5))
    except Exception as e:
        logger.warning(f"⚠️  워밍업 실패 (무시하고 진행): {e}")


def collect_model_list(session):
    logger.info("📡 [1단계] Gigabyte 메인보드 모델 리스트 수집 중...")
    motherboards = []

    warmup_session(session)

    resp_cs = safe_get(session, CHIPSET_API, headers=API_HEADERS)
    if not resp_cs:
        raise RuntimeError("칩셋 리스트 API 호출 실패")

    chipsets = resp_cs.json().get("data") or []
    logger.info(f"   칩셋 {len(chipsets)}개 발견")

    total_cs = len(chipsets)
    for idx, cs in enumerate(chipsets, 1):
        cs_key  = cs["key"]
        cs_name = cs["name"]
        resp_p  = safe_get(session, PRODUCTS_API,
                           params={"property": cs_key}, headers=API_HEADERS)
        if not resp_p:
            logger.warning(f"[{idx}/{total_cs}] 칩셋 '{cs_name}' 수집 실패, 건너뜀")
            continue
        products = resp_p.json().get("data") or []
        brand, chipset_clean = split_brand_chipset(cs_name)
        for p in products:
            motherboards.append({
                "brand":      brand,
                "chipset":    chipset_clean,
                "model_name": p["productName"],
                "model_id":   str(p.get("productId", "")),
            })
        logger.info(f"   [{idx}/{total_cs}] {cs_name} — {len(products)}개")
        time.sleep(random.uniform(0.3, 0.7))

    # 동일 모델이 여러 칩셋에 중복 등장할 경우 첫 번째 칩셋 기준으로 중복 제거
    seen = set()
    unique = []
    for mb in motherboards:
        if mb["model_name"] not in seen:
            seen.add(mb["model_name"])
            unique.append(mb)
    motherboards = unique

    with open(MASTER_FILE, "w", encoding="utf-8") as f:
        json.dump(motherboards, f, ensure_ascii=False, indent=4)

    logger.info(f"✅ 모델 리스트 수집 완료: {len(motherboards)}개 → {MASTER_FILE}")
    return motherboards


# ──────────────────────────────────────────────────────────────────
#  체크포인트
# ──────────────────────────────────────────────────────────────────
def load_checkpoint():
    if not os.path.exists(CHECKPOINT_FILE):
        return set()
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_checkpoint(completed):
    with checkpoint_lock:
        with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            json.dump(list(completed), f, ensure_ascii=False)


def checkpoint_key(mb):
    """체크포인트 키: '{brand}|{chipset}|{model_id}'"""
    return f"{mb.get('brand', '')}|{mb.get('chipset', '')}|{mb.get('model_id', '')}"


# ──────────────────────────────────────────────────────────────────
#  SQLite 저장
# ──────────────────────────────────────────────────────────────────
def save_to_sqlite(all_data):
    conn = sqlite3.connect(DB_FILE)
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS motherboards (
            model_id        TEXT PRIMARY KEY,
            model_name      TEXT,
            brand           TEXT DEFAULT '',
            chipset         TEXT DEFAULT '',
            image_url       TEXT DEFAULT '',
            product_url     TEXT DEFAULT '',
            updated_at      TEXT DEFAULT (datetime('now','localtime')),
            last_valid_date TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bios_versions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            model_id     TEXT,
            model_name   TEXT,
            version      TEXT,
            date         TEXT DEFAULT '',
            info         TEXT DEFAULT '',
            name         TEXT DEFAULT '',
            download_url TEXT DEFAULT '',
            size         TEXT DEFAULT '',
            UNIQUE(model_id, version)
        )
    """)
    # 스키마 마이그레이션 (기존 DB 호환)
    for col, default in [
        ("brand",           "''"),
        ("image_url",       "''"),
        ("product_url",     "''"),
        ("last_valid_date", "NULL"),
    ]:
        try:
            cur.execute(f"ALTER TABLE motherboards ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    for col, default in [
        ("model_id",     "''"),
        ("info",         "''"),
        ("name",         "''"),
        ("download_url", "''"),
        ("size",         "''"),
    ]:
        try:
            cur.execute(f"ALTER TABLE bios_versions ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass

    for item in all_data:
        model_id  = str(item.get("model_id", ""))
        model     = item.get("model_name", "")
        image_url = item.get("image_url", "")
        has_bios  = bool(item.get("bios_list"))
        cur.execute("""
            INSERT INTO motherboards
                (model_id, model_name, brand, chipset, image_url, product_url,
                 updated_at, last_valid_date)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'),
                    CASE WHEN ? THEN datetime('now','localtime') ELSE NULL END)
            ON CONFLICT(model_id) DO UPDATE SET
                model_name      = excluded.model_name,
                brand           = excluded.brand,
                chipset         = excluded.chipset,
                image_url       = CASE WHEN motherboards.image_url != '' THEN motherboards.image_url
                                       ELSE excluded.image_url END,
                product_url     = excluded.product_url,
                updated_at      = excluded.updated_at,
                last_valid_date = CASE WHEN excluded.last_valid_date IS NOT NULL
                                       THEN excluded.last_valid_date
                                       ELSE motherboards.last_valid_date
                                  END
        """, (model_id, model, item.get("brand", ""), item.get("chipset", ""),
              image_url, item.get("product_url", ""), 1 if has_bios else 0))
        for b in item.get("bios_list", []):
            cur.execute("""
                INSERT INTO bios_versions
                    (model_id, model_name, version, date, info, name, download_url, size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(model_id, version) DO UPDATE SET
                    date         = excluded.date,
                    info         = excluded.info,
                    name         = excluded.name,
                    download_url = excluded.download_url,
                    size         = excluded.size
            """, (model_id, model, b["version"], b["date"],
                  b.get("description", ""), b.get("file_name", ""),
                  b.get("link", ""), str(b.get("size", ""))))
    conn.commit()
    conn.close()
    logger.info(f"💾 SQLite 저장 완료: {len(all_data)}개 모델 → {DB_FILE}")


def _save_results(all_data, completed):
    with open(FINAL_JSON, "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=4)
    save_checkpoint(completed)


# ──────────────────────────────────────────────────────────────────
#  영구 불가 모델 로그
# ──────────────────────────────────────────────────────────────────
def append_no_bios_log(model_names):
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


# ──────────────────────────────────────────────────────────────────
#  병렬 수집 공통 함수
# ──────────────────────────────────────────────────────────────────
def run_collection(pending_mbs, total, done_offset, completed_models,
                   all_data, desc="수집 중"):
    failed_mbs = []
    counter    = {"n": done_offset}
    completed_models_ref[0] = completed_models

    def worker(mb):
        return process_model(mb, None)

    progress = tqdm(total=total, initial=done_offset,
                    desc=desc, unit="모델") if USE_TQDM else None

    with ThreadPoolExecutor(max_workers=CONFIG["workers"]) as executor:
        future_to_mb = {executor.submit(worker, mb): mb for mb in pending_mbs}

        for future in as_completed(future_to_mb):
            mb    = future_to_mb[future]
            model = mb["model_name"]

            try:
                result = future.result()
            except Exception as e:
                logger.error(f"🔥 예외 [{model}]: {e}")
                result = None

            counter["n"] += 1
            n = counter["n"]

            if result:
                bios_count = len(result["bios_list"])
                with save_lock:
                    all_data.append(result)
                    if bios_count > 0:
                        completed_models.add(checkpoint_key(mb))
                    else:
                        failed_mbs.append(mb)
                with print_lock:
                    if USE_TQDM:
                        progress.set_postfix_str(f"{model[:20]} | 💾 {bios_count}개")
                        progress.update(1)
                    else:
                        print(f"✅ [{n}/{total}] "
                              f"{model[:25].ljust(25)} | 💾 {bios_count}개")
            else:
                with save_lock:
                    failed_mbs.append(mb)
                with print_lock:
                    if USE_TQDM:
                        progress.update(1)
                    else:
                        print(f"⚠️  [{n}/{total}] {model} — 수집 실패")

            if n % CONFIG["save_interval"] == 0:
                with save_lock:
                    _save_results(all_data, completed_models)
            if n % CONFIG["db_save_interval"] == 0:
                with save_lock:
                    save_to_sqlite(all_data)
                    logger.info(f"💾 SQLite 중간 저장 ({n}번째)")

    if progress:
        progress.close()

    return all_data, completed_models, failed_mbs


# ──────────────────────────────────────────────────────────────────
#  2단계: BIOS 수집 + 3단계: 실패 재시도
# ──────────────────────────────────────────────────────────────────
def collect_bios_data(motherboards):
    logger.info(f"\n🚀 [2단계] 상세 수집 시작 (workers={CONFIG['workers']})")

    completed_models = load_checkpoint()
    if completed_models:
        logger.info(f"⏩ Resume 모드: {len(completed_models)}개 완료, 나머지 수집")

    all_data = []
    if os.path.exists(FINAL_JSON):
        try:
            with open(FINAL_JSON, "r", encoding="utf-8") as f:
                all_data = json.load(f)
        except Exception:
            all_data = []

    pending = [mb for mb in motherboards if checkpoint_key(mb) not in completed_models]
    total   = len(motherboards)
    done    = len(completed_models)

    # ── 2단계: 전체 수집 ──
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

    # ── 3단계: 실패 모델 5분 대기 후 1회 재시도 ──
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
        all_data, completed_models, still_failed = run_collection(
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
            f"성공: {retry_total - len(still_failed)}개 | "
            f"최종 실패: {len(still_failed)}개"
        )
        if still_failed:
            append_no_bios_log([mb["model_name"] for mb in still_failed])
            logger.warning(
                f"🚫 BIOS 없는 모델 {len(still_failed)}개 → "
                f"{os.path.basename(NO_BIOS_LOG)} 에 영구 기록"
            )
    else:
        still_failed = []
        logger.info("✅ 실패 모델 없음, 재시도 생략")

    # ── 최종 SQLite 저장 ──
    save_to_sqlite(all_data)
    logger.info(
        f"\n✨ 전체 완료!\n"
        f"   ✅ 수집 성공: {len(completed_models)}개\n"
        f"   🚫 BIOS 없음: {len(still_failed)}개"
    )


# ──────────────────────────────────────────────────────────────────
#  헬퍼
# ──────────────────────────────────────────────────────────────────
def load_no_bios_log():
    if not os.path.exists(NO_BIOS_LOG):
        return set()
    try:
        with open(NO_BIOS_LOG, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except Exception:
        return set()


# ══════════════════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Gigabyte BIOS 스크래퍼")
    parser.add_argument(
        "--full", action="store_true",
        help="no_bios_log 모델 포함 전체 재수집"
    )
    parser.add_argument(
        "--recollect", action="store_true",
        help="수집 결과 캐시(final JSON) 포함 전체 초기화 후 재수집"
    )
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update(API_HEADERS)

    if os.path.exists(MASTER_FILE):
        with open(MASTER_FILE, "r", encoding="utf-8") as f:
            motherboards = json.load(f)
        logger.info(f"📂 마스터 파일 로드 (API 생략): {len(motherboards)}개 → {MASTER_FILE}")
    else:
        motherboards = collect_model_list(session)

    # 체크포인트는 매 실행마다 초기화 (크래시 복구 전용)
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        logger.info(f"🗑️  초기화: {os.path.basename(CHECKPOINT_FILE)}")

    # --recollect: 수집 결과 캐시도 초기화
    if args.recollect:
        if os.path.exists(FINAL_JSON):
            os.remove(FINAL_JSON)
            logger.info(f"🗑️  재수집 초기화: {os.path.basename(FINAL_JSON)}")

    if args.full or args.recollect:
        logger.info("🔄 전체 재수집 모드 (no_bios_log 포함)")
        collect_bios_data(motherboards)
    else:
        no_bios = load_no_bios_log()
        pending = [mb for mb in motherboards if mb["model_name"] not in no_bios]
        logger.info(
            f"📊 수집 시작 | 전체: {len(motherboards)}개 | "
            f"BIOS 없음(제외): {len(no_bios)}개 | "
            f"수집 대상: {len(pending)}개"
        )
        collect_bios_data(pending)


if __name__ == "__main__":
    main()

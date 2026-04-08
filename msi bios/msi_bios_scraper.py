"""
MSI BIOS Data Collector
crontab: 0 7 * * * python3 /path/to/msi_bios_scraper.py

실행 흐름:
  1단계 — 전체 메인보드 모델 리스트 수집
           /support/ajax/get_tag_list_by_product_line?id=8 → 시리즈 태그 목록
           /support/ajax/get_product_by_tag?id={tag_id}   → 시리즈별 product_link 목록
  2단계 — 병렬 BIOS 데이터 수집
           /api/v1/product/support/panel?product={link}&type=bios
  3단계 — 실패 모델 5분 대기 후 1회 재시도
  4단계 — 재시도 후에도 실패 = BIOS 없는 단종/특수 모델
           → msi_no_bios_models.log 에 영구 기록
"""

import argparse
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

try:
    from tqdm import tqdm
    USE_TQDM = True
except ImportError:
    USE_TQDM = False

# ══════════════════════════════════════════════════════════════════
#  설정값
# ══════════════════════════════════════════════════════════════════
CONFIG = {
    "workers":           2,
    "max_retries":       3,
    "timeout":           20,
    "delay_min":         2.0,
    "delay_max":         4.0,
    "save_interval":     20,
    "db_save_interval":  100,
    "retry_wait":        300,
    "timeout_cooldown":  30,
    "timeout_threshold": 5,
    "block_cooldown":    900,
    "block_max_retry":   3,
    "block_threshold":   5,
    "country_code":      "global",
}

# ══════════════════════════════════════════════════════════════════
#  경로
# ══════════════════════════════════════════════════════════════════
BASE_PATH       = os.path.dirname(os.path.abspath(__file__))
MASTER_FILE     = os.path.join(BASE_PATH, "msi_bios_motherboards_master.json")
FINAL_JSON      = os.path.join(BASE_PATH, "msi_bios_data_final.json")
CHECKPOINT_FILE = os.path.join(BASE_PATH, "msi_bios_checkpoint.json")
NO_BIOS_LOG     = os.path.join(BASE_PATH, "msi_bios_no_bios_models.log")
DB_FILE         = os.path.join(BASE_PATH, "msi_bios.db")

# ══════════════════════════════════════════════════════════════════
#  로거
# ══════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_PATH, "msi_bios_scraper.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  공통 헤더 / 락
# ══════════════════════════════════════════════════════════════════
HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.msi.com/support/download",
    "Origin":          "https://www.msi.com",
    "Sec-Fetch-Site":  "same-origin",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Ch-Ua":       '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile":"?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Connection":      "keep-alive",
}

HTML_HEADERS = {
    **HEADERS,
    "Accept":      "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Site": "none",
}

MSI_BASE          = "https://www.msi.com"
TAG_LIST_API      = f"{MSI_BASE}/support/ajax/get_tag_list_by_product_line"
PRODUCT_TAG_API   = f"{MSI_BASE}/support/ajax/get_product_by_tag"
BIOS_PANEL_API    = f"{MSI_BASE}/api/v1/product/support/panel"
SUPPORT_PAGE      = f"{MSI_BASE}/support/download"
MB_PRODUCT_LINE_ID = 8   # Motherboard product line ID

save_lock              = Lock()
print_lock             = Lock()
checkpoint_lock        = Lock()
timeout_lock           = Lock()
consecutive_timeouts   = {"count": 0}
consecutive_blocks     = {"count": 0}
completed_models_ref   = [set()]
block_cooldown_count   = {"count": 0}

# ──────────────────────────────────────────────────────────────────
#  유틸: 칩셋/브랜드 추출 + 체크포인트 키
# ──────────────────────────────────────────────────────────────────
_AMD_CHIPSETS = {
    "X870E","X870","X670E","X670","B850","B650E","B650","B550",
    "A620","A520","A320","X570E","X570","X470","X370","B450","B350",
}
_INTEL_CHIPSETS = {
    "Z890","Z790","Z690","Z590","Z490","Z390","Z370",
    "B760","B660","B560","B460","B365","B360",
    "H770","H670","H570","H470","H410","H310",
    "W790","W680","W580",
}

def extract_chipset_brand(model_id):
    """모델 ID에서 칩셋 및 CPU 브랜드 추출"""
    m = re.search(r'\b([A-Z]\d{3}E?)\b', model_id.upper())
    if m:
        chipset = m.group(1)
        if chipset in _AMD_CHIPSETS:
            return chipset, "AMD"
        if chipset in _INTEL_CHIPSETS:
            return chipset, "Intel"
    return "", ""


def ckpt_key(mb):
    """체크포인트 키: '{brand}|{chipset}|{model_id}' 형식"""
    return f"{mb.get('brand','')}|{mb.get('chipset','')}|{mb['model_id']}"


# ──────────────────────────────────────────────────────────────────
#  세션 초기화 (쿠키 획득 + CSRF 토큰 추출)
# ──────────────────────────────────────────────────────────────────
def init_session(session):
    """
    1) msi.com 메인 → 쿠키 적재
    2) support/download → CSRF 토큰 파싱
    세션 객체에 쿠키가 누적되므로 이후 API 호출에 자동 포함됨.
    """
    # 1단계: 메인 페이지 방문으로 기본 쿠키 획득
    try:
        session.get("https://www.msi.com", headers=HTML_HEADERS,
                    timeout=CONFIG["timeout"])
        time.sleep(random.uniform(1.0, 2.0))
    except Exception as e:
        logger.warning(f"메인 페이지 방문 실패 (무시): {e}")

    # 2단계: 지원 페이지 방문 → 추가 쿠키 + CSRF 토큰
    csrf_token = None
    try:
        resp = session.get(SUPPORT_PAGE, headers=HTML_HEADERS,
                           timeout=CONFIG["timeout"])
        if resp.status_code == 200:
            # <meta name="csrf-token" content="TOKEN">
            m = re.search(
                r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
                resp.text
            )
            if m:
                csrf_token = m.group(1)
                logger.info(f"✅ CSRF 토큰: {csrf_token[:20]}...")
            else:
                m = re.search(r'["\']_token["\']\s*:\s*["\']([^"\']+)["\']', resp.text)
                if m:
                    csrf_token = m.group(1)
                    logger.info(f"✅ CSRF 토큰 (JS): {csrf_token[:20]}...")
                else:
                    logger.warning("CSRF 토큰 미발견 — get_product_by_tag 실패 가능")
        else:
            logger.warning(f"지원 페이지 HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"지원 페이지 방문 실패: {e}")

    time.sleep(random.uniform(1.0, 2.0))
    return csrf_token


# ──────────────────────────────────────────────────────────────────
#  유틸: 재시도 포함 GET
# ──────────────────────────────────────────────────────────────────
def safe_get(session, url, params=None, retries=CONFIG["max_retries"]):
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, headers=HEADERS,
                               timeout=CONFIG["timeout"])
            if resp.status_code == 200:
                # 성공 시 카운터 초기화
                with timeout_lock:
                    consecutive_timeouts["count"] = 0
                    consecutive_blocks["count"]   = 0
                return resp

            if resp.status_code == 500:
                logger.debug(f"HTTP 500 (skip) | {url} {params}")
                return None

            if resp.status_code in (403, 451):
                with timeout_lock:
                    consecutive_blocks["count"] += 1
                    block_count = consecutive_blocks["count"]
                # 연속 차단이 임계값 초과 시 긴 쿨다운
                if block_count >= CONFIG["block_threshold"]:
                    with timeout_lock:
                        block_cooldown_count["count"] += 1
                        cooldown_n = block_cooldown_count["count"]
                    if cooldown_n > CONFIG["block_max_retry"]:
                        logger.error(
                            f"❌ 차단 쿨다운 {cooldown_n-1}회 초과 — 오늘 수집 종료, 내일 재시도"
                        )
                        raise SystemExit(1)
                    wait_min = CONFIG["block_cooldown"] // 60
                    logger.warning(
                        f"🚫 연속 차단({resp.status_code}) — {wait_min}분 대기 후 재개 "
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
                    logger.warning(
                        f"HTTP {resp.status_code} (일시 차단) {wait:.0f}초 대기 후 재시도... ({attempt}/{retries})"
                    )
                    time.sleep(wait)
                continue

            if resp.status_code == 429:
                wait = 2 ** attempt + random.uniform(1, 3)
                logger.warning(f"HTTP 429 {wait:.1f}초 대기 후 재시도... ({attempt}/{retries})")
                time.sleep(wait)
            else:
                logger.warning(f"HTTP {resp.status_code} | 시도 {attempt}/{retries}")

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
#  파싱: panel?type=bios → BIOS 목록
# ──────────────────────────────────────────────────────────────────
def parse_bios_response(raw_json):
    """
    /api/v1/product/support/panel?product={link}&type=bios 응답 파싱.

    구조: result.downloads = {
        "AMI BIOS": [ {download_version, download_release, download_size,
                        download_url, download_description}, ... ],
        "type_title": [...],  ← 스킵
        "os": [],             ← 스킵
    }
    """
    bios_list = []
    downloads = (raw_json.get("result") or {}).get("downloads") or {}

    skip_keys = {"type_title", "os"}
    for bios_type, entries in downloads.items():
        if bios_type in skip_keys or not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            raw_url = item.get("download_url") or ""
            bios_list.append({
                "version":      item.get("download_version", ""),
                "date":         item.get("download_release", ""),
                "info":         item.get("download_description", ""),
                "name":         raw_url.split("/")[-1] if raw_url else "",
                "download_url": raw_url,
            })

    return bios_list


# ──────────────────────────────────────────────────────────────────
#  단일 모델 처리
# ──────────────────────────────────────────────────────────────────
def process_model(mb, session):
    model_id   = mb["model_id"]
    model_name = mb.get("model_name", "")
    image_url  = mb.get("image_url", "")

    bios_list = []

    resp = safe_get(session, BIOS_PANEL_API, params={
        "product": model_id,
        "type":    "bios",
    })

    if resp:
        try:
            raw = resp.json()
            bios_list = parse_bios_response(raw)
        except Exception as e:
            logger.debug(f"파싱 오류 [{model_id}]: {e}")

    time.sleep(random.uniform(CONFIG["delay_min"], CONFIG["delay_max"]))

    return {
        "model_id":      model_id,
        "model_name":    model_name,
        "brand":         mb.get("brand", ""),
        "chipset":       mb.get("chipset", ""),
        "image_url":     image_url,
        "bios_page_url": f"{MSI_BASE}/Motherboard/{model_id}/support#BIOS",
        "bios_list":     bios_list,
    }


# ──────────────────────────────────────────────────────────────────
#  1단계: 마스터 리스트 수집
# ──────────────────────────────────────────────────────────────────
def collect_model_list(session):
    logger.info("📡 [1단계] MSI 메인보드 모델 리스트 수집 중...")

    # CSRF 토큰
    csrf_token = init_session(session)
    if not csrf_token:
        raise RuntimeError("CSRF 토큰 획득 실패 — 지원 페이지 접근 불가")

    # ── 시리즈 태그 목록 ───────────────────────────────────────────
    resp_tags = safe_get(session, TAG_LIST_API, params={
        "id":     MB_PRODUCT_LINE_ID,
        "_token": csrf_token,
    })
    if not resp_tags:
        raise RuntimeError("get_tag_list_by_product_line API 호출 실패")

    tags_data  = resp_tags.json()
    series_map = {}  # {tag_id: series_name}

    # 구조: filter_tag_list["1"] = Product Segment 태그 목록 (PRO/MEG/MPG/MAG 등)
    # 각 항목: {"tag_id": 223, "tag_title": "PRO Series", "tag_showed": 1, ...}
    filter_tag_list = tags_data.get("filter_tag_list") or {}

    # product_filter_type_array 가 허용된 filter_type만 포함 → 그 키들의 태그만 수집
    allowed_types = set(
        (tags_data.get("product_filter_type_array") or {"1": ""}).keys()
    )

    for ftype_id, tag_list in filter_tag_list.items():
        if ftype_id not in allowed_types:
            continue
        for tag in (tag_list or []):
            if not isinstance(tag, dict):
                continue
            if not tag.get("tag_showed") or not tag.get("tag_published"):
                continue
            tid  = tag.get("tag_id")
            name = (tag.get("tag_title") or "").strip()
            if tid and name:
                series_map[tid] = name

    if not series_map:
        logger.warning(f"시리즈 태그 찾지 못함. filter_tag_list 키: {list(filter_tag_list.keys())}")

    logger.info(f"📋 발견된 메인보드 시리즈: {len(series_map)}개 → {series_map}")

    # ── get_product_by_tag: 시리즈별 제품 수집 ───────────────────
    motherboards = []

    for series_id, series_name in series_map.items():
        params = {
            "id":           series_id,
            "product_line": "mb",
        }
        if csrf_token:
            params["_token"] = csrf_token

        resp_tag = safe_get(session, PRODUCT_TAG_API, params=params)
        if not resp_tag:
            logger.warning(f"시리즈 '{series_name}' (id={series_id}) 수집 실패, 건너뜀")
            continue

        try:
            tag_data = resp_tag.json()
        except Exception:
            logger.warning(f"시리즈 '{series_name}' JSON 파싱 실패")
            continue

        logger.info(f"  [{series_name} id={series_id}] 응답: {json.dumps(tag_data, ensure_ascii=False)[:500]}")

        # 제품 목록 추출
        if isinstance(tag_data, list):
            products = tag_data
        elif isinstance(tag_data, dict):
            products = (
                tag_data.get("result")
                or tag_data.get("Result")
                or tag_data.get("data")
                or tag_data.get("products")
                or []
            )
            if isinstance(products, dict):
                products = products.get("items") or products.get("list") or []
        else:
            products = []


        for p in products:
            if not isinstance(p, dict):
                continue
            model_id = p.get("link") or ""
            if not model_id:
                continue
            model_name = (
                p.get("title") or p.get("name") or p.get("product_name") or model_id
            ).strip()
            image_url = p.get("picture") or ""
            chipset, brand = extract_chipset_brand(model_id)
            motherboards.append({
                "series":     series_name,
                "model_id":   model_id,
                "model_name": model_name,
                "brand":      brand,
                "chipset":    chipset,
                "image_url":  image_url,
            })

        logger.info(f"  {series_name}: {len(products)}개 모델")
        time.sleep(random.uniform(0.5, 1.2))  # 시리즈 수집 간 간격

    # 중복 제거 (동일 product_link가 여러 시리즈에 속할 수 있음)
    seen = set()
    unique_mbs = []
    for mb in motherboards:
        if mb["model_id"] not in seen:
            seen.add(mb["model_id"])
            unique_mbs.append(mb)

    with open(MASTER_FILE, "w", encoding="utf-8") as f:
        json.dump(unique_mbs, f, ensure_ascii=False, indent=4)

    logger.info(f"✅ 모델 리스트 수집 완료: {len(unique_mbs)}개 → {MASTER_FILE}")
    return unique_mbs


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
            UNIQUE(model_id, version)
        )
    """)

    for col, default in [
        ("image_url",       "''"),
        ("brand",           "''"),
        ("chipset",         "''"),
        ("last_valid_date", "NULL"),
    ]:
        try:
            cur.execute(f"ALTER TABLE motherboards ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    for col, default in [("model_name", "''"), ("info", "''"), ("name", "''"), ("download_url", "''")]:
        try:
            cur.execute(f"ALTER TABLE bios_versions ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass

    for item in all_data:
        mid        = item["model_id"]
        model_name = item.get("model_name", "")
        has_bios   = bool(item.get("bios_list"))
        cur.execute("""
            INSERT INTO motherboards
                (model_id, model_name, brand, chipset, image_url,
                 updated_at, last_valid_date)
            VALUES (?, ?, ?, ?, ?, datetime('now','localtime'),
                    CASE WHEN ? THEN datetime('now','localtime') ELSE NULL END)
            ON CONFLICT(model_id) DO UPDATE SET
                model_name      = excluded.model_name,
                brand           = excluded.brand,
                chipset         = excluded.chipset,
                image_url       = CASE WHEN motherboards.image_url != '' THEN motherboards.image_url
                                       ELSE excluded.image_url END,
                updated_at      = excluded.updated_at,
                last_valid_date = CASE WHEN excluded.last_valid_date IS NOT NULL
                                       THEN excluded.last_valid_date
                                       ELSE motherboards.last_valid_date
                                  END
        """, (mid, model_name, item.get("brand", ""), item.get("chipset", ""),
              item.get("image_url", ""), 1 if has_bios else 0))
        for b in item.get("bios_list", []):
            cur.execute("""
                INSERT INTO bios_versions (model_id, model_name, version, date, info, name, download_url)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(model_id, version) DO UPDATE SET
                    date         = excluded.date,
                    info         = excluded.info,
                    name         = excluded.name,
                    download_url = excluded.download_url
            """, (mid, model_name, b["version"], b["date"], b["info"], b["name"], b["download_url"]))
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
        s = requests.Session()
        s.headers.update(HEADERS)
        return process_model(mb, s)

    progress = tqdm(total=total, initial=done_offset,
                    desc=desc, unit="모델") if USE_TQDM else None

    with ThreadPoolExecutor(max_workers=CONFIG["workers"]) as executor:
        future_to_mb = {executor.submit(worker, mb): mb for mb in pending_mbs}

        for future in as_completed(future_to_mb):
            mb    = future_to_mb[future]
            model = mb["model_id"]

            try:
                result = future.result()
            except Exception as e:
                logger.error(f"🔥 예외 발생 [{model}]: {e}")
                result = None

            counter["n"] += 1
            n = counter["n"]

            if result:
                bios_count = len(result["bios_list"])
                with save_lock:
                    all_data.append(result)
                    if bios_count > 0:
                        completed_models.add(ckpt_key(mb))
                    else:
                        failed_mbs.append(mb)
                with print_lock:
                    if USE_TQDM:
                        progress.set_postfix_str(f"{model[:20]} | 💾 {bios_count}개")
                        progress.update(1)
                    else:
                        print(f"✅ [{n}/{total}] {model[:30].ljust(30)} | 💾 {bios_count}개")
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
        logger.info(f"⏩ Resume 모드: {len(completed_models)}개 이미 완료")

    all_data = []
    if os.path.exists(FINAL_JSON):
        try:
            with open(FINAL_JSON, "r", encoding="utf-8") as f:
                all_data = json.load(f)
        except Exception:
            all_data = []

    pending = [mb for mb in motherboards if ckpt_key(mb) not in completed_models]
    total   = len(motherboards)
    done    = len(completed_models)

    all_data, completed_models, failed_mbs = run_collection(
        pending_mbs=pending, total=total, done_offset=done,
        completed_models=completed_models, all_data=all_data, desc="수집 중",
    )
    _save_results(all_data, completed_models)
    logger.info(
        f"\n📊 1차 수집 완료 | "
        f"성공: {len(completed_models)}개 | 실패: {len(failed_mbs)}개"
    )

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
            pending_mbs=failed_mbs, total=retry_total, done_offset=0,
            completed_models=completed_models, all_data=all_data, desc="재시도",
        )
        _save_results(all_data, completed_models)
        logger.info(
            f"\n📊 재시도 완료 | "
            f"성공: {retry_total - len(still_failed)}개 | "
            f"최종 실패: {len(still_failed)}개"
        )
        if still_failed:
            append_no_bios_log([ckpt_key(mb) for mb in still_failed])
    else:
        still_failed = []
        logger.info("✅ 실패 모델 없음, 재시도 생략")

    save_to_sqlite(all_data)
    logger.info(
        f"\n✨ 전체 완료!\n"
        f"   ✅ 수집 성공:  {len(completed_models)}개\n"
        f"   🚫 BIOS 없음:  {len(still_failed)}개"
    )


# ──────────────────────────────────────────────────────────────────
#  디버그 도우미: 단일 모델 응답 출력
# ──────────────────────────────────────────────────────────────────
def debug_single(product_link):
    """
    python "msi bios scraper.py" --debug A320M-PRO-C
    실제 API 응답 구조를 출력해 parse_bios_response 수정에 활용.
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    resp = safe_get(session, BIOS_PANEL_API, params={
        "product": product_link,
        "type":    "bios",
    })
    if resp:
        data = resp.json()
        print("=== panel?type=bios 응답 ===")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print("\n=== parse_bios_response 결과 ===")
        print(json.dumps(parse_bios_response(data), ensure_ascii=False, indent=2))
    else:
        print(f"API 호출 실패: {product_link}")


# ══════════════════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════════════════
def load_no_bios_log():
    if not os.path.exists(NO_BIOS_LOG):
        return set()
    try:
        with open(NO_BIOS_LOG, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except Exception:
        return set()


def main():
    parser = argparse.ArgumentParser(description="MSI BIOS 스크래퍼")
    parser.add_argument("--recollect", action="store_true", help="no_bios_log 포함 전체 재수집")
    parser.add_argument("--debug", metavar="MODEL_ID",
                        help="단일 모델 API 응답 구조 출력 (예: A320M-PRO-C)")
    parser.add_argument("--data-dir", default=None,
                        help="DB 저장 경로 (기본: 스크래퍼 폴더)")
    args = parser.parse_args()

    global DB_FILE
    if args.data_dir:
        DB_FILE = os.path.join(args.data_dir, os.path.basename(DB_FILE))

    if args.debug:
        debug_single(args.debug)
        return

    session = requests.Session()
    session.headers.update(HEADERS)

    motherboards = collect_model_list(session)

    for path in [CHECKPOINT_FILE, FINAL_JSON]:
        if os.path.exists(path):
            os.remove(path)
            logger.info(f"🗑️  초기화: {os.path.basename(path)} 삭제")

    no_bios = set() if args.recollect else load_no_bios_log()
    pending = [mb for mb in motherboards if ckpt_key(mb) not in no_bios]
    logger.info(
        f"📊 수집 시작 | 전체: {len(motherboards)}개 | "
        f"BIOS 없음(제외): {len(no_bios)}개 | "
        f"수집 대상: {len(pending)}개"
    )
    collect_bios_data(pending)


if __name__ == "__main__":
    main()

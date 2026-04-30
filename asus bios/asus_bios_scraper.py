"""
ASUS BIOS Data Collector
crontab: 0 7 * * * python3 /path/to/asus_bios_scraper.py

실행 흐름:
  1단계 — 전체 메인보드 모델 리스트 수집
  2단계 — 병렬 BIOS 데이터 수집
           GetPDSupportTab → URL에서 API용 모델명 추출 → GetPDBIOS 호출
  3단계 — 실패 모델 5분 대기 후 1회 재시도
  4단계 — 재시도 후에도 실패 = BIOS 없는 단종/특수 모델
           → asus_no_bios_models.log 에 영구 기록
"""

import argparse
import json
import os
import re
import time
import random
import logging
from datetime import datetime
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import requests

try:
    from tqdm import tqdm
    USE_TQDM = True
except ImportError:
    USE_TQDM = False

# ══════════════════════════════════════════════════════════════════
#  설정값
# ══════════════════════════════════════════════════════════════════
CONFIG = {
    "workers":          2,    # 3 → 2, 차단 줄이기
    "max_retries":      3,
    "tab_retries":      1,    # TAB_API는 실패해도 fallback 있으므로 1회만
    "timeout":          20,
    "delay_min":        2.0,  # 요청 간격 늘리기
    "delay_max":        4.0,
    "save_interval":    20,   # JSON + 체크포인트 저장 주기
    "db_save_interval": 100,  # SQLite 저장 주기
    "retry_wait":       300,  # 재시도 전 대기 시간 (초, 5분)
    "timeout_cooldown": 30,   # 연속 타임아웃 N회 발생 시 대기 시간(초)
    "timeout_threshold": 5,   # 연속 타임아웃 몇 회부터 쿨다운 적용
    "block_cooldown":   900,  # 연속 451 N회 발생 시 대기 시간(초) — 15분
    "block_max_retry":  3,    # 451 쿨다운 최대 횟수, 초과 시 종료
    "block_threshold":  5,    # 연속 451 몇 회부터 쿨다운 적용
}

# ══════════════════════════════════════════════════════════════════
#  경로
# ══════════════════════════════════════════════════════════════════
BASE_PATH       = os.path.dirname(os.path.abspath(__file__))
MASTER_FILE     = os.path.join(BASE_PATH, "asus_motherboards_master.json")
FINAL_JSON      = os.path.join(BASE_PATH, "asus_bios_data_final.json")
CHECKPOINT_FILE = os.path.join(BASE_PATH, "asus_checkpoint.json")
NO_BIOS_LOG     = os.path.join(BASE_PATH, "asus_no_bios_models.log")  # 영구 불가 모델
DB_FILE         = os.path.join(BASE_PATH, "asus_bios.db")

# ══════════════════════════════════════════════════════════════════
#  로거
# ══════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_PATH, "asus_scraper.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  공통 헤더 / 락
# ══════════════════════════════════════════════════════════════════
HEADERS = {
    "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36",
    "Referer":          "https://www.asus.com/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept":           "application/json, text/javascript, */*; q=0.01",
    "Accept-Language":  "en-US,en;q=0.9",
}

save_lock       = Lock()
print_lock      = Lock()
checkpoint_lock = Lock()
timeout_lock    = Lock()
consecutive_timeouts  = {"count": 0}  # 전체 워커 공유 연속 타임아웃 카운터
consecutive_blocks    = {"count": 0}  # 전체 워커 공유 연속 451 카운터
completed_models_ref  = [set()]       # run_collection 에서 주입, 451 쿨다운 시 체크포인트 저장용
block_cooldown_count  = {"count": 0}  # 451 쿨다운 누적 횟수

BIOS_API = "https://www.asus.com/support/api/product.asmx/GetPDBIOS"
TAB_API  = "https://www.asus.com/support/api/product.asmx/GetPDSupportTab"


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
                    consecutive_blocks["count"] = 0
                return resp
            if resp.status_code == 500:
                logger.debug(f"HTTP 500 (skip) | {params}")
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
                        f"🚫 연속 451 차단 — {wait_min}분 대기 후 재개 ({cooldown_n}/{CONFIG['block_max_retry']})"
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
                logger.warning(f"HTTP {resp.status_code} | 시도 {attempt}/{retries}")
        except requests.exceptions.Timeout:
            with timeout_lock:
                consecutive_timeouts["count"] += 1
                count = consecutive_timeouts["count"]
            logger.warning(f"Timeout | 시도 {attempt}/{retries}")
            # 연속 타임아웃이 임계값 초과 시 쿨다운
            if count >= CONFIG["timeout_threshold"]:
                logger.warning(
                    f"⚠️  연속 타임아웃 {count}회 — {CONFIG['timeout_cooldown']}초 대기 후 재시도"
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
#  파싱
# ──────────────────────────────────────────────────────────────────
def parse_bios_response(raw_json):
    bios_list = []
    for obj in ((raw_json.get("Result") or {}).get("Obj") or []):
        for f in (obj.get("Files") or []):
            url = (
                (f.get("DownloadUrl") or {}).get("Global")
                or (f.get("DownloadUrl") or {}).get("Origin")
                or ""
            )
            bios_list.append({
                "version":     f.get("Version", ""),
                "date":        (lambda d: (
                    datetime(*[int(x) for x in d.split("/")]).strftime("%Y-%m-%d")
                    if d and d.count("/") == 2
                    else d
               ))(f.get("ReleaseDate", "")),
                "size":        f.get("FileSize", ""),
                "description": f.get("Description", ""),
                "link":        url,
            })
    return bios_list


def extract_api_model_from_tab(raw_json):
    """
    GetPDSupportTab 응답에서 BIOS 페이지 URL, API용 모델명, 이미지 URL 추출.
    예) /supportonly/A55BM-PLUSCSM/HelpDesk_BIOS/ → "A55BM-PLUSCSM"
    실패 시 (bios_page_url, None, img_url) 반환.
    """
    result        = raw_json.get("Result") or {}
    bios_page_url = "N/A"
    api_model     = None
    img_url       = result.get("PDImgUrl", "")  # 최상위 Result 바로 아래 위치

    for section in (result.get("Obj") or []):
        for item in (section.get("Items") or []):
            if item.get("Type") == "HelpDesk_BIOS":
                bios_page_url = item.get("Url", "N/A")
                m = re.search(r'/supportonly/([^/]+)/HelpDesk_BIOS/', bios_page_url)
                if m:
                    api_model = m.group(1)
                break

    return bios_page_url, api_model, img_url


# ──────────────────────────────────────────────────────────────────
#  단일 모델 처리
# ──────────────────────────────────────────────────────────────────
def process_model(mb, session):
    model    = mb["model_name"]
    pdid     = mb.get("pdid", "")
    platform = mb.get("platform", "")

    SITE_ORDER = ["us", "global", "me-en"]

    bios_list     = []
    bios_page_url = "N/A"
    img_url       = ""
    used_fallback = False

    # Step 1: GetPDSupportTab → API용 모델명 추출
    api_model = None
    if pdid:
        resp_tab = safe_get(session, TAB_API,
                            params={"website": "global", "pdid": pdid, "model": model},
                            retries=CONFIG["tab_retries"])
        if resp_tab:
            try:
                bios_page_url, api_model, img_url = extract_api_model_from_tab(resp_tab.json())
            except Exception:
                pass

    if not api_model:
        api_model = model
        logger.debug(f"api_model 추출 실패, 원본 사용: {model}")

    # Step 2: 추출한 모델명으로 GetPDBIOS 호출 (us → global → me-en)
    for i, site in enumerate(SITE_ORDER):
        resp = safe_get(session, BIOS_API,
                        params={"website": site, "model": api_model})
        if resp:
            try:
                bios_list = parse_bios_response(resp.json())
            except Exception:
                bios_list = []
            if bios_list:
                used_fallback = (i > 0)
                break

    # Step 3: api_model 실패 시 원본 모델명으로 한번 더
    if not bios_list and api_model != model:
        logger.debug(f"api_model 실패, 원본 재시도: {model}")
        for i, site in enumerate(SITE_ORDER):
            resp = safe_get(session, BIOS_API,
                            params={"website": site, "model": model})
            if resp:
                try:
                    bios_list = parse_bios_response(resp.json())
                except Exception:
                    bios_list = []
                if bios_list:
                    used_fallback = (i > 0)
                    break

    time.sleep(random.uniform(CONFIG["delay_min"], CONFIG["delay_max"]))

    return {
        "platform":      platform,
        "model_name":    model,
        "image_url":     img_url,
        "product_url":   bios_page_url,
        "bios_list":     bios_list,
        "used_fallback": used_fallback,
    }


# ──────────────────────────────────────────────────────────────────
#  1단계: 마스터 리스트 수집
# ──────────────────────────────────────────────────────────────────
def collect_model_list(session):
    logger.info("📡 [1단계] ASUS 메인보드 모델 리스트 수집 중...")
    motherboards = []
    api_pd_level = "https://www.asus.com/support/api/product.asmx/GetPDLevel"

    resp_p    = None
    list_site = "us"
    for site in ["us", "global", "me-en"]:
        resp_p = safe_get(session, api_pd_level, params={
            "website": site, "type": "1", "typeid": "1156",
            "productflag": "0", "siteid": "1"
        })
        if resp_p:
            list_site = site
            break
    if not resp_p:
        raise RuntimeError("플랫폼 리스트 API 호출 실패 (us/global/me-en 모두 실패)")

    platforms = (
        (resp_p.json().get("Result") or {})
        .get("ProductLevel", {})
        .get("Products", {})
        .get("Items", [])
    )

    for p in platforms:
        p_id, p_name = p.get("Id"), p.get("Name")
        resp_m = safe_get(session, api_pd_level, params={
            "website": list_site, "type": "2", "typeid": p_id,
            "productflag": "1", "siteid": "1"
        })
        if not resp_m:
            logger.warning(f"플랫폼 '{p_name}' 수집 실패, 건너뜀")
            continue
        for m in (resp_m.json().get("Result") or {}).get("Product", []):
            motherboards.append({
                "platform":   p_name,
                "model_name": m.get("PDName"),
                "pdid":       m.get("PDId"),
            })

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
            updated_at      TEXT,
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


def save_to_sqlite(all_data):
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
            updated_at      TEXT,
            last_valid_date TEXT
        )
    """)
    _migrate_db(conn)
    for col, default in [
        ("series",          "NULL"),
        ("form_factor",     "NULL"),
        ("product_url",     "NULL"),
        ("image_url",       "''"),
        ("category",        "''"),
        ("last_valid_date", "NULL"),
    ]:
        try:
            cur.execute(f"ALTER TABLE motherboards ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass

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
        model      = item["model_name"]
        bios_count = len(item.get("bios_list", []))
        cur.execute("""
            INSERT INTO motherboards (model_name, platform, product_url, image_url,
                                      updated_at, last_valid_date)
            VALUES (?, ?, ?, ?, datetime('now','localtime'),
                    CASE WHEN ? THEN datetime('now','localtime') ELSE NULL END)
            ON CONFLICT(model_name) DO UPDATE SET
                platform        = excluded.platform,
                product_url     = excluded.product_url,
                image_url       = CASE WHEN motherboards.image_url != '' THEN motherboards.image_url
                                       ELSE excluded.image_url END,
                updated_at      = excluded.updated_at,
                last_valid_date = CASE WHEN excluded.last_valid_date IS NOT NULL
                                       THEN excluded.last_valid_date
                                       ELSE motherboards.last_valid_date
                                  END
        """, (model, item.get("platform", ""), item.get("product_url", ""),
              item.get("image_url", ""), bios_count > 0))
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
    """
    pending_mbs 를 병렬 수집.
    반환: (성공 결과 추가된 all_data, 업데이트된 completed_models, 실패 mb 목록)
    """
    failed_mbs = []
    counter    = {"n": done_offset}

    # 451 쿨다운 시 체크포인트 저장을 위해 현재 completed_models 공유
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
            model = mb["model_name"]

            try:
                result = future.result()
            except Exception as e:
                logger.error(f"🔥 예외 발생 [{model}]: {e}")
                result = None

            counter["n"] += 1
            n = counter["n"]

            if result:
                bios_count    = len(result["bios_list"])
                fallback_mark = " 🔄" if result.get("used_fallback") else ""

                with save_lock:
                    all_data.append(result)
                    if bios_count > 0:
                        completed_models.add(model)
                    else:
                        failed_mbs.append(mb)

                with print_lock:
                    if USE_TQDM:
                        progress.set_postfix_str(
                            f"{model[:20]} | 💾 {bios_count}개{fallback_mark}")
                        progress.update(1)
                    else:
                        print(f"✅ [{n}/{total}] "
                              f"{model[:25].ljust(25)} | "
                              f"💾 {bios_count}개{fallback_mark}")
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
        logger.info(f"⏩ Resume 모드: {len(completed_models)}개 이미 완료, 나머지만 수집")
  
    all_data = []
    if os.path.exists(FINAL_JSON):
        try:
            with open(FINAL_JSON, "r", encoding="utf-8") as f:
                all_data = json.load(f)
            logger.info(f"📂 기존 데이터 로드: {len(all_data)}개")
        except Exception:
            all_data = []
  
    pending = [mb for mb in motherboards if mb["model_name"] not in completed_models]
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
        logger.info(f"\n⏳ [3단계] 실패 모델 {len(failed_mbs)}개 → "
                    f"{CONFIG['retry_wait'] // 60}분 후 재시도...")
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

        # 재시도 후에도 실패 = BIOS 없는 단종/특수 모델 → 영구 분류
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

    # ── 최종 SQLite 저장 ──
    save_to_sqlite(all_data)

    logger.info(
        f"\n✨ 전체 완료!\n"
        f"   ✅ 수집 성공: {len(completed_models)}개\n"
        f"   🚫 BIOS 없음: {len(still_failed_mbs)}개"
    )


# ──────────────────────────────────────────────────────────────────
#  헬퍼
# ──────────────────────────────────────────────────────────────────
def load_no_bios_log():
    """asus_no_bios_models.log 에서 BIOS 없는 모델명 집합 반환"""
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
    parser = argparse.ArgumentParser(description="ASUS BIOS 스크래퍼")
    parser.add_argument(
        "--full", action="store_true",
        help="no_bios_log 모델 포함 전체 재수집"
    )
    parser.add_argument(
        "--data-dir", default=None,
        help="DB 저장 경로 (기본: 스크래퍼 폴더)"
    )
    args = parser.parse_args()

    global DB_FILE
    if args.data_dir:
        DB_FILE = os.path.join(args.data_dir, os.path.basename(DB_FILE))

    session = requests.Session()
    session.headers.update(HEADERS)
  
    motherboards = collect_model_list(session)
  
    # 체크포인트는 매 실행마다 초기화 (크래시 복구 전용)
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        logger.info(f"🗑️  초기화: {os.path.basename(CHECKPOINT_FILE)} 삭제")
  
    if args.full:
        # no_bios_log 포함 전체 재수집
        logger.info("🔄 전체 재수집 모드 (no_bios_log 포함)")
        collect_bios_data(motherboards)
    else:
        # 기본: no_bios_log 모델 제외 후 전체 수집
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
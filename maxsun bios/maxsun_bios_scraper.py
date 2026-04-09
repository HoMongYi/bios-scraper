"""
Maxsun BIOS Data Collector
crontab: 0 7 * * * python3 /path/to/maxsun_bios_scraper.py

사이트 구조:
  URL: https://www.maxsun.com/ko/pages/support
  드롭다운 4단계:
    1. Select Product       → "Motherboard"
    2. Select Chipset Brand → "Intel" / "AMD"
    3. Select Chipset       → e.g. "B850", "Z890", ...
    4. Select Model         → e.g. "MS-eSport B850ITX WIFI ICE"
  BIOS 테이블:
    BIOS version | date | info | name | add.1(DOWN 다운로드 링크)

실행 흐름:
  1단계 — Playwright: Product→Brand→Chipset→Model 순회 + BIOS 테이블 즉시 파싱
  2단계 — 실패 모델 수집
  3단계 — 3분 대기 후 1회 재시도
  4단계 — 재시도 후에도 실패 = BIOS 없는 단종/특수 모델
           → maxsun_no_bios_models.log 에 영구 기록
"""

import argparse
import json
import os
import re
import time
import random
import logging
import sqlite3
import urllib.parse
from threading import Lock

import requests

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
    "delay_min":     0.8,
    "delay_max":     2.0,
    "retry_wait":    180,    # 재시도 대기 (초)
    "page_timeout":  30000,
    "dropdown_wait": 2500,   # 드롭다운 변경 후 JS 갱신 대기 (ms)
    "table_wait":    8000,   # BIOS 테이블 로드 대기 (ms)
    "headless":      True,
    "debug":         False,
    # driversearch.html 고정 셀렉터
    "sel_product": "#pro_b_region",
    "sel_brand":   "#zb_b_region",       # 메인보드 브랜드 (INTEL/AMD)
    "sel_chipset": "#zb_b_country",      # 칩셋 (Z590, H610, ...)
    "sel_model":   "#zb_b_producttype",  # 모델명 (MS-...)
}

# ══════════════════════════════════════════════════════════════════
#  URL 상수
# ══════════════════════════════════════════════════════════════════
SUPPORT_URL = "https://myshopify.maxsun.com.cn/search/driversearch.html"
BASE_URL    = "https://myshopify.maxsun.com.cn"

# ══════════════════════════════════════════════════════════════════
#  경로
# ══════════════════════════════════════════════════════════════════
BASE_PATH       = os.path.dirname(os.path.abspath(__file__))
FINAL_JSON      = os.path.join(BASE_PATH, "maxsun_bios_data_final.json")
CHECKPOINT_FILE = os.path.join(BASE_PATH, "maxsun_checkpoint.json")
NO_BIOS_LOG     = os.path.join(BASE_PATH, "maxsun_no_bios_models.log")
DB_FILE         = os.path.join(BASE_PATH, "maxsun_bios.db")

# ══════════════════════════════════════════════════════════════════
#  로거
# ══════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_PATH, "maxsun_scraper.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  공통 락
# ══════════════════════════════════════════════════════════════════
checkpoint_lock = Lock()


# ──────────────────────────────────────────────────────────────────
#  Playwright 브라우저
# ──────────────────────────────────────────────────────────────────
def _make_browser(playwright):
    browser = playwright.chromium.launch(
        headless=CONFIG["headless"],
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 900},
        locale="ko-KR",
    )
    page = ctx.new_page()
    stealth_sync(page)
    return browser, ctx, page


def _debug_save(html: str, tag: str):
    path = os.path.join(BASE_PATH, f"debug_{tag}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"🐛 HTML 저장: {path}")


# ──────────────────────────────────────────────────────────────────
#  드롭다운 헬퍼
# ──────────────────────────────────────────────────────────────────
def _detect_selects(page) -> dict:
    """
    페이지에서 4개 드롭다운 셀렉터를 자동 감지.
    레이블 텍스트 기반으로 product/brand/chipset/model 매핑.
    """
    result = page.evaluate("""() => {
        const selects = Array.from(document.querySelectorAll('select'));
        return selects.map((sel, idx) => {
            let labelText = '';

            // 1) <label for="id"> 탐색
            if (sel.id) {
                try {
                    const lbl = document.querySelector(`label[for="${CSS.escape(sel.id)}"]`);
                    if (lbl) labelText = lbl.textContent.trim();
                } catch(e) {}
            }

            // 2) 부모 래퍼 텍스트 (select/option 제거 후)
            if (!labelText) {
                const wrap = sel.parentElement;
                if (wrap) {
                    const clone = wrap.cloneNode(true);
                    clone.querySelectorAll('select, option').forEach(e => e.remove());
                    labelText = clone.textContent.replace(/\\s+/g, ' ').trim();
                }
            }

            // 3) 이전 형제 요소 텍스트
            if (!labelText) {
                let prev = sel.previousElementSibling;
                while (prev) {
                    const t = prev.textContent.trim();
                    if (t) { labelText = t; break; }
                    prev = prev.previousElementSibling;
                }
            }

            const options = Array.from(sel.options)
                .filter(o => o.value.trim())
                .map(o => ({ value: o.value.trim(), text: o.text.trim() }));

            return {
                idx,
                id:       sel.id   || '',
                name:     sel.name || '',
                selector: sel.id   ? `#${sel.id}` : `select:nth-of-type(${idx + 1})`,
                label:    labelText,
                options_preview: options.slice(0, 5),
            };
        });
    }""")

    logger.info(f"감지된 드롭다운 수: {len(result)}")
    for r in result:
        logger.info(f"  [{r['idx']}] label='{r['label']}' "
                    f"selector='{r['selector']}' "
                    f"preview={r['options_preview']}")

    mapping = {"product": None, "brand": None, "chipset": None, "model": None}
    label_map = [
        ("product", ["product", "제품"]),
        ("brand",   ["brand", "브랜드", "chipset brand"]),
        ("chipset", ["chipset", "칩셋"]),
        ("model",   ["model", "모델"]),
    ]
    for item in result:
        lbl = item["label"].lower()
        for key, keywords in label_map:
            if mapping[key] is None and any(k in lbl for k in keywords):
                mapping[key] = item["selector"]
                break

    # Fallback: 위치 순서로 채우기 (product=0, brand=1, chipset=2, model=3)
    ordered = sorted(result, key=lambda x: x["idx"])
    for i, key in enumerate(["product", "brand", "chipset", "model"]):
        if mapping[key] is None and i < len(ordered):
            mapping[key] = ordered[i]["selector"]
            logger.warning(f"  ⚠️ '{key}' 위치 기반 fallback → {ordered[i]['selector']}")

    logger.info(f"드롭다운 매핑: {mapping}")
    return mapping


def _get_select_options(page, selector: str) -> list:
    """<select> 유효 옵션 반환: [(value, text), ...]"""
    if not selector:
        return []
    try:
        opts = page.evaluate(f"""() => {{
            const sel = document.querySelector('{selector}');
            if (!sel) return [];
            return Array.from(sel.options)
            .filter(o => o.value.trim() && o.value.trim() !== '0' && !o.value.trim().startsWith('.'))
            .map(o => [o.value.trim(), o.text.trim()]);
        }}""")
        return opts or []
    except Exception as e:
        logger.warning(f"옵션 읽기 실패 [{selector}]: {e}")
        return []


def _select_option(page, selector: str, value: str):
    """<select>에서 value로 옵션 선택"""
    if not selector:
        return
    try:
        page.select_option(selector, value=value)
    except Exception as e:
        logger.warning(f"옵션 선택 실패 [{selector}={value}]: {e}")


# ──────────────────────────────────────────────────────────────────
#  BIOS 테이블 파싱
# ──────────────────────────────────────────────────────────────────
def _extract_download_url(cells: list, dl_idx) -> str:
    """
    셀 목록에서 다운로드 URL 추출.
    우선순위: 지정 컬럼 href → onclick 내 URL → 행 전체 첫 번째 download 링크
    """
    def _href_from_cell(cell) -> str:
        for a in cell.find_all("a"):
            href = (a.get("href") or "").strip()
            if href and href not in ("#", "javascript:void(0)", ""):
                return href if href.startswith("http") else BASE_URL + href
            # onclick 에서 URL 추출 (window.open('url') 또는 location.href='url' 패턴)
            onclick = (a.get("onclick") or "").strip()
            if onclick:
                m = re.search(r"""['\"](https?://[^'\"]+)['\"]""", onclick)
                if not m:
                    m = re.search(r'["\']([^"\']+\.(?:rar|zip|rom|bin))["\']', onclick, re.I)
                if m:
                    url = m.group(1).strip()
                    return url if url.startswith("http") else BASE_URL + url
        return ""

    # 1) 지정된 다운로드 컬럼
    if dl_idx is not None and dl_idx < len(cells):
        url = _href_from_cell(cells[dl_idx])
        if url:
            return url

    # 2) 모든 셀을 역순으로 탐색 (다운로드 링크는 보통 오른쪽)
    for cell in reversed(cells):
        url = _href_from_cell(cell)
        if url:
            return url

    return ""


def _parse_bios_table(html: str) -> list:
    """
    Maxsun 드라이버 검색 결과 페이지에서 BIOS 테이블 파싱.

    실제 페이지 구조 (.showall div 안):
      <table>
        <tr>  ← Row 0: 섹션 제목 (colspan=6) "BIOS Update Download"
        <tr>  ← Row 1: 컬럼 헤더 — BIOS version | Date | Info | Name | Add.1 | Add.2
        <tr>  ← Row 2+: 데이터 행
    다운로드 링크: Add.1 셀의 <a href="https://download.maxsun.com.cn:8443/..."> 
    """
    soup = BeautifulSoup(html, "html.parser")
    bios_list = []

    # .showall div 안으로 스코프 제한 (드라이버/매뉴얼 테이블 혼입 방지)
    container = soup.find(class_="showall") or soup

    for tbl in container.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 3:
            continue

        # Row 0: 섹션 제목 — "BIOS Update Download" 인지 확인
        first_text = rows[0].get_text(strip=True).lower()
        if "bios" not in first_text:
            continue

        # Row 1: 컬럼 헤더 매핑
        col_map = {}
        for i, cell in enumerate(rows[1].find_all(["th", "td"])):
            text = cell.get_text(strip=True).lower()
            if "version" in text or "bios" in text:
                col_map["version"] = i
            elif "date" in text:
                col_map["date"] = i
            elif "info" in text or "description" in text:
                col_map["info"] = i
            elif "name" in text:
                col_map["name"] = i
            elif "add" in text or "down" in text:
                col_map["download"] = i

        if not col_map:
            col_map = {"version": 0, "date": 1, "info": 2, "name": 3, "download": 4}

        # Row 2+: 데이터
        for row in rows[2:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            def _cell_text(key: str) -> str:
                idx = col_map.get(key)
                if idx is None or idx >= len(cells):
                    return ""
                return cells[idx].get_text(" ", strip=True)

            version = _cell_text("version")
            if not version:
                continue

            dl_url = _extract_download_url(cells, col_map.get("download"))

            bios_list.append({
                "version":      version,
                "date":         _cell_text("date"),
                "info":         _cell_text("info"),
                "name":         _cell_text("name"),
                "download_url": dl_url,
            })

    return bios_list


# ──────────────────────────────────────────────────────────────────
#  이미지 URL 조회
# ──────────────────────────────────────────────────────────────────
SEARCH_BASE_URL = "https://www.maxsun.com/ko/search"
_IMG_SESSION = requests.Session()
_IMG_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
})


def fetch_image_url(model_name: str, timeout: int = 10) -> str:
    """
    Maxsun 상품 검색 결과 첫 번째 이미지 URL 반환.
    URL: https://www.maxsun.com/ko/search?type=product&q=[model_name]
    product-list 클래스 안의 첫 번째 상품 이미지만 사용.
    """
    url = SEARCH_BASE_URL + "?type=product&q=" + urllib.parse.quote(model_name)
    try:
        resp = _IMG_SESSION.get(url, timeout=timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        container = soup.find(class_="product-list")
        if container is None:
            return ""
        for img in container.find_all("img"):
            src = (img.get("src") or img.get("data-src") or "").strip()
            if not src:
                continue
            if "cdn.shopify" in src or "/cdn/shop/" in src or "/products/" in src:
                if src.startswith("//"):
                    src = "https:" + src
                elif src.startswith("/"):
                    src = "https://www.maxsun.com" + src
                return src
    except Exception as e:
        logger.debug(f"이미지 URL 조회 실패 [{model_name}]: {e}")
    return ""


# ──────────────────────────────────────────────────────────────────
#  체크포인트
# ──────────────────────────────────────────────────────────────────
def load_checkpoint() -> set:
    if not os.path.exists(CHECKPOINT_FILE):
        return set()
    try:
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_checkpoint(completed: set):
    with checkpoint_lock:
        with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            json.dump(list(completed), f)


def _save_results(all_data: list, completed: set):
    with open(FINAL_JSON, "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=4)
    save_checkpoint(completed)


# ──────────────────────────────────────────────────────────────────
#  SQLite 저장
# ──────────────────────────────────────────────────────────────────
def save_to_sqlite(all_data: list):
    conn = sqlite3.connect(DB_FILE)
    cur  = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS motherboards (
            model_id        TEXT PRIMARY KEY,
            model_name      TEXT,
            brand           TEXT DEFAULT '',
            chipset         TEXT DEFAULT '',
            image_url       TEXT DEFAULT '',
            updated_at      TEXT DEFAULT (datetime('now','localtime')),
            last_valid_date TEXT
        );
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
        );
    """)

    # 스키마 마이그레이션 (기존 DB 호환)
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

    for item in all_data:
        mid      = item.get("model_id") or ""
        has_bios = bool(item.get("bios_list"))
        cur.execute("""
            INSERT INTO motherboards
                (model_id, model_name, brand, chipset, image_url,
                 updated_at, last_valid_date)
            VALUES (?,?,?,?,?,datetime('now','localtime'),
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
        """, (mid, item.get("model_name",""),
               item.get("brand",""), item.get("chipset",""),
               item.get("image_url",""), 1 if has_bios else 0))

        for b in item.get("bios_list", []):
            cur.execute("""
                INSERT INTO bios_versions
                    (model_id, model_name, version, date, info, name, download_url)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(model_id, version) DO UPDATE SET
                    date         = excluded.date,
                    info         = excluded.info,
                    name         = excluded.name,
                    download_url = excluded.download_url
            """, (mid, item.get("model_name",""), b.get("version",""),
                   b.get("date",""), b.get("info",""),
                   b.get("name",""), b.get("download_url","")))

    conn.commit()
    conn.close()
    logger.info(f"💾 SQLite 저장 완료: {len(all_data)}개 모델 → {DB_FILE}")


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


# ──────────────────────────────────────────────────────────────────
#  1단계: Playwright 전체 수집
# ──────────────────────────────────────────────────────────────────
def collect_all_data(skip_models: set = None) -> tuple:
    """
    드롭다운 4단계를 순회하며 모든 모델의 BIOS 테이블을 즉시 수집.
    반환: (all_data, failed_entries)
    """
    if skip_models is None:
        skip_models = set()

    logger.info("\n🚀 [1단계] 상세 수집 시작")
    all_data = []
    failed   = []
    n        = 0

    with sync_playwright() as pw:
        browser, ctx, page = _make_browser(pw)
        try:
            page.goto(SUPPORT_URL, timeout=CONFIG["page_timeout"],
                      wait_until="load")

            # JS로 렌더링되는 <select> 요소가 나타날 때까지 대기
            try:
                page.wait_for_selector("select", timeout=15000)
            except PlaywrightTimeout:
                logger.warning("⚠️ <select> 요소 미감지 — 페이지 구조 확인 필요")

            page.wait_for_timeout(2000)

            if CONFIG["debug"]:
                _debug_save(page.content(), "support_initial")

            # 고정 셀렉터 사용 (driversearch.html 전용)
            SEL_PRODUCT = CONFIG["sel_product"]
            SEL_BRAND   = CONFIG["sel_brand"]
            SEL_CHIPSET = CONFIG["sel_chipset"]
            SEL_MODEL   = CONFIG["sel_model"]

            # Product → Motherboard 선택
            mb_opts = _get_select_options(page, SEL_PRODUCT)
            mb_val  = next((v for v, t in mb_opts if "motherboard" in t.lower()), None)
            if mb_val:
                _select_option(page, SEL_PRODUCT, mb_val)
                page.wait_for_timeout(CONFIG["dropdown_wait"])
                logger.info("✅ Product = Motherboard 선택")
            else:
                logger.warning(f"⚠️ 'Motherboard' 옵션 없음. 옵션 목록: {mb_opts}")

            # 메인보드 브랜드 목록 (INTEL / AMD)
            brand_opts = _get_select_options(page, SEL_BRAND)
            logger.info(f"🔌 브랜드 {len(brand_opts)}개: {[t for _,t in brand_opts]}")

            for brand_val, brand_name in brand_opts:
                logger.info(f"\n{'='*55}")
                logger.info(f"🔌 브랜드: {brand_name}")
                _select_option(page, SEL_BRAND, brand_val)
                page.wait_for_timeout(CONFIG["dropdown_wait"])

                chipset_opts = _get_select_options(page, SEL_CHIPSET)
                logger.info(f"   칩셋 {len(chipset_opts)}개: {[t for _,t in chipset_opts]}")

                for chip_val, chip_name in chipset_opts:
                    _select_option(page, SEL_CHIPSET, chip_val)
                    page.wait_for_timeout(CONFIG["dropdown_wait"])

                    model_opts = _get_select_options(page, SEL_MODEL)
                    logger.info(f"   [{chip_name}] 모델 {len(model_opts)}개")

                    for model_val, model_name in model_opts:
                        n += 1

                        # 체크포인트 스킵
                        ck_key = f"{brand_name}|{chip_name}|{model_val}"
                        if ck_key in skip_models:
                            logger.info(f"      ⏩ [{n}] {model_name} (스킵)")
                            continue

                        _select_option(page, SEL_MODEL, model_val)

                        # BIOS 테이블 로드 대기
                        try:
                            page.wait_for_selector("table", timeout=CONFIG["table_wait"])
                        except PlaywrightTimeout:
                            pass
                        page.wait_for_timeout(1200)

                        html = page.content()
                        if CONFIG["debug"]:
                            tag = re.sub(r"[^a-zA-Z0-9]", "_", model_name)[:40]
                            _debug_save(html, f"bios_{tag}")

                        bios_list  = _parse_bios_table(html)
                        bios_count = len(bios_list)
                        logger.info(f"      [{n}] {model_name} → BIOS {bios_count}개")

                        image_url = fetch_image_url(model_name)
                        logger.info(f"         이미지: {image_url or '없음'}")

                        entry = {
                            "model_id":   model_val,
                            "model_name": model_name,
                            "brand":      brand_name,
                            "chipset":    chip_name,
                            "image_url":  image_url,
                            "bios_list":  bios_list,
                        }
                        all_data.append(entry)

                        if bios_count > 0:
                            skip_models.add(ck_key)
                        else:
                            failed.append(entry)

                        # 주기적 중간 저장 (20개마다)
                        if len(all_data) % 20 == 0:
                            _save_results(all_data, skip_models)

                        time.sleep(random.uniform(CONFIG["delay_min"], CONFIG["delay_max"]))

        finally:
            ctx.close()
            browser.close()

    _save_results(all_data, skip_models)
    logger.info(
        f"\n📊 1차 수집 완료 | "
        f"성공: {len(all_data) - len(failed)}개 | 실패: {len(failed)}개"
    )
    return all_data, failed


# ──────────────────────────────────────────────────────────────────
#  2단계: BIOS 없는 모델 재시도
# ──────────────────────────────────────────────────────────────────
def retry_failed(failed: list, existing_data: list, completed: set) -> list:
    """BIOS 테이블이 비어있던 모델을 재시도."""
    if not failed:
        return existing_data

    logger.info(
        f"\n⏳ [3단계] 실패 모델 {len(failed)}개 → "
        f"{CONFIG['retry_wait'] // 60}분 후 재시도..."
    )
    for rem in range(CONFIG["retry_wait"], 0, -30):
        logger.info(f"   재시도까지 {rem}초 남음...")
        time.sleep(30)

    logger.info(f"\n🔄 재시도 시작 ({len(failed)}개)")
    still_failed  = []
    id_to_entry   = {item["model_id"]: item for item in existing_data}

    with sync_playwright() as pw:
        browser, ctx, page = _make_browser(pw)
        try:
            page.goto(SUPPORT_URL, timeout=CONFIG["page_timeout"],
                      wait_until="load")
            try:
                page.wait_for_selector("select", timeout=15000)
            except PlaywrightTimeout:
                logger.warning("⚠️ <select> 요소 미감지")
            page.wait_for_timeout(2000)

            SEL_PRODUCT = CONFIG["sel_product"]
            SEL_BRAND   = CONFIG["sel_brand"]
            SEL_CHIPSET = CONFIG["sel_chipset"]
            SEL_MODEL   = CONFIG["sel_model"]

            # Motherboard 재선택
            mb_opts = _get_select_options(page, SEL_PRODUCT)
            mb_val  = next((v for v, t in mb_opts if "motherboard" in t.lower()), None)
            if mb_val:
                _select_option(page, SEL_PRODUCT, mb_val)
                page.wait_for_timeout(CONFIG["dropdown_wait"])

            for i, entry in enumerate(failed, 1):
                model_id   = entry["model_id"]
                model_name = entry["model_name"]
                brand      = entry["brand"]
                chipset    = entry["chipset"]
                logger.info(f"   [{i}/{len(failed)}] 재시도: {model_name}")

                try:
                    # 브랜드 재선택
                    brand_opts = _get_select_options(page, SEL_BRAND)
                    brand_val  = next((v for v, t in brand_opts if t == brand), None)
                    if brand_val:
                        _select_option(page, SEL_BRAND, brand_val)
                        page.wait_for_timeout(CONFIG["dropdown_wait"])

                    # 칩셋 재선택
                    chip_opts = _get_select_options(page, SEL_CHIPSET)
                    chip_val  = next((v for v, t in chip_opts if t == chipset), None)
                    if chip_val:
                        _select_option(page, SEL_CHIPSET, chip_val)
                        page.wait_for_timeout(CONFIG["dropdown_wait"])

                    # 모델 재선택
                    _select_option(page, SEL_MODEL, model_id)
                    try:
                        page.wait_for_selector("table", timeout=CONFIG["table_wait"])
                    except PlaywrightTimeout:
                        pass
                    page.wait_for_timeout(1500)

                    bios_list = _parse_bios_table(page.content())

                    if bios_list:
                        entry["bios_list"] = bios_list
                        id_to_entry[model_id] = entry
                        ck_key = f"{brand}|{chipset}|{model_id}"
                        completed.add(ck_key)
                        logger.info(f"      ✅ BIOS {len(bios_list)}개 성공")
                    else:
                        still_failed.append(entry)
                        logger.info("      ❌ 여전히 BIOS 없음")

                except Exception as e:
                    logger.error(f"      🔥 재시도 예외: {e}")
                    still_failed.append(entry)

                time.sleep(random.uniform(CONFIG["delay_min"], CONFIG["delay_max"]))

        finally:
            ctx.close()
            browser.close()

    result = list(id_to_entry.values())
    _save_results(result, completed)

    logger.info(
        f"\n📊 재시도 완료 | "
        f"성공: {len(failed)-len(still_failed)}개 | "
        f"최종 실패: {len(still_failed)}개"
    )
    if still_failed:
        no_bios_names = [e["model_name"] for e in still_failed]
        append_no_bios_log(no_bios_names)
        logger.warning(
            f"🚫 BIOS 없는 모델 {len(no_bios_names)}개 → "
            f"{os.path.basename(NO_BIOS_LOG)} 에 영구 기록"
        )

    return result


# ──────────────────────────────────────────────────────────────────
#  메인
# ──────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Maxsun BIOS 스크래퍼")
    parser.add_argument("--no-headless", action="store_true",
                        help="브라우저 창 표시 (디버깅)")
    parser.add_argument("--debug",       action="store_true",
                        help="HTML을 debug_*.html 로 저장")
    parser.add_argument("--recollect",   action="store_true",
                        help="체크포인트 무시, 전체 재수집")
    parser.add_argument("--retry-db",    action="store_true",
                        help="DB에서 BIOS 없는 모델만 재시도")
    parser.add_argument("--data-dir",    default=None,
                        help="DB 저장 경로 (기본: 스크래퍼 폴더)")
    args = parser.parse_args()

    global DB_FILE
    if args.data_dir:
        DB_FILE = os.path.join(args.data_dir, os.path.basename(DB_FILE))

    if args.no_headless:
        CONFIG["headless"] = False
    if args.debug:
        CONFIG["debug"] = True

    # --retry-db 모드
    if args.retry_db:
        if not os.path.exists(DB_FILE):
            logger.error("❌ DB 파일 없음. 먼저 전체 수집을 실행하세요.")
            return
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute("""
            SELECT model_id, model_name, brand, chipset
            FROM motherboards
            WHERE model_id NOT IN (SELECT DISTINCT model_id FROM bios_versions)
        """).fetchall()
        conn.close()
        retry_entries = [
            {"model_id": r[0], "model_name": r[1],
             "brand": r[2], "chipset": r[3], "image_url": "", "bios_list": []}
            for r in rows
        ]
        logger.info(f"🔄 --retry-db: DB에서 BIOS 없는 모델 {len(retry_entries)}개 재시도")
        existing = []
        if os.path.exists(FINAL_JSON):
            with open(FINAL_JSON, encoding="utf-8") as f:
                existing = json.load(f)
        completed = load_checkpoint()
        result = retry_failed(retry_entries, existing, completed)
        save_to_sqlite(result)
        return

    # 체크포인트 초기화 또는 로드
    if args.recollect:
        completed = set()
        for path in [CHECKPOINT_FILE, FINAL_JSON, NO_BIOS_LOG]:
            if os.path.exists(path):
                os.remove(path)
        logger.info("🔄 전체 재수집 모드")
    else:
        # 체크포인트는 매 실행 시 삭제 (크래시 복구 전용 — 전 모델 재방문으로 신규 BIOS 감지)
        if os.path.exists(CHECKPOINT_FILE):
            os.remove(CHECKPOINT_FILE)
            logger.info("🗑️ 체크포인트 초기화 → 전 모델 재방문")
        completed = set()

    # 기존 데이터 로드 (resume 시)
    existing_data = []
    if os.path.exists(FINAL_JSON) and not args.recollect:
        try:
            with open(FINAL_JSON, encoding="utf-8") as f:
                existing_data = json.load(f)
            logger.info(f"📂 기존 데이터 로드: {len(existing_data)}개")
        except Exception:
            pass

    # 1단계: 수집
    new_data, failed = collect_all_data(skip_models=completed)

    # 기존 + 신규 병합 (중복 제거)
    existing_ids = {x["model_id"] for x in existing_data}
    all_data = existing_data + [d for d in new_data if d["model_id"] not in existing_ids]

    # 2단계: 재시도
    if failed:
        all_data = retry_failed(failed, all_data, completed)
    else:
        logger.info("✅ 실패 모델 없음, 재시도 생략")

    # DB 저장
    save_to_sqlite(all_data)
    bios_total = sum(len(d.get("bios_list", [])) for d in all_data)
    logger.info(
        f"\n✨ 완료! 총 {len(all_data)}개 모델 | "
        f"BIOS 버전 합계: {bios_total}개"
    )


if __name__ == "__main__":
    main()

"""
Biostar BIOS Data Collector
crontab: 0 7 * * * python3 /path/to/biostar_bios_scraper.py

사이트 구조:
  다운로드 센터: https://www.biostar.com.tw/app/kr/support/download.php
  3단계 드롭다운 (Playwright 로 소켓/칩셋 조합 열거):
    1. 제품 유형 → 마더 보드 / IPC
    2. 소켓     → INTEL 1851, INTEL 1700, AM5, AM4 ...
    3. 칩셋     → Intel H810, Intel Z890 ...
  모델 목록 URL (GET 파라미터로 서버사이드 렌더링):
    ?Ptype=mb&Psocket=<socket_id>&Pchip=<chipset_name>#down_id
  BIOS 뷰 URL (data-type=DOWNLOAD 으로 탭 자동 활성화):
    introduction.php?S_ID=<s_id>&data-type=DOWNLOAD
  BIOS 카드 구조 (AJAX 로딩):
    섹션 헤딩 "BIOS" > 파일 카드:
      버전 / Description / 파일 크기 / 날짜 / 다운로드

실행 흐름:
  1단계 — 다운로드 센터 드롭다운으로 소켓/칩셋 열거 → 모델 목록 수집
  2단계 — ?data-type=DOWNLOAD 페이지에서 BIOS 카드 파싱
  3단계 — 실패 모델 5분 대기 후 1회 재시도
  4단계 — 재시도 후에도 실패 = BIOS 없는 단종/특수 모델
           → biostar_no_bios_models.log 에 영구 기록
"""

import argparse
import json
import os
import re
import time
import random
import logging
import sqlite3
from threading import Lock

from urllib.parse import quote as urlquote

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
    "retry_wait":    180,     # 재시도 대기 (초)
    "page_timeout":  30000,
    "bios_wait":     8000,    # BIOS 카드 AJAX 로딩 대기 (ms)
    "dropdown_wait": 2500,    # 드롭다운 변경 후 갱신 대기 (ms)
    "headless":      True,
    "debug":         False,
}

# ══════════════════════════════════════════════════════════════════
#  URL 상수
# ══════════════════════════════════════════════════════════════════
BASE_URL     = "https://www.biostar.com.tw"
DOWNLOAD_URL = "https://www.biostar.com.tw/app/kr/support/download.php"
MB_VIEW_TPL  = "https://www.biostar.com.tw/app/kr/mb/introduction.php?S_ID={}&data-type=DOWNLOAD"
IPC_VIEW_TPL = "https://www.biostar.com.tw/app/kr/ipc/introduction.php?S_ID={}&data-type=DOWNLOAD"

# ══════════════════════════════════════════════════════════════════
#  경로
# ══════════════════════════════════════════════════════════════════
BASE_PATH       = os.path.dirname(os.path.abspath(__file__))
FINAL_JSON      = os.path.join(BASE_PATH, "biostar_bios_data_final.json")
CHECKPOINT_FILE = os.path.join(BASE_PATH, "biostar_checkpoint.json")
NO_BIOS_LOG     = os.path.join(BASE_PATH, "biostar_no_bios_models.log")
DB_FILE         = os.path.join(BASE_PATH, "biostar_bios.db")

# ══════════════════════════════════════════════════════════════════
#  로거
# ══════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_PATH, "biostar_scraper.log"), encoding="utf-8"),
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
    path = os.path.join(BASE_PATH, f"debug_biostar_{tag}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"🐛 HTML 저장: {path}")


# ──────────────────────────────────────────────────────────────────
#  1단계 헬퍼: 다운로드 센터 드롭다운 탐색 + 모델 목록 수집
# ──────────────────────────────────────────────────────────────────
def _discover_socket_chipset_combos(page, ptype: str) -> list:
    """
    다운로드 센터 3단계 드롭다운에서 소켓/칩셋 조합 목록 수집.
    반환: [{"socket_id": str, "socket_name": str, "chipset": str}, ...]
    """
    logger.info(f"🔍 [{ptype.upper()}] 소켓/칩셋 조합 탐색...")
    try:
        page.goto(DOWNLOAD_URL, timeout=CONFIG["page_timeout"], wait_until="load")
        page.wait_for_timeout(2000)
    except PlaywrightTimeout:
        logger.warning("⚠️ 다운로드 센터 타임아웃")
        return []

    if CONFIG["debug"]:
        _debug_save(page.content(), f"download_center_{ptype}")

    # 드롭다운 1: 제품 유형 선택
    page.evaluate("""(ptype) => {
        const sels = Array.from(document.querySelectorAll('select'));
        for (const sel of sels) {
            const opt = Array.from(sel.options).find(o =>
                o.value.toLowerCase() === ptype ||
                /\ub9c8\ub354.*\ubcf4\ub4dc|motherboard/i.test(o.text)
            );
            if (opt) {
                sel.value = opt.value;
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                break;
            }
        }
    }""", ptype)
    page.wait_for_timeout(CONFIG["dropdown_wait"])

    # 소켓 옵션 수집
    sockets = page.evaluate("""() => {
        const sels = Array.from(document.querySelectorAll('select'));
        if (sels.length < 2) return [];
        return Array.from(sels[1].options)
            .filter(o => o.value && o.value !== '0' && o.value.trim() !== '')
            .map(o => ({ id: o.value, name: o.text.trim() }));
    }""")

    if not sockets:
        logger.warning("  ⚠️ 소켓 옵션 없음 — 드롭다운 구조 확인 필요")
        return []

    combos = []
    for socket in sockets:
        # 소켓 선택
        page.evaluate("""(sid) => {
            const sels = Array.from(document.querySelectorAll('select'));
            if (sels.length < 2) return;
            sels[1].value = sid;
            sels[1].dispatchEvent(new Event('change', { bubbles: true }));
        }""", socket["id"])
        page.wait_for_timeout(CONFIG["dropdown_wait"])

        # 칩셋 옵션 수집
        chipsets = page.evaluate("""() => {
            const sels = Array.from(document.querySelectorAll('select'));
            if (sels.length < 3) return [];
            return Array.from(sels[2].options)
                .filter(o => o.value && o.value !== '0' && o.value.trim() !== '')
                .map(o => o.text.trim());
        }""")

        for chip in chipsets:
            combos.append({
                "socket_id":   socket["id"],
                "socket_name": socket["name"],
                "chipset":     chip,
            })
        logger.debug(f"  소켓 [{socket['name']}]: {len(chipsets)}개 칩셋")

    logger.info(f"  → {len(combos)}개 소켓/칩셋 조합")
    return combos


def _collect_models_from_combo(page, ptype: str, combo: dict) -> list:
    """
    download.php?Ptype=&Psocket=&Pchip= URL에서 모델 목록 수집.
    View 링크(introduction.php?S_ID=XX&data-type=DOWNLOAD) 파싱.
    반환: [{"s_id", "model_name", "product_type", "socket", "chipset"}, ...]
    """
    url = (f"{DOWNLOAD_URL}?Ptype={ptype}"
           f"&Psocket={combo['socket_id']}"
           f"&Pchip={urlquote(combo['chipset'])}"
           f"#down_id")
    try:
        page.goto(url, timeout=CONFIG["page_timeout"], wait_until="load")
        page.wait_for_timeout(1000)
    except PlaywrightTimeout:
        logger.warning(f"  ⚠️ 타임아웃: {combo['chipset']}")
        return []

    soup   = BeautifulSoup(page.content(), "html.parser")
    models = []
    seen   = set()

    for a in soup.find_all("a", href=re.compile(r"introduction\.php.*S_ID=\d+.*data-type=DOWNLOAD", re.I)):
        m = re.search(r"S_ID=(\d+)", a.get("href", ""))
        if not m:
            continue
        s_id = m.group(1)
        if s_id in seen:
            continue
        seen.add(s_id)

        # 모델명: <div class="row"> > <p> 텍스트
        model_name = ""
        row = a.find_parent("div", class_="row")
        if row:
            p = row.find("p")
            if p:
                model_name = p.get_text(strip=True)

        models.append({
            "s_id":         s_id,
            "model_name":   model_name or f"Model-{s_id}",
            "product_type": ptype,
            "socket":       combo["socket_name"],
            "chipset":      combo["chipset"],
        })

    return models


# ──────────────────────────────────────────────────────────────────
#  BIOS 카드 파싱 (data-type=DOWNLOAD 페이지)
# ──────────────────────────────────────────────────────────────────
def _parse_bios_card(html: str) -> list:
    """
    introduction.php?S_ID=XX&data-type=DOWNLOAD 페이지의 BIOS 섹션 파싱.

    실제 HTML 구조 (DevTools 확인):
      <!-- BIOS-->
      <div class="tab-box">
        <div class="tab-title">BIOS</div>
        <div class="table">
          <div class="tbody">
            <div class="tr">  ← BIOS 버전 1개
              <div class="td" rwd-title="버전"><p>H81EO317.BSS</p></div>
              <div class="td" rwd-title="Description"><p>Initial BIOS</p></div>
              <div class="td" rwd-title="파일 크기"><p>32768 KB</p></div>
              <div class="td" rwd-title="날짜"><p>2026-03-17</p></div>
              <div class="td tb-file" rwd-title="다운로드">
                <a href="javascript:;"
                   onclick="openLightboxWithParameters(9151,'H81EO317.BSS','H81EO317BSS.zip','Y');count(9151);">
                  <div class="icon-download"></div>
                </a>
              </div>
            </div>
          </div>
        </div>
      </div>
      <!-- BIOS SOP 後台BIOS設定 -->

    다운로드 URL 조합:
      openLightboxWithParameters(id, bssName, zipName, flag)
      → https://www.biostar.com.tw/upload/Bios/{zipName}
    """
    soup = BeautifulSoup(html, "html.parser")

    BIOS_DL_BASE = "https://www.biostar.com.tw/upload/Bios/"

    # rwd-title 속성값 → 필드명
    RWD_MAP = {
        "버전":    "version",
        "description": "info",
        "파일 크기": "size",
        "날짜":    "date",
        "다운로드": "download_url",
    }

    def _download_url(td_el) -> str:
        """다운로드 .td 에서 실제 URL 추출."""
        a = td_el.find("a")
        if not a:
            return ""

        # 1순위: openLightboxWithParameters(id, bssName, zipName, flag)
        onclick = (a.get("onclick") or "")
        m = re.search(
            r"openLightboxWithParameters\s*\(\s*\d+\s*,\s*'[^']*'\s*,\s*'([^']+)'",
            onclick,
        )
        if m:
            return BIOS_DL_BASE + m.group(1)

        # 2순위: 표준 href
        href = (a.get("href") or "").strip()
        if href and href not in ("#", "javascript:;", "javascript:void(0)"):
            if href.startswith("http"):
                return href
            # 상대 경로 → 절대 경로
            return BASE_URL + "/" + href.lstrip("./")

        # 3순위: data-* 속성
        for attr in ("data-href", "data-url", "data-file", "data-link", "data-download"):
            val = (a.get(attr) or "").strip()
            if val and val not in ("#", ""):
                return val if val.startswith("http") else BASE_URL + "/" + val.lstrip("./")

        return ""

    # ── .tab-box[BIOS] → .tbody → .tr 탐색 ──────────────────────────
    for tab_box in soup.find_all("div", class_=re.compile(r"\btab-box\b", re.I)):
        title_el = tab_box.find("div", class_=re.compile(r"\btab-title\b", re.I))
        if not title_el or not re.search(r"\bbios\b", title_el.get_text(strip=True), re.I):
            continue

        tbody = tab_box.find("div", class_=re.compile(r"\btbody\b", re.I))
        if not tbody:
            continue

        bios_list = []
        for tr in tbody.find_all("div", class_=re.compile(r"\btr\b", re.I), recursive=False):
            entry: dict = {}

            for td in tr.find_all("div", class_=re.compile(r"\btd\b", re.I)):
                rwd   = (td.get("rwd-title") or "").strip()
                field = RWD_MAP.get(rwd)
                if not field:
                    continue

                if field == "download_url":
                    entry[field] = _download_url(td)
                else:
                    p = td.find("p")
                    entry[field] = (p.get_text(strip=True) if p
                                    else td.get_text(strip=True))

            if entry.get("version"):
                bios_list.append(entry)

        if bios_list:
            return bios_list

    # ── 폴백: 파일 확장자 링크 직접 탐색 ─────────────────────────────
    return _parse_direct_links(soup)


def _parse_direct_links(soup) -> list:
    """BIOS 파일 확장자 링크 직접 탐색 (최후 폴백)."""
    result   = []
    seen_urls: set = set()

    containers = [
        tag for tag in soup.find_all(["div", "section", "li", "ul", "tr"])
        if re.search(r"\bbios\b", tag.get_text(strip=True), re.I)
    ] or [soup]

    for container in containers:
        for a in container.find_all("a", href=True):
            href = a["href"].strip()
            if not re.search(r"\.(zip|rar|rom|bin|cap|fd)$", href, re.I):
                continue
            url = href if href.startswith("http") else BASE_URL + href
            if url in seen_urls:
                continue
            seen_urls.add(url)
            name = a.get_text(strip=True) or os.path.basename(href)
            result.append({
                "version": name, "date": "", "info": "",
                "size": "", "name": name, "download_url": url,
            })
    return result


# ──────────────────────────────────────────────────────────────────
#  개별 제품 BIOS 수집
# ──────────────────────────────────────────────────────────────────
def collect_bios_for_product(page, s_id: str, product_type: str = "mb") -> dict:
    """
    ?data-type=DOWNLOAD URL로 이동 → BIOS 카드 AJAX 로딩 대기 → 파싱.
    탭 클릭 불필요 (data-type=DOWNLOAD 파라미터가 자동 활성화).
    반환: {"image_url": str, "bios_list": list}
    """
    url = (IPC_VIEW_TPL if product_type == "ipc" else MB_VIEW_TPL).format(s_id)

    try:
        page.goto(url, timeout=CONFIG["page_timeout"], wait_until="load")
        page.wait_for_timeout(1500)
    except PlaywrightTimeout:
        logger.warning(f"  ⚠️ [{s_id}] 페이지 타임아웃")
        return {"image_url": "", "bios_list": []}

    # 이미지 URL 추출 — lazy entered loaded 클래스 이미지 우선
    image_url = page.evaluate("""() => {
        const img = document.querySelector(
            'img.lazy.entered.loaded, ' +
            '.product-img img, .main-img img, #main-img, .intro-img img, .thumb img'
        );
        return img ? (img.src || img.getAttribute('data-src') || '') : '';
    }""")

    # BIOS 카드 AJAX 로딩 대기: "파일 크기" 또는 "버전" 텍스트 등장 확인
    try:
        page.wait_for_function(
            """() => {
                const t = document.body.innerText || '';
                return /파일\s*크기|file\s*size/i.test(t) || /버전/i.test(t);
            }""",
            timeout=CONFIG["bios_wait"],
        )
    except PlaywrightTimeout:
        pass  # 타임아웃 시 현재 상태로 파싱 진행

    if CONFIG["debug"]:
        _debug_save(page.content(), f"product_{re.sub(r'[^a-zA-Z0-9]', '_', s_id)}")

    html      = page.content()
    bios_list = _parse_bios_card(html)

    # 파싱 실패 시 파일 링크 폴백
    if not bios_list:
        bios_list = _parse_direct_links(BeautifulSoup(html, "html.parser"))

    return {
        "image_url": image_url or "",
        "bios_list": bios_list,
    }


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
            chipset         TEXT DEFAULT '',
            socket          TEXT DEFAULT '',
            product_type    TEXT DEFAULT 'mb',
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
            size         TEXT DEFAULT '',
            download_url TEXT DEFAULT '',
            UNIQUE(model_id, version)
        );
    """)
    # 스키마 마이그레이션 (기존 DB 호환)
    for col, default in [("last_valid_date", "NULL"), ("socket", "''"), ("size", "''"), ("bios_page_url", "''")]:
        try:
            cur.execute(f"ALTER TABLE motherboards ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    try:
        cur.execute("ALTER TABLE bios_versions ADD COLUMN size TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    for item in all_data:
        mid      = item.get("model_id") or ""
        has_bios = bool(item.get("bios_list"))
        cur.execute("""
            INSERT INTO motherboards
                (model_id, model_name, chipset, socket, product_type, image_url, bios_page_url,
                 updated_at, last_valid_date)
            VALUES (?,?,?,?,?,?,?,datetime('now','localtime'),
                    CASE WHEN ? THEN datetime('now','localtime') ELSE NULL END)
            ON CONFLICT(model_id) DO UPDATE SET
                model_name      = excluded.model_name,
                chipset         = excluded.chipset,
                socket          = excluded.socket,
                product_type    = excluded.product_type,
                image_url       = CASE WHEN motherboards.image_url != '' THEN motherboards.image_url
                                       ELSE excluded.image_url END,
                bios_page_url   = excluded.bios_page_url,
                updated_at      = excluded.updated_at,
                last_valid_date = CASE WHEN excluded.last_valid_date IS NOT NULL
                                       THEN excluded.last_valid_date
                                       ELSE motherboards.last_valid_date
                                  END
        """, (mid, item.get("model_name", ""), item.get("chipset", ""),
              item.get("socket", ""), item.get("product_type", "mb"),
              item.get("image_url", ""), item.get("bios_page_url", ""), 1 if has_bios else 0))

        for b in item.get("bios_list", []):
            cur.execute("""
                INSERT INTO bios_versions
                    (model_id, model_name, version, date, info, size, download_url)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(model_id, version) DO UPDATE SET
                    date         = excluded.date,
                    info         = excluded.info,
                    size         = excluded.size,
                    download_url = excluded.download_url
            """, (mid, item.get("model_name", ""), b.get("version", ""),
                  b.get("date", ""), b.get("info", ""),
                  b.get("size", ""), b.get("download_url", "")))

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
#  1단계: 제품 목록 수집
# ──────────────────────────────────────────────────────────────────
def gather_product_list(include_ipc: bool = False) -> list:
    """
    다운로드 센터 드롭다운으로 소켓/칩셋 조합 열거 후 모델 목록 수집.
    반환: [{"s_id", "model_name", "product_type", "socket", "chipset"}, ...]
    """
    logger.info("📡 [1단계] Biostar 메인보드 모델 리스트 수집 중...")
    all_products = []

    with sync_playwright() as pw:
        browser, ctx, page = _make_browser(pw)
        try:
            for ptype in (["mb", "ipc"] if include_ipc else ["mb"]):
                combos = _discover_socket_chipset_combos(page, ptype)
                logger.info(f"📋 [{ptype.upper()}] {len(combos)}개 조합 → 모델 목록 수집")

                for i, combo in enumerate(combos, 1):
                    models = _collect_models_from_combo(page, ptype, combo)
                    all_products.extend(models)
                    logger.debug(
                        f"  [{i}/{len(combos)}] {combo['chipset']}: {len(models)}개"
                    )

                    time.sleep(random.uniform(0.3, 0.8))
        finally:
            ctx.close()
            browser.close()

    # 중복 제거 (s_id 기준)
    seen: set = set()
    unique = []
    for p in all_products:
        if p["s_id"] not in seen:
            seen.add(p["s_id"])
            unique.append(p)

    logger.info(f"✅ 모델 리스트 수집 완료: {len(unique)}개")
    return unique


# ──────────────────────────────────────────────────────────────────
#  2단계: 각 제품 BIOS 수집
# ──────────────────────────────────────────────────────────────────
def collect_all_data(product_list: list, skip_models: set = None) -> tuple:
    """
    모든 제품의 BIOS 데이터를 수집.
    반환: (all_data, failed_entries)
    """
    if skip_models is None:
        skip_models = set()

    all_data = []
    failed   = []
    n        = 0
    total    = len(product_list)

    logger.info(f"\n🚀 [2단계] 상세 수집 시작 ({total}개 제품)")

    with sync_playwright() as pw:
        browser, ctx, page = _make_browser(pw)
        try:
            for prod in product_list:
                n += 1
                s_id        = prod["s_id"]
                model_name  = prod["model_name"]
                ptype       = prod.get("product_type", "mb")

                ck_key = f"{ptype}|{s_id}"
                if ck_key in skip_models:
                    logger.info(f"   ⏩ [{n}/{total}] {model_name} (s_id={s_id}) — 스킵")
                    continue

                logger.info(f"   [{n}/{total}] {model_name} (s_id={s_id})")

                chipset = prod.get("chipset", "")
                socket  = prod.get("socket", "")

                try:
                    result    = collect_bios_for_product(page, s_id, ptype)
                    bios_list = result["bios_list"]
                    image_url = result["image_url"]

                    logger.info(f"      → BIOS {len(bios_list)}개 | {chipset}")
                except Exception as e:
                    logger.error(f"      🔥 예외 발생 [{model_name}]: {e}")
                    bios_list = []
                    image_url = ""

                entry = {
                    "model_id":     s_id,
                    "model_name":   model_name,
                    "chipset":      chipset,
                    "socket":       socket,
                    "product_type": ptype,
                    "image_url":    image_url,
                    "bios_page_url": (IPC_VIEW_TPL if ptype == "ipc" else MB_VIEW_TPL).format(s_id),
                    "bios_list":    bios_list,
                }
                all_data.append(entry)

                if bios_list:
                    skip_models.add(ck_key)
                else:
                    failed.append(entry)

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
#  3단계: BIOS 없는 모델 재시도
# ──────────────────────────────────────────────────────────────────
def retry_failed(failed: list, existing_data: list, completed: set) -> list:
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
    still_failed = []
    id_to_entry  = {item["model_id"]: item for item in existing_data}

    with sync_playwright() as pw:
        browser, ctx, page = _make_browser(pw)
        try:
            for i, entry in enumerate(failed, 1):
                s_id  = entry["model_id"]
                ptype = entry.get("product_type", "mb")
                logger.info(f"   [{i}/{len(failed)}] 재시도: {entry['model_name']}")

                try:
                    result    = collect_bios_for_product(page, s_id, ptype)
                    bios_list = result["bios_list"]

                    if bios_list:
                        entry["bios_list"] = bios_list
                        if result["image_url"]:
                            entry["image_url"] = result["image_url"]
                        id_to_entry[s_id] = entry
                        ck_key = f"{ptype}|{s_id}"
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
        f"성공: {len(failed) - len(still_failed)}개 | "
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
    parser = argparse.ArgumentParser(description="Biostar BIOS 스크래퍼")
    parser.add_argument("--no-headless", action="store_true",
                        help="브라우저 창 표시 (디버깅)")
    parser.add_argument("--debug",       action="store_true",
                        help="HTML을 debug_biostar_*.html 로 저장")
    parser.add_argument("--recollect",   action="store_true",
                        help="체크포인트 무시, 전체 재수집")
    parser.add_argument("--ipc",         action="store_true",
                        help="산업용 메인보드(IPC)도 수집")
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
            SELECT model_id, model_name, chipset, socket, product_type
            FROM motherboards
            WHERE model_id NOT IN (SELECT DISTINCT model_id FROM bios_versions)
        """).fetchall()
        conn.close()
        retry_entries = [
            {"model_id": r[0], "model_name": r[1],
             "chipset": r[2], "socket": r[3], "product_type": r[4],
             "image_url": "", "bios_list": []}
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
        completed = load_checkpoint()
        if completed:
            logger.info(f"⏩ Resume 모드: {len(completed)}개 이미 완료, 나머지만 수집")

    # 기존 데이터 로드 (resume 시)
    existing_data = []
    if os.path.exists(FINAL_JSON) and not args.recollect:
        try:
            with open(FINAL_JSON, encoding="utf-8") as f:
                existing_data = json.load(f)
            logger.info(f"📂 기존 데이터 로드: {len(existing_data)}개")
        except Exception:
            pass

    # 1단계: 제품 목록 수집
    product_list = gather_product_list(include_ipc=args.ipc)
    if not product_list:
        logger.error("❌ 제품 목록 수집 실패")
        return

    # resume: 이미 수집된 s_id 제외
    existing_ids = {x["model_id"] for x in existing_data}
    pending = [p for p in product_list if p["s_id"] not in existing_ids or
               f"{p.get('product_type','mb')}|{p['s_id']}" not in completed]

    logger.info(f"📋 수집 대상: {len(pending)}개 (전체 {len(product_list)}개)")

    # 2단계: BIOS 수집
    new_data, failed = collect_all_data(pending, skip_models=completed)

    # 기존 + 신규 병합 (중복 제거)
    all_data = existing_data + [d for d in new_data if d["model_id"] not in existing_ids]

    # 3단계: 재시도
    if failed:
        all_data = retry_failed(failed, all_data, completed)
    else:
        logger.info("✅ 실패 모델 없음, 재시도 생략")

    # DB 저장
    save_to_sqlite(all_data)
    logger.info(
        f"\n✨ 전체 완료!\n"
        f"   ✅ 수집 성공: {sum(1 for d in all_data if d.get('bios_list'))}개\n"
        f"   🚫 BIOS 없음: {sum(1 for d in all_data if not d.get('bios_list'))}개"
    )


if __name__ == "__main__":
    main()

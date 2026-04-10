# Biostar BIOS Scraper

Biostar 공식 다운로드 센터에서 전체 메인보드 모델의 BIOS 정보를 Playwright로 수집하고 SQLite DB에 저장합니다.

## 수집 흐름

1. **모델 리스트 수집** — 다운로드 센터 3단계 드롭다운 열거
   - 소켓 → 칩셋 → 모델 목록 (`?Ptype=mb&Psocket=...&Pchip=...`)
2. **BIOS 수집** — `introduction.php?S_ID=...&data-type=DOWNLOAD` 페이지 BIOS 카드 파싱
3. **재시도** — 실패 모델 대기 후 1회 재시도
4. **영구 기록** — 재시도 후에도 실패 → `biostar_no_bios_models.log`

## 의존성

```
playwright
playwright-stealth
beautifulsoup4
tqdm
```

```bash
pip install playwright playwright-stealth beautifulsoup4 tqdm
playwright install chromium
```

## 실행

```bash
# 전체 수집 (기본)
python biostar_bios_scraper.py

# DB에서 BIOS 없는 모델만 재시도
python biostar_bios_scraper.py --retry-db

# no_bios_log 포함 전체 재수집
python biostar_bios_scraper.py --recollect

# IPC 제품 포함 수집
python biostar_bios_scraper.py --ipc

# 브라우저 창 표시 (디버그)
python biostar_bios_scraper.py --no-headless

# HTML 디버그 저장 활성화
python biostar_bios_scraper.py --debug

# DB를 지정 경로에 저장 (Docker 공유 폴더 등)
python biostar_bios_scraper.py --data-dir /volume1/docker/bios-finder/server/data
```

## 주요 설정 (`CONFIG`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `delay_min/max` | 0.8 / 2.0 | 요청 간격 (초) |
| `retry_wait` | 180 | 재시도 전 대기 시간 (초) |
| `page_timeout` | 30000 | Playwright 페이지 로드 타임아웃 (ms) |
| `bios_wait` | 8000 | BIOS 카드 AJAX 로딩 대기 (ms) |
| `dropdown_wait` | 2500 | 드롭다운 변경 후 갱신 대기 (ms) |

## 출력 파일

| 파일 | 설명 |
|---|---|
| `biostar_bios.db` | SQLite DB (메인 결과) |
| `biostar_bios_data_final.json` | 수집 결과 JSON |
| `biostar_checkpoint.json` | 재개용 체크포인트 |
| `biostar_no_bios_models.log` | BIOS 없는 단종/특수 모델 목록 |
| `biostar_scraper.log` | 실행 로그 |

## DB 스키마

```sql
CREATE TABLE motherboards (
    model_id        TEXT PRIMARY KEY,
    model_name      TEXT,
    chipset         TEXT DEFAULT '',
    product_type    TEXT DEFAULT 'mb',
    image_url       TEXT DEFAULT '',
    updated_at      TEXT DEFAULT (datetime('now','localtime')),
    last_valid_date TEXT   -- 마지막으로 BIOS가 수집된 날짜
);

CREATE TABLE bios_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id     TEXT,
    model_name   TEXT,
    version      TEXT,
    date         TEXT DEFAULT '',  -- YYYY-MM-DD ISO 형식
    info         TEXT DEFAULT '',
    size         TEXT DEFAULT '',
    download_url TEXT DEFAULT '',
    UNIQUE(model_id, version)
);
```

## 자동 실행 (crontab)

```cron
0 7 * * * python3 /path/to/biostar_bios_scraper.py
```

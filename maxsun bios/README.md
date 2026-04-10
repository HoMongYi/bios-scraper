# Maxsun BIOS Scraper

Maxsun 드라이버 검색 페이지에서 전체 메인보드 모델의 BIOS 정보를 Playwright로 수집하고 SQLite DB에 저장합니다.

## 수집 흐름

1. **BIOS 수집** — 드라이버 검색 페이지 4단계 드롭다운 순회 + BIOS 테이블 파싱
   - Product → Chipset Brand → Chipset → Model
   - 대상: `myshopify.maxsun.com.cn/search/driversearch.html`
   - BIOS 버전 목록은 날짜 내림차순 정렬 후 저장
2. **이미지 수집** — `maxsun.com/ko/search?type=product&q={모델명}` 검색 결과 첫 이미지
   - DB에 기존 `image_url`이 있으면 요청 생략 (캐시)
3. **재시도** — 실패 모델 대기 후 1회 재시도
4. **영구 기록** — 재시도 후에도 실패 → `maxsun_no_bios_models.log`

## 의존성

```
playwright
playwright-stealth
requests
beautifulsoup4
tqdm
```

```bash
pip install playwright playwright-stealth requests beautifulsoup4 tqdm
playwright install chromium
```

## 실행

```bash
# 전체 수집 (기본)
python maxsun_bios_scraper.py

# DB에서 BIOS 없는 모델만 재시도
python maxsun_bios_scraper.py --retry-db

# no_bios_log 포함 전체 재수집
python maxsun_bios_scraper.py --recollect

# 브라우저 창 표시 (디버그)
python maxsun_bios_scraper.py --no-headless

# HTML 디버그 저장 활성화
python maxsun_bios_scraper.py --debug

# DB를 지정 경로에 저장 (Docker 공유 폴더 등)
python maxsun_bios_scraper.py --data-dir /volume1/docker/bios-finder/server/data
```

## 주요 설정 (`CONFIG`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `delay_min/max` | 0.8 / 2.0 | 요청 간격 (초) |
| `retry_wait` | 180 | 재시도 전 대기 시간 (초) |
| `page_timeout` | 30000 | Playwright 페이지 로드 타임아웃 (ms) |
| `dropdown_wait` | 2500 | 드롭다운 변경 후 JS 갱신 대기 (ms) |
| `table_wait` | 8000 | BIOS 테이블 로드 대기 (ms) |

## 출력 파일

| 파일 | 설명 |
|---|---|
| `maxsun_bios.db` | SQLite DB (메인 결과) |
| `maxsun_bios_data_final.json` | 수집 결과 JSON |
| `maxsun_checkpoint.json` | 재개용 체크포인트 |
| `maxsun_no_bios_models.log` | BIOS 없는 단종/특수 모델 목록 |
| `maxsun_scraper.log` | 실행 로그 |

## DB 스키마

```sql
CREATE TABLE motherboards (
    model_id        TEXT PRIMARY KEY,
    model_name      TEXT,
    brand           TEXT DEFAULT '',
    chipset         TEXT DEFAULT '',
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
    name         TEXT DEFAULT '',
    download_url TEXT DEFAULT '',
    UNIQUE(model_id, version)
);
```

## 자동 실행 (crontab)

```cron
0 7 * * * python3 /path/to/maxsun_bios_scraper.py
```

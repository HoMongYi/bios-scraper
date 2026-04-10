# ASRock BIOS Scraper

ASRock 공식 사이트에서 전체 메인보드 모델의 BIOS 정보를 Playwright로 수집하고 SQLite DB에 저장합니다.

## 수집 흐름

1. **모델 리스트 수집** — `asrock.com/mb/index.asp` 파싱 → JS `allmodels` 배열 추출
2. **BIOS 수집** — 각 제품 페이지 BIOS 탭 파싱 (병렬, Playwright)
3. **재시도** — 실패 모델 5분 대기 후 1회 재시도
4. **영구 기록** — 재시도 후에도 실패 → `asrock_no_bios_models.log`

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
python asrock_bios_scraper.py

# DB에서 BIOS 없는 모델만 재시도
python asrock_bios_scraper.py --retry-db

# no_bios_log 포함 전체 재수집
python asrock_bios_scraper.py --recollect

# 브라우저 창 표시 (디버그)
python asrock_bios_scraper.py --no-headless

# 병렬 스레드 수 지정
python asrock_bios_scraper.py --workers 2

# DB/체크포인트 초기화 후 재수집
python asrock_bios_scraper.py --reset

# HTML 디버그 저장 활성화
python asrock_bios_scraper.py --debug

# DB를 지정 경로에 저장 (Docker 공유 폴더 등)
python asrock_bios_scraper.py --data-dir /volume1/docker/bios-finder/server/data
```

## 주요 설정 (`CONFIG`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `workers` | 4 | 병렬 수집 스레드 수 |
| `delay_min/max` | 0.5 / 1.5 | 요청 간격 (초) |
| `retry_wait` | 300 | 재시도 전 대기 시간 (초) |
| `page_timeout` | 20000 | Playwright 페이지 로드 타임아웃 (ms) |
| `bios_wait` | 5000 | BIOS 콘텐츠 대기 (ms) |

## 출력 파일

| 파일 | 설명 |
|---|---|
| `asrock_bios.db` | SQLite DB (메인 결과) |
| `asrock_motherboards_master.json` | 전체 모델 목록 |
| `asrock_bios_data_final.json` | 수집 결과 JSON |
| `asrock_checkpoint.json` | 재개용 체크포인트 |
| `asrock_no_bios_models.log` | BIOS 없는 단종/특수 모델 목록 |
| `asrock_scraper.log` | 실행 로그 |

## DB 스키마

```sql
CREATE TABLE motherboards (
    model_name      TEXT PRIMARY KEY,
    series          TEXT,
    platform        TEXT,
    form_factor     TEXT,
    product_url     TEXT,
    image_url       TEXT DEFAULT '',
    category        TEXT DEFAULT '',
    updated_at      TEXT DEFAULT (datetime('now','localtime')),
    last_valid_date TEXT   -- 마지막으로 BIOS가 수집된 날짜
);

CREATE TABLE bios_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name  TEXT,
    version     TEXT,
    date        TEXT,   -- YYYY-MM-DD ISO 형식
    description TEXT,
    link        TEXT,
    UNIQUE(model_name, version)
);
```

## 자동 실행 (crontab)

```cron
0 7 * * * python3 /path/to/asrock_bios_scraper.py
```

# Gigabyte BIOS Scraper

Gigabyte 공식 사이트에서 전체 메인보드 모델의 BIOS 정보를 수집하고 SQLite DB에 저장합니다.  
Nuxt.js SSR 페이지의 `__NUXT_DATA__` JSON을 파싱하며, `requests` 빠른 경로와 `nodriver` 폴백을 함께 사용합니다.

## 수집 흐름

1. **모델 리스트 수집** — `GetSecondProperty`(칩셋) → `GetProducts`(모델명) API
2. **BIOS 수집** — 제품 지원 페이지 HTML → `__NUXT_DATA__` 파싱 (병렬)
   - requests 빠른 경로 우선 시도 → 실패 시 nodriver(Chromium) 폴백
3. **재시도** — 실패 모델 5분 대기 후 1회 재시도
4. **영구 기록** — 재시도 후에도 실패 → `gigabyte_no_bios_models.log`

## 의존성

```
requests
nodriver
tqdm
```

```bash
pip install requests nodriver tqdm
```

## 실행

```bash
# 전체 수집 (기본)
python gigabyte_bios_scraper.py

# no_bios_log 포함 전체 재수집
python gigabyte_bios_scraper.py --recollect

# 단일 모델 디버그 출력
python gigabyte_bios_scraper.py --debug "Z890 AORUS MASTER"
```

## 주요 설정 (`CONFIG`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `workers` | 4 | 병렬 수집 스레드 수 |
| `delay_min/max` | 1.0 / 2.5 | 요청 간격 (초) |
| `retry_wait` | 300 | 재시도 전 대기 시간 (초) |
| `block_cooldown` | 900 | 연속 451/차단 시 대기 (초) |

## 출력 파일

| 파일 | 설명 |
|---|---|
| `gigabyte_bios.db` | SQLite DB (메인 결과) |
| `gigabyte_motherboards_master.json` | 전체 모델 목록 |
| `gigabyte_bios_data_final.json` | 수집 결과 JSON |
| `gigabyte_checkpoint.json` | 재개용 체크포인트 |
| `gigabyte_no_bios_models.log` | BIOS 없는 단종/특수 모델 목록 |
| `gigabyte_scraper.log` | 실행 로그 |

## DB 스키마

```sql
CREATE TABLE motherboards (
    model_id        TEXT PRIMARY KEY,
    model_name      TEXT,
    brand           TEXT DEFAULT '',
    chipset         TEXT DEFAULT '',
    image_url       TEXT DEFAULT '',
    product_url     TEXT DEFAULT '',
    updated_at      TEXT DEFAULT (datetime('now','localtime')),
    last_valid_date TEXT   -- 마지막으로 BIOS가 수집된 날짜
);

CREATE TABLE bios_versions (
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
);
```

## 자동 실행 (crontab)

```cron
0 7 * * * python3 /path/to/gigabyte_bios_scraper.py
```

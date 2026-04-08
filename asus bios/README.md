# ASUS BIOS Scraper

ASUS 공식 지원 API를 통해 전체 메인보드 모델의 BIOS 정보를 수집하고 SQLite DB에 저장합니다.

## 수집 흐름

1. **모델 리스트 수집** — `GetPDLevel` API로 플랫폼·모델 목록 수집
2. **BIOS 수집** — `GetPDSupportTab` → API용 모델명 추출 → `GetPDBIOS` 호출 (병렬)
3. **재시도** — 실패 모델 5분 대기 후 1회 재시도
4. **영구 기록** — 재시도 후에도 실패 → `asus_no_bios_models.log`

## 의존성

```
requests
tqdm
```

```bash
pip install requests tqdm
```

## 실행

```bash
# 전체 수집 (기본)
python asus_bios_scraper.py

# no_bios_log 포함 전체 재수집
python asus_bios_scraper.py --recollect

# 단일 모델 API 응답 디버그 출력
python asus_bios_scraper.py --debug "ROG STRIX B650E-F GAMING WIFI"

# DB를 지정 경로에 저장 (Docker 공유 폴더 등)
python asus_bios_scraper.py --data-dir /volume1/docker/bios-finder/server/data
```

## 주요 설정 (`CONFIG`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `workers` | 2 | 병렬 수집 스레드 수 |
| `delay_min/max` | 2.0 / 4.0 | 요청 간격 (초) |
| `retry_wait` | 300 | 재시도 전 대기 시간 (초) |
| `block_cooldown` | 900 | 연속 451 차단 시 대기 (초) |

## 출력 파일

| 파일 | 설명 |
|---|---|
| `asus_bios.db` | SQLite DB (메인 결과) |
| `asus_motherboards_master.json` | 전체 모델 목록 |
| `asus_bios_data_final.json` | 수집 결과 JSON |
| `asus_checkpoint.json` | 재개용 체크포인트 |
| `asus_no_bios_models.log` | BIOS 없는 단종/특수 모델 목록 |
| `asus_scraper.log` | 실행 로그 |

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
    updated_at      TEXT,
    last_valid_date TEXT   -- 마지막으로 BIOS가 수집된 날짜
);

CREATE TABLE bios_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name  TEXT,
    version     TEXT,
    date        TEXT,
    description TEXT,
    link        TEXT,
    UNIQUE(model_name, version)
);
```

## 자동 실행 (crontab)

```cron
0 7 * * * python3 /path/to/asus_bios_scraper.py
```

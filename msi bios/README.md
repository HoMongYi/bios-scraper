# MSI BIOS Scraper

MSI 공식 지원 API를 통해 전체 메인보드 모델의 BIOS 정보를 수집하고 SQLite DB에 저장합니다.

## 수집 흐름

1. **모델 리스트 수집**
   - `get_tag_list_by_product_line?id=8` → 시리즈 태그 목록
   - `get_product_by_tag?id={tag_id}` → 시리즈별 제품 목록
2. **BIOS 수집** — `/api/v1/product/support/panel?product={link}&type=bios` (병렬)
3. **재시도** — 실패 모델 5분 대기 후 1회 재시도
4. **영구 기록** — 재시도 후에도 실패 → `msi_bios_no_bios_models.log`

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
python msi_bios_scraper.py

# no_bios_log 포함 전체 재수집
python msi_bios_scraper.py --recollect

# 단일 모델 API 응답 디버그 출력
python msi_bios_scraper.py --debug A320M-PRO-C

# DB를 지정 경로에 저장 (Docker 공유 폴더 등)
python msi_bios_scraper.py --data-dir /volume1/docker/bios-finder/server/data
```

## 주요 설정 (`CONFIG`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `workers` | 2 | 병렬 수집 스레드 수 |
| `delay_min/max` | 2.0 / 4.0 | 요청 간격 (초) |
| `retry_wait` | 300 | 재시도 전 대기 시간 (초) |
| `block_cooldown` | 900 | 연속 차단 시 대기 (초) |

## 출력 파일

| 파일 | 설명 |
|---|---|
| `msi_bios.db` | SQLite DB (메인 결과) |
| `msi_bios_motherboards_master.json` | 전체 모델 목록 |
| `msi_bios_data_final.json` | 수집 결과 JSON |
| `msi_bios_checkpoint.json` | 재개용 체크포인트 |
| `msi_bios_no_bios_models.log` | BIOS 없는 단종/특수 모델 목록 |
| `msi_bios_scraper.log` | 실행 로그 |

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
0 7 * * * python3 /path/to/msi_bios_scraper.py
```

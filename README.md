# BIOS Scraper Collection

6개 메인보드 제조사의 공식 사이트에서 BIOS 정보를 자동으로 수집하고 SQLite DB에 저장하는 스크래퍼 모음입니다.

## 지원 제조사

| 폴더 | 제조사 | 수집 방식 |
|---|---|---|
| `asrock bios/` | ASRock | Playwright (병렬) |
| `asus bios/` | ASUS | requests API (병렬) |
| `gigabyte bios/` | Gigabyte | requests + nodriver 폴백 (병렬) |
| `msi bios/` | MSI | requests API (병렬) |
| `biostar bios/` | Biostar | Playwright (순차) |
| `maxsun bios/` | Maxsun | Playwright (순차) |

## 공통 DB 스키마

모든 스크래퍼가 동일한 구조의 SQLite DB를 생성합니다.

```sql
-- 메인보드 모델 테이블
motherboards (
    model_id / model_name  TEXT PRIMARY KEY,
    ...
    image_url       TEXT,   -- DB에 값이 있으면 스크래퍼가 덮어쓰지 않음
    updated_at      TEXT,   -- 마지막 스크랩 시각
    last_valid_date TEXT    -- 마지막으로 BIOS가 수집된 날짜
)

-- BIOS 버전 테이블
bios_versions (
    model_id / model_name  TEXT,
    version         TEXT,
    date            TEXT,
    download_url    TEXT,
    ...
    UNIQUE(model_id, version)
)
```

## 설치

### 공통 의존성

```bash
pip install requests beautifulsoup4 tqdm
```

### Playwright 기반 스크래퍼 (ASRock, Biostar, Maxsun)

```bash
pip install playwright playwright-stealth
playwright install chromium
```

### Gigabyte 추가 의존성

```bash
pip install nodriver
```

## 실행

각 스크래퍼 폴더의 README를 참조하세요.

```bash
# 예시
python "asrock bios/asrock_bios_scraper.py"
python "asus bios/asus_bios_scraper.py"
python "gigabyte bios/gigabyte_bios_scraper.py"
python "msi bios/msi_bios_scraper.py"
python "biostar bios/biostar_bios_scraper.py"
python "maxsun bios/maxsun_bios_scraper.py"
```

## 공통 CLI 인수

| 인수 | 설명 |
|---|---|
| `--recollect` | no_bios_log 포함 전체 재수집 |
| `--retry-db` | DB에서 BIOS 없는 모델만 재시도 |
| `--no-headless` | 브라우저 창 표시 (Playwright 기반만 해당) |
| `--debug` | 디버그 정보 출력 / HTML 저장 |
| `--data-dir PATH` | DB 저장 경로 지정 (기본: 스크래퍼 폴더) |

## 자동 실행 (crontab)

```cron
0 7 * * * python3 /path/to/asrock_bios_scraper.py
0 7 * * * python3 /path/to/asus_bios_scraper.py
0 7 * * * python3 /path/to/gigabyte_bios_scraper.py
0 7 * * * python3 /path/to/msi_bios_scraper.py
0 7 * * * python3 /path/to/biostar_bios_scraper.py
0 7 * * * python3 /path/to/maxsun_bios_scraper.py
```

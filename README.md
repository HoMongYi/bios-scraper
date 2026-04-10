# BIOS Scraper Collection

6개 메인보드 제조사의 공식 사이트에서 BIOS 정보를 자동으로 수집하고 SQLite DB에 저장하는 스크래퍼 모음입니다.

## 지원 제조사

| 폴더 | 제조사 | 수집 방식 | 이미지 수집 |
|---|---|---|---|
| `asrock bios/` | ASRock | Playwright (병렬) | 모델 목록 페이지에서 수집 |
| `asus bios/` | ASUS | requests API (병렬) | BIOS API 응답에 포함 |
| `gigabyte bios/` | Gigabyte | requests + nodriver 폴백 (병렬) | support 페이지 파싱, DB 캐시 활용 |
| `msi bios/` | MSI | requests API (병렬) | 모델 API 응답에 포함 |
| `biostar bios/` | Biostar | Playwright (순차) | 제품 페이지에서 파싱 |
| `maxsun bios/` | Maxsun | Playwright (순차) | 검색 API로 별도 수집, DB 캐시 활용 |

## 공통 DB 스키마

모든 스크래퍼가 동일한 구조의 SQLite DB를 생성합니다.

```sql
-- 메인보드 모델 테이블
motherboards (
    model_id / model_name  TEXT PRIMARY KEY,
    ...
    image_url       TEXT,   -- 기존 값이 있으면 덮어쓰지 않음 (UPSERT 보호)
    updated_at      TEXT,   -- 마지막 스크랩 시각
    last_valid_date TEXT    -- 마지막으로 BIOS가 수집된 날짜
)

-- BIOS 버전 테이블
bios_versions (
    model_id / model_name  TEXT,
    version         TEXT,
    date            TEXT,   -- YYYY-MM-DD ISO 형식 통일 (ORDER BY date DESC 정렬 가능)
    download_url    TEXT,
    ...
    UNIQUE(model_id, version)
)
```

> **이미지 DB 캐시**: Maxsun과 Gigabyte는 실행 시작 시 DB에서 기존 `image_url`을 미리 로드합니다.
> 이미 수집된 모델은 불필요한 이미지 요청을 건너뜁니다.

> **날짜 형식 통일**: 모든 스크래퍼가 `date` 컬럼을 `YYYY-MM-DD` ISO 형식으로 저장합니다.
>
> | 제조사 | 원본 형식 | 저장 형식 |
> |---|---|---|
> | ASRock | `2026/4/7` | `2026-04-07` |
> | ASUS | `2026/4/8` | `2026-04-08` |
> | Gigabyte | `2025-10-17T...` | `2025-10-17` |
> | MSI | `12/28/2024` | `2024-12-28` |
> | Biostar | `2024-12-28` | `2024-12-28` |
> | Maxsun | `10/29/2025` | `2025-10-29` |

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

## 공통 로그 패턴

모든 스크래퍼가 동일한 단계 구조와 로그 형식을 사용합니다.

```
📡 [1단계] {제조사} 메인보드 모델 리스트 수집 중...
✅ 모델 리스트 수집 완료: N개
💾 DB 기존 이미지 N개 로드 (캐시 사용)       ← Maxsun·Gigabyte만
⏩ Resume 모드: N개 이미 완료, 나머지만 수집

🚀 [2단계] 상세 수집 시작 (workers=N)
📊 1차 수집 완료 | 성공: N개 | 실패: N개

⏳ [3단계] 실패 모델 N개 → M분 후 재시도...
🔄 재시도 시작 (N개)
📊 재시도 완료 | 성공: N개 | 최종 실패: N개

✨ 전체 완료!
   ✅ 수집 성공: N개
   🚫 BIOS 없음: N개
```

## 자동 실행 (crontab)

```cron
0 7 * * * python3 /path/to/asrock_bios_scraper.py
0 7 * * * python3 /path/to/asus_bios_scraper.py
0 7 * * * python3 /path/to/gigabyte_bios_scraper.py
0 7 * * * python3 /path/to/msi_bios_scraper.py
0 7 * * * python3 /path/to/biostar_bios_scraper.py
0 7 * * * python3 /path/to/maxsun_bios_scraper.py
```

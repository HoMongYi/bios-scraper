#!/bin/bash
# ============================================================
# BIOS Scraper - 전체 제조사 자동 수집 스크립트
# 대상 NAS: UGREEN DXP2800
# 실행 경로: /volume1/docker/bios-scraper/
# ============================================================

BASE_DIR="/volume1/docker/bios-scraper"
DATA_DIR="/volume1/docker/bios-finder/server/data"
LOG_FILE="$BASE_DIR/run_all.log"
PYTHON="python3"

export PYTHONPATH="/home/plummh15/.local/lib/python3.11/site-packages:$PYTHONPATH"
export PATH="/home/plummh15/.local/bin:$PATH"
export HOME="/home/plummh15"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

run_scraper() {
    local name="$1"
    local script="$2"

    log "===== $name 수집 시작 ====="
    $PYTHON "$script" --data-dir "$DATA_DIR" >> "$LOG_FILE" 2>&1
    local exit_code=$?

    if [ $exit_code -eq 0 ]; then
        log "===== $name 수집 완료 ====="
    else
        log "===== $name 수집 실패 (exit: $exit_code) ====="
    fi
}

mkdir -p "$DATA_DIR"

log "########## 전체 BIOS 수집 시작 ##########"

run_scraper "ASRock"   "$BASE_DIR/asrock bios/asrock_bios_scraper.py"
run_scraper "ASUS"     "$BASE_DIR/asus bios/asus_bios_scraper.py"
run_scraper "Gigabyte" "$BASE_DIR/gigabyte bios/gigabyte_bios_scraper.py"
run_scraper "MSI"      "$BASE_DIR/msi bios/msi_bios_scraper.py"
run_scraper "Biostar"  "$BASE_DIR/biostar bios/biostar_bios_scraper.py"
run_scraper "Maxsun"   "$BASE_DIR/maxsun bios/maxsun_bios_scraper.py"

log "########## 전체 BIOS 수집 완료 ##########"

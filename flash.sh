#!/usr/bin/env bash
# One-key flashing tool for XPeng XP5 boards (runs on the Linux bench PC).
#
# It scans a "prep" folder of downloaded ROM packages, auto-detects connected
# devices by ChipMode, and flashes the requested target(s):
#   xp5_a / xp5_b / vlmu  -> big package (adc_flashall.sh) or small package
#                            (deploy_xpilot_by_adb.sh)
#   mcu                   -> aurix_v7_xp5.sh (adb version)
#
# Package classification is by filename prefix (case-sensitive):
#   VLMU*  = vlmu big      vlmu*  = vlmu small
#   XCCP*  = xp5  big      xpilot*= xp5 small
#   XPilot_AURIX* = mcu
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
step()  { echo -e "${GREEN}==>${NC} $*"; }

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS] [TARGET...]

One-key flash of XP5 packages found in the prep folder.

TARGET: xp5_a | xp5_b | vlmu | mcu | all      (default: all)

Options:
  -d, --dir DIR       Prep folder holding downloaded packages
                      (default: $SCRIPT_DIR/flash_prep)
  -s, --serial SER    Force a specific adb serial (skip ChipMode auto-detect)
      --big           Prefer big packages (adc_flashall.sh) when both exist
      --small         Prefer small packages (deploy_xpilot_by_adb.sh)
  -n, --dry-run       Print planned actions without flashing
  -h, --help          Show this help

Examples:
  $(basename "$0") -n all                 # dry-run: show device + package plan
  $(basename "$0") vlmu                   # flash vlmu (auto big/small + device)
  $(basename "$0") xp5_a xp5_b            # flash both xp5 boards
  $(basename "$0") mcu                    # flash MCU via SoC_A device
  $(basename "$0") --small vlmu           # force small-package vlmu flash
EOF
    exit 0
}

# --- args ---
PREP_DIR="$SCRIPT_DIR/flash_prep"
FORCE_SERIAL=""
SIZE_PREF="auto"
DRY_RUN=false
TARGETS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--dir)     PREP_DIR="$2"; shift 2 ;;
        -s|--serial)  FORCE_SERIAL="$2"; shift 2 ;;
        --big)        SIZE_PREF="big"; shift ;;
        --small)      SIZE_PREF="small"; shift ;;
        -n|--dry-run) DRY_RUN=true; shift ;;
        -h|--help)    usage ;;
        -*)           error "Unknown option: $1"; usage ;;
        *)            TARGETS+=("$1"); shift ;;
    esac
done

[[ ${#TARGETS[@]} -eq 0 ]] && TARGETS=(all)

WORK_ROOT="$PREP_DIR/.extracted"

# --- device detection (ChipMode) ---
SERIAL_VLMU=""; SERIAL_A=""; SERIAL_B=""

detect_devices() {
    step "Detecting connected devices..."
    if ! command -v adb >/dev/null 2>&1; then
        error "adb not found in PATH."; exit 1
    fi
    local serial chipmode found=0
    while read -r serial; do
        [[ -z "$serial" ]] && continue
        found=1
        chipmode=$(adb -s "$serial" shell version </dev/null 2>/dev/null \
            | tr -d '\000\r' \
            | grep -i "Current ChipMode" | head -1 \
            | sed 's/.*ChipMode:[[:space:]]*//' | xargs)
        info "  $serial -> ChipMode: ${chipmode:-unknown}"
        case "$chipmode" in
            SoC_VLMU) SERIAL_VLMU="$serial" ;;
            SoC_A)    SERIAL_A="$serial" ;;
            SoC_B)    SERIAL_B="$serial" ;;
        esac
    done < <(adb devices | awk 'NR>1 && $2=="device" {print $1}')
    [[ $found -eq 0 ]] && warn "  no adb devices online"
    echo ""
}

serial_for_target() {
    local t="$1"
    if [[ -n "$FORCE_SERIAL" ]]; then echo "$FORCE_SERIAL"; return 0; fi
    case "$t" in
        vlmu)  echo "$SERIAL_VLMU" ;;
        xp5_a) echo "$SERIAL_A" ;;
        xp5_b) echo "$SERIAL_B" ;;
        mcu)   echo "$SERIAL_A" ;;
        *)     echo "" ;;
    esac
}

# --- package discovery ---
list_archives_newest_first() {
    # portable (BSD + GNU): list archives in PREP_DIR, newest first
    ( cd "$PREP_DIR" 2>/dev/null && ls -t -- *.tar.gz *.tgz *.zip 2>/dev/null ) \
        | while IFS= read -r b; do [[ -n "$b" ]] && echo "$PREP_DIR/$b"; done
}

# print newest archive whose basename matches glob $1
pick_pkg() {
    local pat="$1" p b
    while IFS= read -r p; do
        [[ -z "$p" ]] && continue
        b=$(basename "$p")
        if [[ "$b" == $pat ]]; then echo "$p"; return 0; fi
    done < <(list_archives_newest_first)
    return 1
}

work_dir_for() {
    local b; b=$(basename "$1")
    b=${b%.zip}; b=${b%.tgz}; b=${b%.tar.gz}; b=${b%.tar}
    echo "$WORK_ROOT/$b"
}

# --- helpers ---
run_in_dir() { local d="$1"; shift; ( cd "$d" && "$@" ); }

exec_step() {  # $1 = display string, rest = command
    local display="$1"; shift
    if [[ "$DRY_RUN" == true ]]; then
        echo -e "${YELLOW}[DRY-RUN]${NC} $display"
        return 0
    fi
    info "$display"
    "$@"
    local rc=$?
    if [[ $rc -ne 0 ]]; then error "FAILED (rc=$rc): $display"; exit $rc; fi
}

extract_pkg() {  # $1 = pkg, $2 = dest
    local pkg="$1" dest="$2"
    if [[ -d "$dest" && -n "$(ls -A "$dest" 2>/dev/null)" ]]; then
        info "already extracted, reuse: $dest"
        return 0
    fi
    mkdir -p "$dest"
    step "Extracting $(basename "$pkg")"
    case "$pkg" in
        *.tar.gz|*.tgz) tar -xzf "$pkg" -C "$dest" ;;
        *.zip)          unzip -oq "$pkg" -d "$dest" ;;
        *) error "unknown archive type: $pkg"; exit 1 ;;
    esac
}

# --- flash routines ---
flash_big() {  # $1 target, $2 pkg, $3 serial
    local t="$1" pkg="$2" serial="$3"
    local work; work=$(work_dir_for "$pkg")
    if [[ "$DRY_RUN" == true ]]; then
        echo -e "${YELLOW}[DRY-RUN]${NC} extract $pkg"
        echo -e "${YELLOW}[DRY-RUN]${NC} cd <pkg>/flash_tools && ./adc_flashall.sh $serial"
        echo -e "${BLUE}[NOTE]${NC} adc_flashall.sh enters fastboot itself (auto_fastboot_by_usb_id.sh)"
        return 0
    fi
    extract_pkg "$pkg" "$work"
    local flashall
    flashall=$(find "$work" -type f -name adc_flashall.sh -path '*flash_tools*' | head -1)
    if [[ -z "$flashall" ]]; then
        error "flash_tools/adc_flashall.sh not found in $pkg"; exit 1
    fi
    chmod +x "$flashall" 2>/dev/null || true
    exec_step "Flash BIG $t via adc_flashall.sh (serial=$serial)" \
        run_in_dir "$(dirname "$flashall")" ./adc_flashall.sh "$serial"
}

flash_small() {  # $1 target, $2 pkg
    local t="$1" pkg="$2"
    local type
    case "$t" in vlmu) type=xp5_vlmu ;; xp5_a) type=xp5_a ;; xp5_b) type=xp5_b ;; esac
    local work; work=$(work_dir_for "$pkg")
    if [[ "$DRY_RUN" == true ]]; then
        echo -e "${YELLOW}[DRY-RUN]${NC} extract $pkg"
        echo -e "${YELLOW}[DRY-RUN]${NC} cd <pkg> && ./deploy_xpilot_by_adb.sh $type"
        return 0
    fi
    extract_pkg "$pkg" "$work"
    local deploy
    deploy=$(find "$work" -type f -name deploy_xpilot_by_adb.sh | head -1)
    if [[ -z "$deploy" ]]; then
        error "deploy_xpilot_by_adb.sh not found in $pkg"; exit 1
    fi
    chmod +x "$deploy" 2>/dev/null || true
    exec_step "Flash SMALL $t via deploy_xpilot_by_adb.sh $type" \
        run_in_dir "$(dirname "$deploy")" ./deploy_xpilot_by_adb.sh "$type"
}

flash_mcu() {  # $1 pkg, $2 serial
    local pkg="$1" serial="$2"
    local work; work=$(work_dir_for "$pkg")
    if [[ "$DRY_RUN" == true ]]; then
        echo -e "${YELLOW}[DRY-RUN]${NC} extract $pkg"
        echo -e "${YELLOW}[DRY-RUN]${NC} copy *_APPA.hex / *_APPB.hex -> $SCRIPT_DIR/"
        echo -e "${YELLOW}[DRY-RUN]${NC} cd $SCRIPT_DIR && ./aurix_v7_xp5.sh -s $serial"
        return 0
    fi
    extract_pkg "$pkg" "$work"
    local appa appb
    appa=$(find "$work" -type f -name '*_APPA.hex' | head -1)
    appb=$(find "$work" -type f -name '*_APPB.hex' | head -1)
    if [[ -z "$appa" || -z "$appb" ]]; then
        error "*_APPA.hex / *_APPB.hex not found in $pkg"; exit 1
    fi
    step "Moving MCU hex files next to aurix_v7_xp5.sh"
    cp -f "$appa" "$SCRIPT_DIR/"
    cp -f "$appb" "$SCRIPT_DIR/"
    if [[ ! -x "$SCRIPT_DIR/aurix_v7_xp5.sh" ]]; then
        chmod +x "$SCRIPT_DIR/aurix_v7_xp5.sh" 2>/dev/null || true
    fi
    exec_step "Flash MCU via aurix_v7_xp5.sh (serial=$serial)" \
        run_in_dir "$SCRIPT_DIR" ./aurix_v7_xp5.sh -s "$serial"
}

# --- job list (ordered: big -> small -> mcu) ---
JOBS_BIG=(); JOBS_SMALL=(); JOBS_MCU=()

add_jobs_for_target() {
    local t="$1" big="" small="" m=""
    if [[ "$t" == "mcu" ]]; then
        m=$(pick_pkg 'XPilot_AURIX*')
        [[ -n "$m" ]] && JOBS_MCU+=("mcu|$m")
        return
    fi
    case "$t" in
        vlmu)        big=$(pick_pkg 'VLMU*'); small=$(pick_pkg 'vlmu*') ;;
        xp5_a|xp5_b) big=$(pick_pkg 'XCCP*'); small=$(pick_pkg 'xpilot*') ;;
    esac
    # auto: flash big AND small (in that order) when both exist
    if [[ "$SIZE_PREF" != "small" && -n "$big"   ]]; then JOBS_BIG+=("$t|$big"); fi
    if [[ "$SIZE_PREF" != "big"   && -n "$small" ]]; then JOBS_SMALL+=("$t|$small"); fi
}

run_job() {  # $1 kind, $2 target, $3 pkg
    local kind="$1" t="$2" pkg="$3" serial
    step "Target: $t  [$kind]"
    serial=$(serial_for_target "$t")
    info "  package : $(basename "$pkg")"
    info "  device  : ${serial:-<none detected>}"
    if [[ "$DRY_RUN" != true && -z "$serial" ]]; then
        warn "  no matching device online for '$t' — skipped"; echo ""; return 0
    fi
    [[ -z "$serial" ]] && serial="<auto-detect>"
    case "$kind" in
        big)   flash_big   "$t" "$pkg" "$serial" ;;
        small) flash_small "$t" "$pkg" ;;
        mcu)   flash_mcu   "$pkg" "$serial" ;;
        *)     error "  unknown job kind: $kind"; exit 1 ;;
    esac
    echo ""
}

run_jobs() {  # $1 kind, rest = "target|pkg" entries
    local kind="$1"; shift
    local entry
    for entry in "$@"; do
        [[ -z "$entry" ]] && continue
        run_job "$kind" "${entry%%|*}" "${entry#*|}"
    done
}

# --- main ---
echo ""
ok "XP5 one-key flash"
info "Prep folder : $PREP_DIR"
info "Dry-run     : $DRY_RUN"
info "Size pref   : $SIZE_PREF"
echo ""

if [[ ! -d "$PREP_DIR" ]]; then
    error "Prep folder not found: $PREP_DIR"
    error "Download packages first, e.g.: ./download_rom.sh -p E29 -a <DT...>"
    exit 1
fi

# expand 'all'
EXPANDED=()
for t in "${TARGETS[@]}"; do
    if [[ "$t" == "all" ]]; then
        EXPANDED+=(vlmu xp5_a xp5_b mcu)
    else
        EXPANDED+=("$t")
    fi
done

detect_devices

mkdir -p "$WORK_ROOT"

# build job list from requested targets
for t in ${EXPANDED[@]+"${EXPANDED[@]}"}; do
    case "$t" in
        xp5_a|xp5_b|vlmu|mcu) add_jobs_for_target "$t" ;;
        *) error "Unknown target: $t (use xp5_a|xp5_b|vlmu|mcu|all)"; exit 1 ;;
    esac
done

if [[ ${#JOBS_BIG[@]} -eq 0 && ${#JOBS_SMALL[@]} -eq 0 && ${#JOBS_MCU[@]} -eq 0 ]]; then
    warn "No packages matched in $PREP_DIR for the requested target(s)."
fi

# execute in strict order: 大包 -> 小包 -> MCU
step "Flash order: 大包 -> 小包 -> MCU"
echo ""
run_jobs big   ${JOBS_BIG[@]+"${JOBS_BIG[@]}"}
run_jobs small ${JOBS_SMALL[@]+"${JOBS_SMALL[@]}"}
run_jobs mcu   ${JOBS_MCU[@]+"${JOBS_MCU[@]}"}

if [[ "$DRY_RUN" == true ]]; then
    ok "Dry-run complete. No changes made."
else
    ok "Flashing finished."
fi

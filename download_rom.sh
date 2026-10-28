#!/usr/bin/env bash
set -euo pipefail

BASE_URL="https://cicdops.xiaopeng.link/data/xpilot"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS] <DT_NUMBER> [DT_NUMBER...]

Download ROM files by one or more DT numbers from CI platform.
Multiple DT numbers are downloaded into a single shared folder (batch mode).

Options:
  -p, --platform PLATFORM   Vehicle model code (e.g. E29, F30, D02ES).
                            If not specified, will list available vehicle models.
  -l, --list-only           Only list files, do not download.
  -d, --download-dir DIR    Download directory (default: ./flash_prep/<timestamp>).
                            Each run without -d gets its own timestamped folder so
                            downloads never overwrite each other.
  -f, --filter PATTERN      Filter files by pattern (e.g. "ENG_DEV", "VLMU").
  -T, --type TYPE           Package type; auto-sets path prefix + filter:
                              vlmu-big | vlmu-small | xpu-big | xpu-small | mcu
                            (mcu uses the MCUDEBUG-<DT> tree)
  -a, --all                 Download all files without prompting.
  -w, --watch [MINUTES]     Poll mode: check every N minutes (default 10) until
                            ROM files appear, then auto-download. Default filter
                            is "ENG_DEV" unless -f is specified.
  -t, --timeout HOURS       Max polling duration in hours (default: 24).
                            Only used with --watch.
  -h, --help                Show this help message.

Examples:
  $(basename "$0") 169233                          # Interactive selection
  $(basename "$0") -p E29 169233                   # Specify vehicle model
  $(basename "$0") -l 169233                       # List files only
  $(basename "$0") -p E29 -f "ENG_DEV" 169233     # Filter and download
  $(basename "$0") -p E29 -a 169233               # Download all files
  $(basename "$0") -p E29 -a 196163 196164        # Batch: multiple DTs -> new timestamped folder
  $(basename "$0") -p E29 -w 169233               # Poll every 10min, auto-download ENG_DEV
  $(basename "$0") -p E29 -w 30 169233            # Poll every 30min, auto-download ENG_DEV
  $(basename "$0") -p E29 -w -t 48 169233         # Poll for up to 48 hours
EOF
    exit 0
}

# --- Parse args ---
PLATFORM=""
LIST_ONLY=false
DOWNLOAD_DIR=""
FILTER=""
DOWNLOAD_ALL=false
WATCH=0
TIMEOUT=24
DEFAULT_WATCH_INTERVAL=10
DT_NUMBERS=()
DT_NUMBER=""
TYPE=""
DT_PREFIX="DEBUG"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--platform)  PLATFORM="$2"; shift 2 ;;
        -l|--list-only) LIST_ONLY=true; shift ;;
        -d|--download-dir) DOWNLOAD_DIR="$2"; shift 2 ;;
        -f|--filter)    FILTER="$2"; shift 2 ;;
        -T|--type)      TYPE="$2"; shift 2 ;;
        -a|--all)       DOWNLOAD_ALL=true; shift ;;
        -t|--timeout)   TIMEOUT="$2"; shift 2 ;;
        -w|--watch)
            # If next arg is a small number (<=1440 min = 24h), treat as interval;
            # otherwise it's likely a DT number, use default interval
            if [[ $# -gt 1 && "$2" =~ ^[0-9]+$ && "$2" -le 1440 ]]; then
                WATCH="$2"; shift 2
            else
                WATCH=$DEFAULT_WATCH_INTERVAL; shift
            fi
            ;;
        -h|--help)      usage ;;
        -*)             error "Unknown option: $1"; usage ;;
        *)              DT_NUMBERS+=("$1"); shift ;;
    esac
done

if [[ ${#DT_NUMBERS[@]} -eq 0 ]]; then
    error "At least one DT number is required."
    usage
fi

# --- Package type -> DT path prefix + default filter ---
# vlmu/xpu big packages live under <COMP>/ENG_DEV|ENG_DEBUG/; small ones sit
# directly under <COMP>/. MCU lives under a MCUDEBUG-<dt> tree instead of DEBUG-.
case "$TYPE" in
    vlmu-big)   DT_PREFIX="DEBUG";    : "${FILTER:=VLMU/ENG_}" ;;
    vlmu-small) DT_PREFIX="DEBUG";    : "${FILTER:=VLMU/vlmu_xpilot_release}" ;;
    xpu-big)    DT_PREFIX="DEBUG";    : "${FILTER:=XPU/ENG_}" ;;
    xpu-small)  DT_PREFIX="DEBUG";    : "${FILTER:=XPU/xpilot_release_full}" ;;
    mcu)        DT_PREFIX="MCUDEBUG"; : "${FILTER:=XPilot_AURIX}" ;;
    "")         DT_PREFIX="DEBUG" ;;
    *)          error "Unknown type: $TYPE (use vlmu-big|vlmu-small|xpu-big|xpu-small|mcu)"; exit 1 ;;
esac

# Build the CI browse path for a DT number, honoring the prefix.
dt_path() { echo "images/DEBUG/${PLATFORM}/${DT_PREFIX}-$1/"; }

# --- Browse API ---
browse() {
    local path="$1"
    curl -sfS "${BASE_URL}/browse/${path}" 2>/dev/null
}

# --- List platforms ---
list_platforms() {
    info "Fetching available vehicle models..."
    local json
    json=$(browse "images/DEBUG/")
    if [[ -z "$json" ]]; then
        error "Failed to fetch vehicle model list. Check network connectivity."
        exit 1
    fi
    echo "$json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for d in sorted(data.get('directories', [])):
    # Extract platform name from 'images/DEBUG/E29/' -> 'E29'
    parts = d.rstrip('/').split('/')
    if len(parts) >= 3:
        print(parts[2])
"
}

# --- Recursively find all files using python3 ---
find_files() {
    local path="$1"
    browse "$path" | python3 -c "
import sys, json

FILTER = '''$FILTER'''

def find_all(path, data):
    for d in data.get('directories', []):
        import urllib.request
        url = 'https://cicdops.xiaopeng.link/data/xpilot/browse/' + d
        try:
            resp = urllib.request.urlopen(url)
            sub = json.loads(resp.read())
            find_all(d, sub)
        except:
            pass
    for f in data.get('files', []):
        key = f['key']
        if key.endswith('.md5'):
            continue
        if FILTER and FILTER.lower() not in key.lower():
            continue
        print(key + '\t' + str(f.get('size', 0)))

data = json.load(sys.stdin)
find_all('$path', data)
"
}

# --- Format file size ---
format_size() {
    local bytes=$1
    if [[ $bytes -ge 1073741824 ]]; then
        echo "$(echo "scale=2; $bytes / 1073741824" | bc) GB"
    elif [[ $bytes -ge 1048576 ]]; then
        echo "$(echo "scale=2; $bytes / 1048576" | bc) MB"
    elif [[ $bytes -ge 1024 ]]; then
        echo "$(echo "scale=2; $bytes / 1024" | bc) KB"
    else
        echo "${bytes} B"
    fi
}

# --- Watch mode: poll until ROM appears then auto-download ---
watch_poll() {
    local interval_min="${WATCH}"
    local timeout_hrs="${TIMEOUT}"
    local start_time
    start_time=$(date +%s)
    local timeout_sec=$((timeout_hrs * 3600))
    local check_interval=$((interval_min * 60))
    local attempt=0

    # Default filter to ENG_DEV in watch mode
    if [[ -z "$FILTER" ]]; then
        FILTER="ENG_DEV"
    fi

    echo ""
    ok "Watch mode started"
    info "DT: ${DT_NUMBER} | Vehicle model: ${PLATFORM} | Filter: ${FILTER}"
    info "Polling every ${interval_min} min | Timeout: ${timeout_hrs}h"
    info "Press Ctrl+C to stop"
    echo ""

    while true; do
        attempt=$((attempt + 1))
        local now
        now=$(date +%s)
        local elapsed=$((now - start_time))

        if [[ $elapsed -ge $timeout_sec ]]; then
            error "Timeout reached (${timeout_hrs}h). No ROM found for DT ${DT_NUMBER}."
            exit 1
        fi

        echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${BLUE}[POLL #${attempt}]${NC} Checking for ROM files..."

        local json
        json=$(browse "$(dt_path "$DT_NUMBER")")

        if [[ -z "$json" ]]; then
            local remaining_min=$(( (timeout_sec - elapsed) / 60 ))
            warn "DT ${DT_NUMBER} not found yet. Will retry in ${interval_min} min (${remaining_min} min remaining)"
        else
            # Found the DT directory, scan for files
            local file_count
            file_count=$(echo "$json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
count = 0
for f in data.get('files', []):
    if not f['key'].endswith('.md5'):
        count += 1
print(count)
" 2>/dev/null || echo "0")

            # Recursively check for filtered files
            local tmpfile
            tmpfile=$(mktemp)
            find_files "$(dt_path "$DT_NUMBER")" > "$tmpfile" 2>/dev/null || true
            local filtered_count
            filtered_count=$(wc -l < "$tmpfile" | tr -d ' ')

            if [[ "$filtered_count" -eq 0 ]]; then
                local remaining_min=$(( (timeout_sec - elapsed) / 60 ))
                warn "DT directory exists but no '${FILTER}' files yet. Will retry in ${interval_min} min (${remaining_min} min remaining)"
            else
                ok "Found ${filtered_count} file(s) matching '${FILTER}'!"
                echo ""
                # Show the files
                while IFS=$'\t' read -r key size; do
                    local fname
                    fname=$(basename "$key")
                    if [[ -n "$size" && "$size" != "0" ]]; then
                        echo -e "  ${GREEN}*${NC} ${fname} ($(format_size "$size"))"
                    else
                        echo -e "  ${GREEN}*${NC} ${fname}"
                    fi
                done < "$tmpfile"
                echo ""
                rm -f "$tmpfile"

                ok "Starting download..."
                echo ""
                return 0
            fi
            rm -f "$tmpfile"
        fi

        # Sleep in short chunks so system sleep/wake doesn't freeze us for hours
        local next_poll_time
        next_poll_time=$(python3 -c "from datetime import datetime, timedelta; print((datetime.now() + timedelta(minutes=${interval_min})).strftime('%Y-%m-%d %H:%M:%S'))")
        info "Next poll at: ${next_poll_time} (in ${interval_min} min)"
        echo ""

        local slept_seconds=0
        while [[ $slept_seconds -lt $check_interval ]]; do
            sleep 10
            slept_seconds=$((slept_seconds + 10))
        done
    done
}

# --- Select platform ---
select_platform() {
    local platforms
    platforms=$(list_platforms)
    if [[ -z "$platforms" ]]; then
        error "No vehicle models found."
        exit 1
    fi

    echo ""
    info "Available vehicle models:"
    echo "$platforms" | nl -w3 -s'. '
    echo ""
    read -rp "Select vehicle model number (or type model code): " choice

    if [[ "$choice" =~ ^[0-9]+$ ]]; then
        PLATFORM=$(echo "$platforms" | sed -n "${choice}p")
    else
        PLATFORM="$choice"
    fi

    if [[ -z "$PLATFORM" ]]; then
        error "Invalid vehicle model selection."
        exit 1
    fi
    ok "Vehicle model: $PLATFORM"
}

# --- Download file ---
download_file() {
    local file_key="$1"
    local dest_dir="$2"
    local filename
    filename=$(basename "$file_key")
    local dest_path="${dest_dir}/${filename}"
    local url="${BASE_URL}/${file_key}"

    if [[ -f "$dest_path" ]]; then
        info "Resuming download: $filename"
        curl -fL -C - -o "$dest_path" "$url"
    else
        info "Downloading: $filename"
        curl -fL -o "$dest_path" "$url"
    fi

    if [[ $? -eq 0 ]]; then
        ok "Downloaded: $filename"
    else
        error "Failed to download: $filename"
    fi
}

# --- Process a single DT: scan, select, download ---
process_dt() {
    local dt="$1"
    DT_NUMBER="$dt"

    # MCU: match only this DT's top-level XPilot_AURIX zip (skip loose .hex files)
    if [[ "$TYPE" == "mcu" ]]; then
        FILTER="MCUDEBUG-${dt}.zip"
    fi

    # Watch mode: poll until ROM appears, then auto-download first match
    WATCH_AUTO_DOWNLOAD_FIRST=false
    if [[ "$WATCH" -gt 0 ]]; then
        watch_poll
        WATCH_AUTO_DOWNLOAD_FIRST=true
    fi

    info "Scanning files for ${DT_PREFIX}-${dt} on vehicle model ${PLATFORM}..."
    echo ""

    local tmpfile
    tmpfile=$(mktemp)
    find_files "$(dt_path "$dt")" > "$tmpfile" || true

    ALL_FILES=()
    ALL_SIZES=()
    local key size
    while IFS=$'\t' read -r key size; do
        [[ -z "$key" ]] && continue
        ALL_FILES+=("$key")
        ALL_SIZES+=("$size")
    done < "$tmpfile"
    rm -f "$tmpfile"

    if [[ ${#ALL_FILES[@]} -eq 0 ]]; then
        error "No files found for DT ${dt} on vehicle model ${PLATFORM}."
        error "Please verify the DT number and vehicle model are correct."
        return 1
    fi

    info "Found ${#ALL_FILES[@]} file(s):"
    echo ""
    local i local_path size_str prefix
    prefix="images/DEBUG/${PLATFORM}/${DT_PREFIX}-${dt}/"
    for i in "${!ALL_FILES[@]}"; do
        local_path="${ALL_FILES[$i]#$prefix}"
        size="${ALL_SIZES[$i]}"
        if [[ -n "$size" && "$size" != "0" ]]; then
            size_str="($(format_size "$size"))"
        else
            size_str=""
        fi
        printf "  %2d. %s %s\n" $((i+1)) "$local_path" "$size_str"
    done

    if [[ "$LIST_ONLY" == true ]]; then
        return 0
    fi

    echo ""

    SELECTED=()
    if [[ "$DOWNLOAD_ALL" == true ]]; then
        SELECTED=("${ALL_FILES[@]}")
        ok "Will download all ${#ALL_FILES[@]} file(s)."
    elif [[ "$WATCH_AUTO_DOWNLOAD_FIRST" == true ]]; then
        SELECTED=("${ALL_FILES[0]}")
        ok "Watch mode: will download the first file only"
        info "$(basename "${ALL_FILES[0]}")"
    else
        local selection num idx
        read -rp "Enter file numbers to download (e.g. 1 3 5, or 'all'): " selection
        if [[ "$selection" == "all" || "$selection" == "a" ]]; then
            SELECTED=("${ALL_FILES[@]}")
        else
            for num in $selection; do
                idx=$((num-1))
                if [[ $idx -ge 0 && $idx -lt ${#ALL_FILES[@]} ]]; then
                    SELECTED+=("${ALL_FILES[$idx]}")
                else
                    warn "Invalid selection: $num (skipped)"
                fi
            done
        fi
    fi

    if [[ ${#SELECTED[@]} -eq 0 ]]; then
        warn "No files selected for DT ${dt}."
        return 0
    fi

    echo ""
    info "Will download ${#SELECTED[@]} file(s) to: ${DOWNLOAD_DIR}"
    echo ""

    local file_key
    for file_key in "${SELECTED[@]}"; do
        download_file "$file_key" "$DOWNLOAD_DIR"
        echo ""
    done
    return 0
}

# --- Main ---
if [[ -z "$PLATFORM" ]]; then
    select_platform
fi

# Default: a fresh timestamped session folder under flash_prep so repeated
# downloads never overwrite/mix with each other. flash_menu.sh lists these.
DOWNLOAD_DIR="${DOWNLOAD_DIR:-./flash_prep/$(date +%Y%m%d_%H%M%S)}"
# only create the folder when we actually download (not in list-only mode)
[[ "$LIST_ONLY" == true ]] || mkdir -p "$DOWNLOAD_DIR"

if [[ ${#DT_NUMBERS[@]} -gt 1 ]]; then
    info "Batch mode: ${#DT_NUMBERS[@]} DT numbers -> ${DOWNLOAD_DIR}"
fi

FAIL_COUNT=0
for dt in "${DT_NUMBERS[@]}"; do
    echo ""
    info "==== Processing DT ${dt} ===="
    if ! process_dt "$dt"; then
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
done

echo ""
if [[ "$LIST_ONLY" == true ]]; then
    info "List-only mode, exiting."
    exit 0
fi

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    warn "${FAIL_COUNT} DT number(s) had no files or failed."
fi
ok "All downloads completed. Files saved to: ${DOWNLOAD_DIR}"

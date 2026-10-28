#!/bin/bash
# MCU (Aurix) flashing via adb.
# Converted from the original ssh/scp version (which talked to root@172.20.1.22).
# Target device: the SoC_A (xp5_a) board, where /xpilot/aurix_utility runs.
#
# Usage: aurix_v7_xp5.sh [-s SERIAL] [S]
#   -s SERIAL   adb serial of the SoC_A device (auto-detected if omitted)
#   S           flash the slave Aurix (--m=1)

AURIX_IMG="latest_aurix"
SLAVE_OPTION=""
DEV=""                 # adb serial of the SoC_A device
APPA_hex=""
APPB_hex=""

# default is 0 / A is 1 / B is 2
CURR_AURIX_ZONE=0
NEXT_FLASH_ZONE=0
FINISH_FLASH_ZONE=0

AURIX_UTIL="/xpilot/aurix_utility/aurix_utility"
FLASHDRV="/xpilot/aurix_utility/FlashDrv/FlashDrv.hex"

# --- adb helpers ---
adb_sh() {
    adb -s "$DEV" shell "$@"
}

# auto-detect the SoC_A (xp5_a) device serial via `adb shell version` ChipMode
detect_soc_a() {
    local serial chipmode
    for serial in $(adb devices | awk 'NR>1 && $2=="device" {print $1}'); do
        chipmode=$(adb -s "$serial" shell version </dev/null 2>/dev/null \
            | tr -d '\000\r' \
            | grep -i "Current ChipMode" | head -1 \
            | sed 's/.*ChipMode:[[:space:]]*//' | xargs)
        if [[ "$chipmode" == "SoC_A" ]]; then
            echo "$serial"
            return 0
        fi
    done
    return 1
}

# check whether the adb device is online (replaces check_ssh_status)
check_device_online() {
    local state
    state=$(adb -s "$DEV" get-state 2>/dev/null | tr -d '\r')
    [[ "$state" == "device" ]]
}

# wait for the device to come back online (replaces wait_xpu_online)
wait_device_online() {
    echo -e "\033[41;37mdevice $DEV offline, waiting for it to come back...\033[0m"
    local retries=0
    while ! check_device_online; do
        sleep 5
        retries=$((retries + 1))
        if [[ $retries -ge 60 ]]; then
            echo "device $DEV still offline after $((retries * 5))s"
            return 1
        fi
    done
    return 0
}

# push the two hex files to /xdata on the device (replaces copy_image scp)
copy_image() {
    adb_sh 'chmod 777 /xdata' 2>/dev/null
    adb_sh "rm -rf /xdata/$AURIX_IMG"
    adb_sh "mkdir -p /xdata/$AURIX_IMG"

    APPA_hex=$(ls ./ | grep -E '_APPA\.hex$' | head -1)
    APPB_hex=$(ls ./ | grep -E '_APPB\.hex$' | head -1)

    if [[ -z "$APPA_hex" || -z "$APPB_hex" ]]; then
        echo "Error: cannot find *_APPA.hex / *_APPB.hex in current directory"
        exit 1
    fi

    echo "pushing $APPA_hex and $APPB_hex to /xdata/$AURIX_IMG/"
    adb -s "$DEV" push "./$APPA_hex" "/xdata/$AURIX_IMG/" || exit 1
    adb -s "$DEV" push "./$APPB_hex" "/xdata/$AURIX_IMG/" || exit 1
}

# flash APPA (device currently running zone B)
flash_APPA() {
    echo "Currently Running on Zone: B"
    echo "Currently Flashing Zone: A"
    adb_sh "$AURIX_UTIL --update=/xdata/$AURIX_IMG/$APPA_hex,$FLASHDRV --reboot=1$SLAVE_OPTION"
}

# flash APPB (device currently running zone A)
flash_APPB() {
    echo "Currently Running on Zone: A"
    echo "Currently Flashing Zone: B"
    adb_sh "$AURIX_UTIL --update=/xdata/$AURIX_IMG/$APPB_hex,$FLASHDRV --reboot=1$SLAVE_OPTION"
}

# check which APP zone the MCU is running (replaces ssh + /tmp + scp round-trip)
check_zone() {
    local out result
    out=$(adb_sh "$AURIX_UTIL -v$SLAVE_OPTION" 2>/dev/null | tr -d '\r')
    if [[ -z "$out" ]]; then
        echo "Error: cannot read aurix zone from device $DEV"
        exit 1
    fi

    result=$(echo "$out" | grep "Aurix active boot" | grep ":A")
    if [[ -n "$result" ]]; then
        CURR_AURIX_ZONE=1
    else
        CURR_AURIX_ZONE=2
    fi
    echo "$CURR_AURIX_ZONE"
}

########## Main program starts here ##########

while [[ $# -gt 0 ]]; do
    case "$1" in
        -s|--serial) DEV="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $(basename "$0") [-s SERIAL] [S]"
            exit 0 ;;
        S)
            echo "Flashing Slave!"
            SLAVE_OPTION=" --m=1"
            shift ;;
        *)
            echo "Invalid argument: $1"
            exit 1 ;;
    esac
done

if [[ -z "$DEV" ]]; then
    echo "No serial given, auto-detecting SoC_A (xp5_a) device..."
    DEV=$(detect_soc_a)
    if [[ -z "$DEV" ]]; then
        echo -e "\033[41;37mError: no SoC_A (xp5_a) device found via adb\033[0m"
        exit 1
    fi
fi
echo "Using adb device: $DEV"

# make sure the device is online before starting
while ! check_device_online; do
    wait_device_online || exit 1
done

copy_image

check_zone

# do flash (opposite zone first)
if [[ $CURR_AURIX_ZONE -eq 1 ]]; then
    flash_APPB
    FINISH_FLASH_ZONE=2
    echo "FINISH_FLASH_ZONE = 2"
elif [[ $CURR_AURIX_ZONE -eq 2 ]]; then
    flash_APPA
    FINISH_FLASH_ZONE=1
    echo "FINISH_FLASH_ZONE = 1"
else
    echo -e "\033[41;37mCannot Start Flash!\033[0m"
    exit 1
fi

sleep 30

# device reboots after flashing; wait for it to come back
while ! check_device_online; do
    wait_device_online || exit 1
done

# check aurix zone again
check_zone

echo "FINISH_FLASH_ZONE: $FINISH_FLASH_ZONE"
echo "CURR_AURIX_ZONE: $CURR_AURIX_ZONE"

# another checkpoint for protection
if [[ -z "$FINISH_FLASH_ZONE" || "$FINISH_FLASH_ZONE" -ne "$CURR_AURIX_ZONE" ]]; then
    echo -e "\033[41;37mSwitch failed! Cannot Start Flash!\033[0m"
    exit 1
fi

# do flash
if [[ $CURR_AURIX_ZONE -eq 1 ]]; then
    flash_APPB
elif [[ $CURR_AURIX_ZONE -eq 2 ]]; then
    flash_APPA
else
    echo -e "\033[41;37mCannot Start Flash!\033[0m"
    exit 1
fi

sleep 30

exit 0

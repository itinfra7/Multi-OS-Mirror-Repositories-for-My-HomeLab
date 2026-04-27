#!/bin/sh
# Credits: itinfra7 from GitHub
#
# Alpine Mirror - Real-time Monitor v6
# Reads rsync --info=progress2 output for accurate 1% progress
# Platform: Alpine Linux (BusyBox)
#

REPO="/alpine-repo"
LOG="/var/log/alpine-mirror-sync.log"
STATE="$REPO/.mirror-state"
PFILE="/tmp/.mirror-progress"
RFILE="/tmp/.mirror-rsync-out"
RFILE_CHK="/tmp/.mirror-last-chk"
FILES_DONE="/tmp/.mirror-files-done"
TZ="Asia/Seoul"; export TZ

# ── ANSI ──
R='\033[0m'; B='\033[1m'; D='\033[2m'
GR='\033[32;1m'; YL='\033[33;1m'; RD='\033[31;1m'
CN='\033[36;1m'; BL='\033[34;1m'; WH='\033[37;1m'; MG='\033[35;1m'

W=100
rpt() { local i=0; while [ $i -lt "$2" ]; do printf '%s' "$1"; i=$((i+1)); done; }
BLINE=$(rpt '═' $((W-2)))
SLINE=$(rpt '─' $((W-6)))

hdr() { printf "${BL}╔%s╗${R}\n" "$BLINE"; }
mid() { printf "${BL}╠%s╣${R}\n" "$BLINE"; }
ftr() { printf "${BL}╚%s╝${R}\n" "$BLINE"; }
L()  { printf "${BL}║${R} %b\033[${W}G${BL}║${R}\n" "$1"; }
E()  { printf "${BL}║${R}\033[${W}G${BL}║${R}\n"; }

bar() {
    local p=$1 w=${2:-32} f e i
    [ "$p" -gt 100 ] 2>/dev/null && p=100
    [ "$p" -lt 0 ]   2>/dev/null && p=0
    f=$((p * w / 100)); e=$((w - f)); i=0
    printf "${CN}"; while [ $i -lt $f ]; do printf "█"; i=$((i+1)); done
    printf "${D}"; i=0; while [ $i -lt $e ]; do printf "░"; i=$((i+1)); done
    printf "${R}"
}

elapsed_sec() {
    [ -d "/proc/$1" ] || { echo 0; return; }
    local now s
    now=$(date +%s); s=$(stat -c %Y "/proc/$1" 2>/dev/null)
    [ -n "$s" ] && [ "$s" -gt 0 ] 2>/dev/null && echo $((now-s)) || echo 0
}
fmt_time() {
    local d=$1
    [ "$d" -le 0 ] 2>/dev/null && { echo "--:--:--"; return; }
    printf "%02d:%02d:%02d" $((d/3600)) $(((d%3600)/60)) $((d%60))
}

http_conns() { awk '$2~/:0050$/ && $4=="01"{c++} END{print c+0}' /proc/net/tcp 2>/dev/null; }
http_total() { [ -f /var/log/nginx/access.log ] && wc -l < /var/log/nginx/access.log | tr -d ' ' || echo 0; }

# ── Read rsync progress2 output ──
# rsync uses \r in output even when redirected to file, so we handle that
read_rsync_progress() {
    [ -f "$RFILE" ] || return
    local line chkline
    line=$(tail -c 500 "$RFILE" 2>/dev/null | tr '\r' '\n' | grep -v '^$' | tail -1)
    [ -z "$line" ] && return

    RSYNC_PCT=$(echo "$line" | grep -oE '[0-9]+%' | head -1 | tr -d '%')
    RSYNC_SPEED=$(echo "$line" | grep -oE '[0-9.]+[kMGT]B/s' | head -1)
    RSYNC_XFR=$(echo "$line" | grep -oE 'xfr#[0-9]+' | sed 's/xfr#//')
    RSYNC_REMAIN=$(echo "$line" | grep -oE 'to-chk=[0-9]+/[0-9]+')

    # to-chk doesn't appear on every line; search last 50KB
    if [ -z "$RSYNC_REMAIN" ]; then
        chkline=$(tail -c 50000 "$RFILE" 2>/dev/null | tr '\r' '\n' | grep 'to-chk=' | tail -1)
        if [ -n "$chkline" ]; then
            RSYNC_REMAIN=$(echo "$chkline" | grep -oE 'to-chk=[0-9]+/[0-9]+')
            [ -z "$RSYNC_XFR" ] && RSYNC_XFR=$(echo "$chkline" | grep -oE 'xfr#[0-9]+' | sed 's/xfr#//')
        fi
    fi

    # Cache last known to-chk so it never disappears
    if [ -n "$RSYNC_REMAIN" ]; then
        echo "$RSYNC_REMAIN $RSYNC_XFR" > "$RFILE_CHK"
    elif [ -f "$RFILE_CHK" ]; then
        RSYNC_REMAIN=$(awk '{print $1}' "$RFILE_CHK")
        [ -z "$RSYNC_XFR" ] && RSYNC_XFR=$(awk '{print $2}' "$RFILE_CHK")
    fi
}

# ══════════════════════════════ MAIN LOOP ══════════════════════════════
while true; do
    clear
    hdr
    L "  ${B}${WH}⛰  Alpine Linux Repository Mirror${R}  ${D}—  Real-time Monitor${R}"
    mid

    PID=$(pgrep -f "alpine-mirror-sync.sh" 2>/dev/null | head -1)

    if [ -n "$PID" ]; then
        EL_SEC=$(elapsed_sec "$PID")
        EL=$(fmt_time $EL_SEC)

        # ── Read progress from sync script's progress file ──
        P_DONE=0; P_TOTAL=10; P_CURRENT="starting..."
        if [ -f "$PFILE" ]; then
            P_DONE=$(awk '{print $1}' "$PFILE" 2>/dev/null)
            P_TOTAL=$(awk '{print $2}' "$PFILE" 2>/dev/null)
            P_CURRENT=$(awk '{print $3}' "$PFILE" 2>/dev/null)
        fi
        [ -z "$P_DONE" ]  && P_DONE=0
        [ -z "$P_TOTAL" ] && P_TOTAL=10
        [ "$P_TOTAL" -eq 0 ] 2>/dev/null && P_TOTAL=10

        # ── Read rsync real-time progress ──
        RSYNC_PCT=0; RSYNC_SPEED=""; RSYNC_XFR=""; RSYNC_REMAIN=""
        read_rsync_progress
        [ -z "$RSYNC_PCT" ] && RSYNC_PCT=0

        # ── Calculate overall percentage (1% granularity) ──
        # overall = (done_jobs / total_jobs * 100) + (current_rsync_pct / total_jobs)
        OVERALL=$(( (P_DONE * 100 / P_TOTAL) + (RSYNC_PCT / P_TOTAL) ))
        [ "$OVERALL" -gt 100 ] && OVERALL=100

        # ── Calculate stable ETA from overall progress + elapsed ──
        CALC_ETA=""
        if [ "$OVERALL" -gt 0 ] && [ "$EL_SEC" -gt 30 ]; then
            ETA_SEC=$(( EL_SEC * (100 - OVERALL) / OVERALL ))
            CALC_ETA=$(fmt_time $ETA_SEC)
        fi

        # ── Display ──
        E
        if [ -n "$CALC_ETA" ]; then
            L "  ${B}STATUS${R}     ${YL}● SYNCING${R}    ${D}PID ${PID}${R}  │  ${D}running${R} ${WH}${EL}${R}  │  ${D}ETA${R} ${YL}${CALC_ETA}${R}"
        else
            L "  ${B}STATUS${R}     ${YL}● SYNCING${R}    ${D}PID ${PID}${R}  │  ${D}running${R} ${WH}${EL}${R}"
        fi
        E
        L "  ${B}OVERALL${R}    [$(bar $OVERALL)]  ${WH}$(printf '%3d' $OVERALL)%${R}   ${D}(job ${P_DONE}/${P_TOTAL})${R}"
        L "  ${B}JOB${R}        [$(bar $RSYNC_PCT 32)]  ${WH}$(printf '%3d' $RSYNC_PCT)%${R}   ${D}(current rsync)${R}"
        L "  ${B}CURRENT${R}    ${CN}${P_CURRENT}${R}"

        if [ -n "$RSYNC_SPEED" ]; then
            L "  ${B}SPEED${R}      ${GR}⚡ ${RSYNC_SPEED}${R}"
        else
            L "  ${B}SPEED${R}      ${D}building file list...${R}"
        fi

        # ── Files: all jobs + current job ──
        all_done_f=0
        if [ -f "$FILES_DONE" ]; then
            all_done_f=$(awk '{print $1}' "$FILES_DONE" 2>/dev/null)
        fi
        [ -z "$all_done_f" ] && all_done_f=0

        if [ -n "$RSYNC_REMAIN" ]; then
            remain_n=$(echo "$RSYNC_REMAIN" | sed 's/to-chk=//' | cut -d/ -f1)
            remain_t=$(echo "$RSYNC_REMAIN" | sed 's/to-chk=//' | cut -d/ -f2)
            cur_done=$((remain_t - remain_n))
            total_done=$((all_done_f + cur_done))
            L "  ${B}FILES${R}      ${WH}${total_done}${R} ${D}synced total${R}  │  ${D}job:${R} ${WH}${cur_done}${R}${D}/${R}${WH}${remain_t}${R}  │  ${YL}${remain_n}${R} ${D}left${R}"
        elif [ "$all_done_f" -gt 0 ]; then
            L "  ${B}FILES${R}      ${WH}${all_done_f}${R} ${D}synced total${R}  │  ${D}current job scanning...${R}"
        else
            L "  ${B}FILES${R}      ${D}scanning...${R}"
        fi
    else
        E
        L "  ${B}STATUS${R}     ${GR}● IDLE${R}      ${D}no sync in progress${R}"
        E
        L "  ${B}PROGRESS${R}   [$(bar 100)]  ${GR}100%${R}   ${D}(all done)${R}"
    fi

    E; mid

    # ── Repository Table ──
    E
    L "  ${B}$(printf '   %-20s  %9s  %8s   %s' 'REPOSITORY' 'SIZE' 'FILES' 'TAG')${R}"
    L "  ${D}${SLINE}${R}"

    LS=""; [ -L "$REPO/latest-stable" ] && LS=$(readlink "$REPO/latest-stable")
    PV=""
    [ -f "$STATE" ] && PV=$(grep "^PREV=" "$STATE" 2>/dev/null | cut -d= -f2)
    [ -z "$PV" ] || [ "$PV" = "none" ] && \
        PV=$(grep "Previous stable:" "$LOG" 2>/dev/null | tail -1 | awk '{print $NF}')

    TF=0
    for d in "$REPO"/edge "$REPO"/v*; do
        [ -d "$d" ] || continue
        N=$(basename "$d")
        SZ=$(du -sh "$d" 2>/dev/null | awk '{print $1}')
        FC=$(find "$d" -type f 2>/dev/null | wc -l | tr -d ' ')
        TF=$((TF + FC))

        case "$N" in
            edge)    TAG="${MG}edge${R}"   ;;
            "$LS")   TAG="${GR}latest${R}" ;;
            "$PV")   TAG="${YL}prev${R}"   ;;
            *)       TAG="${D}---${R}"     ;;
        esac

        IC="${GR}✓${R}"
        if [ -n "$PID" ] && [ -f "$PFILE" ]; then
            CUR_JOB=$(awk '{print $3}' "$PFILE" 2>/dev/null)
            case "$CUR_JOB" in
                ${N}/*) IC="${YL}⟳${R}" ;;
                *)
                    DC=$(grep '\[OK\]' "$LOG" 2>/dev/null | grep "${N}/" | sort -u | wc -l | tr -d ' ')
                    [ "$DC" -eq 0 ] && IC="${D}·${R}"
                    ;;
            esac
        fi

        L "  $(printf '%b  %-20s  %9s  %8s   %b' "$IC" "$N" "$SZ" "$FC" "$TAG")"
    done

    TS=$(du -sh "$REPO" 2>/dev/null | awk '{print $1}')
    L "  ${D}${SLINE}${R}"
    L "  ${B}$(printf '   %-20s  %9s  %8s' 'TOTAL' "$TS" "$TF")${R}"
    E; mid

    # ── System ──
    DSZ=$(df -h "$REPO" | awk 'NR==2{print $2}')
    DUS=$(df -h "$REPO" | awk 'NR==2{print $3}')
    DAV=$(df -h "$REPO" | awk 'NR==2{print $4}')
    DPC=$(df "$REPO"    | awk 'NR==2{gsub(/%/,"");print $5}'); [ -z "$DPC" ] && DPC=0
    MU=$(free -m | awk 'NR==2{print $3}')
    MT=$(free -m | awk 'NR==2{print $2}')
    MP=$((MU * 100 / MT))
    LD=$(awk '{printf "%s  %s  %s",$1,$2,$3}' /proc/loadavg)

    E
    L "  ${B}DISK${R}    [$(bar $DPC)]  $(printf '%3d' $DPC)${B}%${R}    ${D}${DUS} / ${DSZ}  •  ${DAV} free${R}"
    L "  ${B}MEM${R}     [$(bar $MP)]  $(printf '%3d' $MP)${B}%${R}    ${D}${MU}M / ${MT}M${R}"
    L "  ${B}LOAD${R}    ${WH}${LD}${R}"
    E; mid

    # ── Services ──
    HC=$(http_conns)
    HT=$(http_total)
    E
    if pgrep nginx >/dev/null 2>&1; then
        L "  ${B}NGINX${R}   ${GR}● UP${R}     ${D}:80${R}   │  ${WH}${HC}${R} ${D}active${R}  •  ${WH}${HT}${R} ${D}total reqs${R}"
    else
        L "  ${B}NGINX${R}   ${RD}● DOWN${R}"
    fi
    pgrep crond >/dev/null 2>&1 \
        && L "  ${B}CRON${R}    ${GR}● UP${R}     ${D}sync daily 02:00 KST${R}" \
        || L "  ${B}CRON${R}    ${RD}● DOWN${R}"
    L "  ${B}SSH${R}     ${GR}● UP${R}     ${D}:22${R}"

    if [ -f "$STATE" ]; then
        LV=$(grep "^LATEST" "$STATE" 2>/dev/null | cut -d= -f2)
        PP=$(grep "^PREV"   "$STATE" 2>/dev/null | cut -d= -f2)
        LS2=$(grep "LAST_SYNC" "$STATE" 2>/dev/null | cut -d= -f2)
        E
        L "  ${B}MIRROR${R}  ${WH}${LV}${R} ${D}(latest)${R}  +  ${WH}${PP}${R} ${D}(prev)${R}  +  ${WH}edge${R}"
        L "  ${B}SYNCED${R}  ${D}${LS2}${R}"
    fi

    E; mid
    L " ${D}$(date '+%Y-%m-%d %H:%M:%S %Z')  │  refresh 3s  │  Ctrl+C to exit${R}"
    ftr

    sleep 3
done

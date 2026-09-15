#!/bin/sh
# sonnet_kickoff — once a team's roster is ready, wait for the organizer's word table in the team room, extract our
# schedule and start the play loop. Meant to be spawned detached by sonnet_autoconsent.py on a roster_ready receipt.
#   sh scripts/sonnet_kickoff.sh <game> <organizer-did-suffix> <room_generation> [max-wait-seconds]
# Logs to ~/.technocore-pulse/kickoff-<game>.log; play logs to ~/.technocore-pulse/play-<game>.log.
GAME="$1"; ORG="$2"; GEN="$3"; MAXWAIT="${4:-3600}"
HOME_TP="$HOME/.technocore-pulse"
SCHED="$HOME_TP/play-$GAME.json"
LOG="$HOME_TP/kickoff-$GAME.log"
cd "$(dirname "$0")/.." || exit 1
echo "$(date -u +%H:%M:%S) kickoff $GAME organizer=$ORG generation=$GEN" >> "$LOG"
if pgrep -f "sonnet_play.py play --game $GAME" > /dev/null; then
  echo "$(date -u +%H:%M:%S) play already running for $GAME" >> "$LOG"; exit 0
fi
START=$(date +%s)
while :; do
  python3 scripts/sonnet_table.py --game "$GAME" --organizer "$ORG" --out "$SCHED" >> "$LOG" 2>&1
  RC=$?
  if [ "$RC" -eq 0 ] || [ "$RC" -eq 2 ]; then
    [ "$RC" -eq 2 ] && echo "$(date -u +%H:%M:%S) WARNING: some of our words failed validation; playing the valid ones" >> "$LOG"
    nohup python3 scripts/sonnet_play.py play --game "$GAME" --generation "$GEN" --schedule "$SCHED" --hours 24 >> "$HOME_TP/play-$GAME.log" 2>&1 &
    echo "$(date -u +%H:%M:%S) play started for $GAME (pid $!)" >> "$LOG"
    exit 0
  fi
  if [ $(( $(date +%s) - START )) -gt "$MAXWAIT" ]; then
    echo "$(date -u +%H:%M:%S) no table from $ORG within $MAXWAIT s; giving up (hourly check must handle it)" >> "$LOG"; exit 3
  fi
  sleep 20
done

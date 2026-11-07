#!/bin/sh
# Launch reView: stream the reMarkable framebuffer straight into the viewer.
# Runs restream WITHOUT -c (reView draws its own pen cursor).

remarkable="${REMARKABLE_IP:-10.11.99.1}"

# Ask framebuffer-spy for address
fb_skip_offset_from_xovi() {
    offset=$(ssh root@"$remarkable" 'sh -s' <<"REMOTE"
if ! [ -p /run/xovi-mb ] || ! [ -p /run/xovi-mb-out ]; then
    echo "framebuffer-spy: xovi-message-broker pipes not present" >&2
    exit 1
fi
# Bound the exchange
tmp=$(mktemp)
(echo ">eframebuffer-spy\$getConfigString:" > /run/xovi-mb && exec cat /run/xovi-mb-out) > "$tmp" &
job=$!
i=0
while kill -0 "$job" 2>/dev/null && [ "$i" -lt 30 ]; do
    sleep 0.1
    i=$((i + 1))
done
kill "$job" 2>/dev/null
cfg=$(cat "$tmp")
rm -f "$tmp"
fb=${cfg%%,*}
case "$fb" in
    0x*) ;;
    *) echo "framebuffer-spy: no framebuffer address yet (got '${cfg:-<empty>}'); draw on the screen once and retry" >&2; exit 1 ;;
esac
pid=$(pidof xochitl)
if [ -z "$pid" ]; then
    echo "framebuffer-spy: xochitl not running" >&2
    exit 1
fi

base=$(awk -F- '/\/dev\/fb0$/{getline; print $1; exit}' "/proc/$pid/maps")
if [ -z "$base" ]; then
    echo "framebuffer-spy: no /dev/fb0 mapping in xochitl" >&2
    exit 1
fi
echo $(( fb - 0x$base ))
REMOTE
    )
    case "$offset" in
        '' | *[!0-9]*) return 1 ;;
        *) echo "$offset" ;;
    esac
}

# Prefer the exact offset reported by framebuffer-spy; fall back to the
# devault value if the xovi extension (or its message broker) isn't available.
skip_offset=$(fb_skip_offset_from_xovi)
if [ -n "$skip_offset" ]; then
    echo "Using framebuffer-spy skip_offset=$skip_offset."
else
    skip_offset=2511448
    echo "framebuffer-spy unavailable; using default skip_offset=$skip_offset."
fi

# resolve reView.py next to this script, regardless of where it's run from
dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# lz4 always reports "Error 68 : Unfinished stream" when restream is torn down
# mid-frame on quit (the stream has no end marker), so drop its stderr.
ssh root@"$remarkable" "~/restream -h 1872 -w 1404 -b 4 -f :mem: -s $skip_offset" 2>/dev/null \
    | lz4 -d 2>/dev/null \
    | python3.12 "$dir/reView.py" "$@"

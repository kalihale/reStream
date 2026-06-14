#!/bin/sh
# Launch reView: stream the reMarkable framebuffer straight into the viewer.
# Run restream WITHOUT -c (reView draws its own pen cursor).

remarkable="${REMARKABLE_IP:-10.11.99.1}"
skip_offset=2511448

# resolve reView.py next to this script, regardless of where it's run from
dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# lz4 always reports "Error 68 : Unfinished stream" when restream is torn down
# mid-frame on quit (the stream has no end marker), so drop its stderr.
ssh root@"$remarkable" "~/restream -h 1872 -w 1404 -b 4 -f :mem: -s $skip_offset" 2>/dev/null \
    | lz4 -d 2>/dev/null \
    | python3.12 "$dir/reView.py" "$@"

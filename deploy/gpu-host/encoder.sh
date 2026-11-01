#!/bin/sh
# The boxed variant (spec §4.2): the worker's annotated MJPEG, encoded once on the L4 (h264_nvenc) and published to
# MediaMTX as seahive_boxes. ffmpeg ends when its input does (camera gap, worker restart); the loop starts it again,
# never faster than every 5 s.
#
# Timing. The MJPEG demuxer stamps frames at a nominal 25 fps whatever their arrival rate, and through decode and
# encode that nominal clock wins (-use_wallclock_as_timestamps only survives a stream copy; bring-up 2026-10-31:
# the worker's ~15 fps came out as 25 fps of stream time, 0.63x real time, 2.4 s segments). So the filter graph
# stamps every frame from the wall clock and the output is a constant ENCODER_FPS (duplicating or dropping the odd
# frame), with one IDR per second of stream time (-g = fps, -forced-idr so the forced keyframes are real IDRs), so
# MediaMTX can cut segments of any whole number of seconds (hlsSegmentDuration in mediamtx.yml, the cost knob) in
# real time, regardless of the inference rate. No B-frames and the low-latency tune keep the glass-to-glass delay
# to the segmenting itself.
set -u
WORKER="${WORKER_URL:-http://worker:8081}"
MTX="${MEDIAMTX_RTSP:-rtsp://mediamtx:8554}"
FPS="${ENCODER_FPS:-15}"
while :; do
  ffmpeg -hide_banner -loglevel warning -nostdin \
    -rw_timeout 15000000 -i "$WORKER/stream.mjpeg" \
    -an -vf "settb=AVTB,setpts='(RTCTIME-RTCSTART)/(TB*1000000)'" -r "$FPS" -fps_mode cfr \
    -c:v h264_nvenc -preset p4 -tune ll -profile:v high -pix_fmt yuv420p \
    -rc vbr -b:v "${ENCODER_BITRATE:-3000k}" -maxrate "${ENCODER_MAXRATE:-4000k}" -bufsize "${ENCODER_BUFSIZE:-8000k}" \
    -bf 0 -g "$FPS" -forced-idr 1 -force_key_frames 'expr:gte(t,n_forced*1)' \
    -f rtsp -rtsp_transport tcp "$MTX/seahive_boxes" || true
  sleep 5                                     # before ENCODER_ONCE is honoured, so the tests can see the pause
  [ "${ENCODER_ONCE:-0}" = "1" ] && exit 0
done

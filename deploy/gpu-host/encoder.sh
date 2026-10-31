#!/bin/sh
# The boxed variant (spec §4.2): the worker's annotated MJPEG, encoded once on the L4 (h264_nvenc) and published to
# MediaMTX as seahive_boxes. ffmpeg ends when its input does (camera gap, worker restart); the loop starts it again,
# never faster than every 5 s. One keyframe per second of stream time, so HLS segments are one second regardless of
# the inference rate; no B-frames and the low-latency tune keep the glass-to-glass delay to the segmenting itself.
set -u
WORKER="${WORKER_URL:-http://worker:8081}"
MTX="${MEDIAMTX_RTSP:-rtsp://mediamtx:8554}"
while :; do
  ffmpeg -hide_banner -loglevel warning -nostdin \
    -rw_timeout 15000000 -use_wallclock_as_timestamps 1 -i "$WORKER/stream.mjpeg" \
    -an -c:v h264_nvenc -preset p4 -tune ll -profile:v high -pix_fmt yuv420p \
    -rc vbr -b:v "${ENCODER_BITRATE:-3000k}" -maxrate "${ENCODER_MAXRATE:-4000k}" -bufsize "${ENCODER_BUFSIZE:-8000k}" \
    -bf 0 -g 60 -force_key_frames 'expr:gte(t,n_forced*1)' -fps_mode passthrough \
    -f rtsp -rtsp_transport tcp "$MTX/seahive_boxes" || true
  sleep 5                                     # before ENCODER_ONCE is honoured, so the tests can see the pause
  [ "${ENCODER_ONCE:-0}" = "1" ] && exit 0
done

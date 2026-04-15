#!/usr/bin/env bash
# Pull the fish-relevant videos from Wahoo Bay's YouTube channel at 720p max.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p data/test_videos

IDS=(
  _Wj3giJzbEQ   # Sergeant Major nest (17 min)
  BfQn1gxIK1U   # Green sea turtle
  0AHOf-qRq5M   # Ciliated false squilla mantis shrimp
  2Cd1TtzVvNM   # Sergeant Major nest (short)
  HVoi-dFXgCU   # Black grouper on SEAHIVE
  j8KURblXv68   # Florida horse conch
  xiqEcWLbmhw   # Cannonball jelly
  VnoENYlWWvM   # Green moray eel
  IF3uX7oED94   # Resident sea turtle
  gic_xYIYcos   # SEAHIVEs close-up
  4TZvXrKqcO4   # Crab cleaning
  FjszPu9Mq_E   # Spotted eagle ray
  wZokelXDhf4   # Manatee
  Dc3raEF8EzM   # Greater soapfish
  8a0lMMVuBt0   # Sting ray
  KvMLusZjl8Y   # Nurse shark
  yQSjtjJAe8c   # Up Close and Personal
  t_T37ieSVb8   # So Many Fish at Wahoo Bay
)

URLS=$(mktemp)
trap 'rm -f "$URLS"' EXIT
for id in "${IDS[@]}"; do
  echo "https://www.youtube.com/watch?v=$id"
done > "$URLS"

cd data/test_videos
yt-dlp -a "$URLS" \
  -f 'bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]' \
  --merge-output-format mp4 \
  -o '%(id)s_%(title)s.%(ext)s' \
  --restrict-filenames \
  --write-info-json \
  --no-overwrites

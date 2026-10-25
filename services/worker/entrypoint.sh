#!/bin/sh
# Fetch the model weights the worker needs (MODELS_URL -> MODELS_DIR, once; a
# bind-mounted data/models is left alone), then run whatever the image was told to.
set -e
python /app/fetch_models.py
exec "$@"

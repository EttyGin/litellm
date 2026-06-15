# Derived image that bakes the max_tokens passthrough patch into LiteLLM.
# Build:  docker build -t <your-registry>/litellm-patched:v1.89.0-maxtokens .
# Push:   docker push <your-registry>/litellm-patched:v1.89.0-maxtokens
#
# Pin the base to the exact upstream tag you run; re-cut this image on upgrades.
FROM ghcr.io/berriai/litellm-database:v1.89.0

COPY sitecustomize.py /patch/sitecustomize.py
ENV PYTHONPATH=/patch

# entrypoint / cmd inherited from the base image (docker/prod_entrypoint.sh)

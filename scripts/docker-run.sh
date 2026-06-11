#!/usr/bin/env bash
# scripts/docker-run.sh — Launch N LOIC containers
set -euo pipefail

SCALE="${SCALE:-1}"
IMAGE="${IMAGE:-loic:v2}"
NETWORK_MODE="${NETWORK_MODE:-host}"

echo "Building image..."
docker build -t "$IMAGE" -f Dockerfile .

echo "Launching $SCALE container(s)..."

for i in $(seq 1 "$SCALE"); do
    docker run --rm -d \
        --network "$NETWORK_MODE" \
        --name "loic-${i}" \
        "$IMAGE" \
        "$@"
    echo "  Started: loic-${i}"
done

echo ""
echo "All $SCALE containers running. To watch:"
echo "  docker ps --filter name=loic"
echo "  docker logs -f loic-1"
echo ""
echo "To stop all:"
echo "  docker stop \$(docker ps -q --filter name=loic)"

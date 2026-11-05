#!/usr/bin/env bash
# Körs på servern av GitHub Actions: deploy/remote-deploy.sh <image-tag>
# Byter till en ny avbild och rullar tillbaka om /health inte blir grön.
# Docker-inloggningen mot GHCR görs av jobbet före och loggas ut efter.
set -euo pipefail

TAG="${1:?image-tag saknas}"
cd /srv/capital-radar
[ -f .env ] || { echo "Saknar /srv/capital-radar/.env (se deploy/README.md)"; exit 1; }

PREVIOUS="$(cat .deployed-tag 2>/dev/null || true)"

wait_healthy() {
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 4 http://127.0.0.1:8001/v2/health >/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# COMPOSE_PROFILES i .env styr om backup/public startas.
IMAGE_TAG="$TAG" docker compose pull
IMAGE_TAG="$TAG" docker compose up -d --remove-orphans

if wait_healthy; then
  echo "$TAG" > .deployed-tag
  docker image prune -f >/dev/null
  echo "Deploy klar: $TAG"
  exit 0
fi

echo "Hälsokontrollen misslyckades för $TAG"
docker compose logs --tail 80 radar || true
if [ -n "$PREVIOUS" ]; then
  echo "Rullar tillbaka till $PREVIOUS"
  IMAGE_TAG="$PREVIOUS" docker compose up -d --remove-orphans
  wait_healthy && echo "Återställd till $PREVIOUS"
fi
exit 1

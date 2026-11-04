#!/usr/bin/env bash
# Körs interaktivt på servern:
#   ssh -t deploy@<server> 'bash /srv/capital-radar/deploy/set-tunnel-token.sh'
# Frågar efter Cloudflare-tunnelns token (visas inte), sparar den i .env,
# slår på profilen "public" och startar cloudflared.
set -euo pipefail
cd /srv/capital-radar

read -rsp "Klistra in tunnel-token från Cloudflare och tryck Enter: " token || true
echo
token="$(printf '%s' "$token" | tr -d '[:space:]')"
# Cloudflare visar ibland hela kommandot; behåll bara token-delen.
token="${token##*--token}"
[ ${#token} -gt 50 ] || { echo "Det ser inte ut som en token. Inget ändrat."; exit 1; }

grep -v '^CLOUDFLARE_TUNNEL_TOKEN=' .env > .env.tmp
printf 'CLOUDFLARE_TUNNEL_TOKEN=%s\n' "$token" >> .env.tmp
if grep -q '^COMPOSE_PROFILES=' .env.tmp; then
  current="$(sed -n 's/^COMPOSE_PROFILES=//p' .env.tmp)"
  case ",$current," in
    *,public,*) ;;
    ,,) sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=public/' .env.tmp ;;
    *) sed -i "s/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=$current,public/" .env.tmp ;;
  esac
else
  echo 'COMPOSE_PROFILES=public' >> .env.tmp
fi
install -m 600 .env.tmp .env && rm -f .env.tmp

docker compose up -d
sleep 5
docker compose logs --tail 5 cloudflared

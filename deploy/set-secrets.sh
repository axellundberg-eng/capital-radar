#!/usr/bin/env bash
# Körs på servern med en fil på stdin:
#   ssh deploy@<server> 'bash /srv/capital-radar/deploy/set-secrets.sh' < hemligheter.env
# Läser NYCKEL=värde-rader för tillåtna nycklar och skriver in dem i .env
# (mode 600). Tomma värden och kommentarer hoppas över. Inga värden skrivs ut.
set -euo pipefail
cd /srv/capital-radar

ALLOWED="DEEPSEEK_API_KEY ANTHROPIC_API_KEY SCREENER_ADMIN_API_TOKEN"
cp .env .env.tmp
changed=""
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  case "$line" in ''|'#'*) continue ;; esac
  key="${line%%=*}"
  value="${line#*=}"
  value="$(printf '%s' "$value" | sed -e 's/^[[:space:]"]*//' -e 's/[[:space:]"]*$//')"
  case " $ALLOWED " in *" $key "*) ;; *) echo "Hoppar över okänd nyckel: $key"; continue ;; esac
  [ -n "$value" ] || continue
  grep -v "^$key=" .env.tmp > .env.tmp2 || true
  printf '%s=%s\n' "$key" "$value" >> .env.tmp2
  mv .env.tmp2 .env.tmp
  changed="$changed $key"
done
install -m 600 .env.tmp .env && rm -f .env.tmp
if [ -z "$changed" ]; then
  echo "Inga värden hittades. Inget ändrat."
  exit 1
fi
echo "Uppdaterade:$changed"
IMAGE_TAG="$(cat .deployed-tag 2>/dev/null || echo latest)" docker compose up -d app >/dev/null 2>&1
echo "Appen startad om med nya inställningar."

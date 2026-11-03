# Drift: Hetzner + Cloudflare

Målbild: en Hetzner-server kör `docker compose` med appen, Litestream-backup
och `cloudflared`. Servern tar bara emot SSH. Webben nås på
`https://radar.perplexinvest.se` via Cloudflare Tunnel och skyddas av
Cloudflare Access. GitHub Actions bygger avbilden och driftsätter vid push
till `main`.

Steg markerade **(du)** kräver inloggning eller betalning och görs av dig.

## 1. SSH-nycklar (lokalt)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/capital-radar -C din-dator
ssh-keygen -t ed25519 -f ~/.ssh/capital-radar-deploy -N "" -C github-actions-deploy
```

Klistra in båda `.pub`-nycklarna i `deploy/cloud-init.yml` i stället för
platshållarna (bara lokalt, committa inte nycklarna).

## 2. Server **(du)**

Hetzner Cloud Console → nytt projekt `capital-radar` → Add Server:

- Plats: Helsingfors eller Falkenstein. Avbild: Ubuntu 24.04.
- Typ: minsta delade x86-typen med 2 vCPU och 4 GB räcker (cirka 4–5 euro i månaden).
- Under Cloud config: klistra in den ifyllda `cloud-init.yml`.
- Backups: slå på (20 % av serverpriset, dagliga ögonblicksbilder).

Vänta ett par minuter och kontrollera:

```bash
ssh -i ~/.ssh/capital-radar deploy@<server-ip> 'docker version && sudo ufw status'
```

Skapa `/srv/capital-radar/.env` från `.env.example` (DeepSeek-nyckel,
`SCREENER_ADMIN_API_TOKEN`, övriga `SCREENER_*` från Railway).

## 3. GitHub **(du)**

Repo → Settings:

- Environments → `production` (lägg gärna till dig själv som required reviewer).
- Secrets and variables → Actions:
  - Secret `DEPLOY_SSH_KEY`: innehållet i `~/.ssh/capital-radar-deploy`.
  - Secret `DEPLOY_KNOWN_HOSTS`: utdata från `ssh-keyscan -t ed25519 <server-ip>`.
  - Variabel `DEPLOY_HOST`: serverns IP. Deploy-jobbet är avstängt tills den finns.

Nästa push till `main` bygger, testar och driftsätter. `deploy/remote-deploy.sh`
väntar på `/health` och rullar tillbaka till förra versionen om den inte blir grön.

Innan Cloudflare är klart: `ssh -L 8000:127.0.0.1:8000 deploy@<server-ip>` och
öppna `http://127.0.0.1:8000`.

## 4. Flytta databasen från Railway

1. **(du)** `railway login` och `railway link` (projektet "Copy", tjänsten för `capital-radar-v4`).
2. `bash deploy/railway-backup.sh --documents backups` tar en konsistent kopia,
   kontrollerar SHA-256 och `integrity_check` och skriver ut antal rader.
3. Kopiera till servern och återställ:

   ```bash
   scp -i ~/.ssh/capital-radar backups/screener-*.db backups/documents-*.tar.gz deploy@<server-ip>:/srv/capital-radar/backups/
   ssh -i ~/.ssh/capital-radar deploy@<server-ip> 'bash /srv/capital-radar/deploy/restore-db.sh /srv/capital-radar/backups/screener-<tid>.db /srv/capital-radar/backups/documents-<tid>.tar.gz'
   ```

   En befintlig databas på servern flyttas till `backups/replaced-*`, den skrivs aldrig över.

Railway rörs inte av detta. Båda körs parallellt tills den nya är verifierad.

## 5. Backup till Object Storage **(du)**

Hetzner Console → Object Storage → skapa bucket (privat) och S3-nycklar.
Fyll i `LITESTREAM_*` i `.env`, lägg till `backup` i `COMPOSE_PROFILES` och kör
`docker compose up -d`. Litestream replikerar databasen löpande och behåller
30 dagar. Dokumentkatalogen täcks av Hetzners serverbackup.

Provåterställning (bör göras en gång):

```bash
docker run --rm --env-file .env -v "$PWD/deploy/litestream.yml:/etc/litestream.yml:ro" -v /tmp:/restore \
  litestream/litestream:0.3.13 restore -o /restore/test.db /data/screener.db
sqlite3 /tmp/test.db 'PRAGMA integrity_check;'
```

## 6. Cloudflare Tunnel och Access **(du)**

Cloudflare Zero Trust (gratis upp till 50 användare):

1. Networks → Tunnels → Create tunnel (cloudflared) → `capital-radar`.
   Kopiera token till `CLOUDFLARE_TUNNEL_TOKEN` i `.env`.
2. Public hostname: `radar.perplexinvest.se` → Service `HTTP` → `app:8000`.
3. Access → Applications → Self-hosted → `radar.perplexinvest.se`.
   Policy "Allow" med din e-postadress (One-time PIN eller Google som inloggning).
4. Lägg till `public` i `COMPOSE_PROFILES` och kör `docker compose up -d`.

Kontrollera att `https://radar.perplexinvest.se` kräver inloggning i ett
privat fönster.

## 7. Byte och nedstängning

1. Kör parallellt några dagar. Jämför översikt och transaktionsflöde mot Railway.
2. Ta en sista backup enligt steg 4 precis före bytet och återställ den.
3. Först därefter tas Railway-tjänsterna bort, en i taget med bekräftelse.
4. Arkivera repot `Screener-analys-hus` på GitHub.

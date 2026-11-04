# Plan: från runway till rätt bolag att kontakta

Efter steg 4 vet vi runway från idag och om en emission redan är annonserad.
Nästa lager svarar på frågan: **vilka av bolagen är värda att kontakta för ett
garanti- eller finansieringsuppdrag, och i vilken ordning?**

Utgångspunkt (Bergs Securities): life science och tech, First North/small cap,
affärer 8–80 MSEK, garanti måste ordnas innan emissionen är offentlig.

## Steg 5: kvalificering (påbörjas)

För varje bolag med runway 3–9 månader och utan annonserad emission:

1. **Kapitalbehov:** burn × 12 månader minus nuvarande kassa, avrundat. Det är
   ungefär vad bolaget måste ta in för att klara ett år.
2. **Marknadsvärde:** från Nasdaqs publika API (via ISIN). NGM saknas där och
   visas som "okänt".
3. **Rimlighet:** kapitalbehov som andel av marknadsvärdet. Tumregel: högst
   cirka 25 %. Över det blir en emission svår att garantera.
4. **Storlek:** behovet ska ligga inom 8–80 MSEK.
5. **Historik:** tidigare emissioner (från steg 4:s händelser), som visar att
   bolaget brukar finansiera sig med eget kapital.
6. **Sektor:** life science och tech prioriteras.

Resultat: en **prospektpoäng** med förklaring per del, och en sorterad lista i
gränssnittet ("Prospekt"), där varje rad visar runway, behov, marknadsvärde,
andel och senaste emission.

## Steg 6: löpande drift

- MFN-bevakningen (gamla systemet samlar fortfarande in) importeras varje timme.
- Nya rapporter extraheras och nya pressmeddelanden läses automatiskt.
- Ändringar flaggas: bolag som går in i 3–9 månader, eller som annonserar en
  emission, syns som "nytt" i listan.

## Steg 7: arbetsflöde

- Status per bolag: ny, kontaktad, bevakas eller ej aktuell, med anteckning.
  Dolda bolag kommer tillbaka om läget ändras (ny rapport, ny emission).
- Export till CSV för vidare arbete.

## Ordning

Steg 5 först, eftersom det gör listan med 50 bolag till en prioriterad
kontaktlista. Steg 6 gör att listan håller sig aktuell utan manuella körningar.

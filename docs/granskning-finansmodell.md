# Granskning av finansmodellen: korrigeringar och berikning

Datum: 2026-11-06. Underlag: koden i `src/radar/` (runway, rounds, qualify) och produktionsdatan.
"Verifierat" betyder att felet bekräftats i koden eller mot rapporttext; övriga punkter är bedömningar.

## A. Korrigeringar (fel i modellen)

1. **Valuta i prospektpoängen (verifierat i koden, 25 bolag).** Kapitalbehovet räknas i rapportens
   valuta, men andelen av marknadsvärdet och storlekskontrollen (8–80 MSEK) jämför det med
   marknadsvärdet i SEK utan omräkning. Gäller 25 bolag (16 i EUR, 4 USD, 3 DKK, 1 GBP, 1 CAD), bland
   annat FluoGuide, Initiator Pharma, Enersize och Nanoform. Ett behov på 3 MEUR blir "3 MSEK, för litet"
   och andelen blir ungefär elva gånger för låg.
   *Lösning:* räkna om behovet till SEK med Riksbankens dagskurs innan kontrollerna.

2. **Burn räknas på den längsta perioden (verifierat, 31 flaggade).** En engångsinbetalning tidigt i
   året gör förlustbolag "kassaflödespositiva": AcouSort (EU-finansiering) och Bioextrax
   (licensförskott) visade 1,8 respektive 1,1 månaders runway efter rättelse.
   *Lösning:* använd rullande tolv månader (LTM: helår + innevarande period − motsvarande period
   föregående år, alla finns i rapporternas jämförelsekolumner). Märk "senaste kvartalet bränner" när
   LTM är positiv men senaste kvartalet negativt.

3. **Leasingbetalningar saknas i burn (verifierat i koden).** Enligt IFRS 16 ligger amortering av
   leasingskuld i finansieringsflödet. Löpande kassaflöde minus investeringar underskattar därför den
   verkliga kassaförbrukningen för bolag med lokaler och utrustning på leasing.
   *Lösning:* extrahera "amortering av leasingskuld" och lägg till den i burn.

4. **Kapitalbehovet ignorerar skulder och kostnader.** Behovet är 12 månaders burn minus kassa.
   Förfallande skulder (4C Group: 92 MSEK räntebärande kortfristiga skulder), emissionskostnader och
   garantiarvoden ingår inte.
   *Lösning:* behov = burn × horisont (12–18 mån) + skulder som förfaller inom 12 mån + kostnader −
   kassa − outnyttjad kredit. Visa "behov inklusive refinansiering" separat.

5. **Emissionskostnad är en schablon på 10 %.** Verkliga kostnader är ofta 5–15 % plus garantiarvode
   (vanligen 8–14 % av garanterat belopp, kontant eller i aktier).
   *Lösning:* använd angivet nettobelopp när det finns; annars schablon per emissionstyp och
   garantiarvode från pressmeddelandet.

6. **Finansiering i annan valuta räknas inte alls (verifierat, 12 fall).** Exempel: Sivers
   (konvertibel i USD), Simris (teckningsoptioner i EUR), Safestate (lån i EUR).
   *Lösning:* samma valutaomräkning som i punkt 1, på händelsedatumet.

7. **Investeringar kan innehålla förvärv och placeringar.** När underlaget faller tillbaka på
   kassaflödet från investeringsverksamheten ingår förvärv, försäljningar och köp av placeringar.
   *Lösning:* använd bara balanserade utgifter och materiella investeringar; flagga när fallback används.

8. **Rörelsekapital ger kvartalssvängningar.** Säsongsbolag ser ut att bränna eller tjäna pengar beroende
   på kvartal.
   *Lösning:* visa burn före och efter förändring av rörelsekapital.

9. **Annonserad finansiering räknas som noll i behovet.** Rimligt för banden, men en
   företrädesemission med teckningsåtaganden och garantier är nästan säker.
   *Lösning:* pro forma-runway med säkrad andel (åtaganden + garantier) av annonserat belopp.

10. **Kreditlinan räknas i runway idag utan villkor.** Kreditlinor kan ha villkor (covenants,
    ägargarantier) eller vara uppsägningsbara.
    *Lösning:* fortsätt visa båda talen; märk kreditlinor med villkor när texten anger det.

11. **Saknad eller gammal data.** 62 bolag saknar runway (oftast balansräkning som bild) och 30 hade
    gammal rapport (åtgärdat genom klassning på bilagans filnamn; Bambuser saknar 2026-rapporter på MFN).
    *Lösning:* OCR för bildsidor; markera bolag vars källa saknar rapporter.

## B. Berikning (tillägg)

Ordnat efter nytta för garantimandat:

1. **Garantidata per emission:** garanter, arvode, utnyttjandegrad, teckningsgrad. Den gamla databasen
   har tabeller för det (`financing_event_guarantee_tranches`) som kan användas som karta.
2. **Finansiella rådgivare och emissionsinstitut:** påbörjat (fältet `advisers`, engångskörning pågår).
3. **Nästa rapportdatum:** finns i kalenderposterna som insamlingen i dag kastar. Emissioner
   annonseras ofta i samband med rapporten, så datumet är en tydlig tidssignal.
4. **Bemyndigande från årsstämman:** ett bemyndigande att emittera aktier är en förvarning om en riktad
   emission.
5. **Teckningsoptioner med lösenperiod och lösenpris** mot aktuell kurs: sannolik inbetalning och när.
6. **Emissionshistorik:** rabatt mot VWAP, utspädning, teckningsgrad, tid mellan emissioner.
7. **Ägarstruktur och insynshandel:** största ägare och insynspersonernas köp och sälj (FI:s
   insynsregister).
8. **Aktiens likviditet:** genomsnittlig daglig omsättning, kursutveckling 3 och 12 månader, blankning.
   Avgör hur stor emission som går att garantera.
9. **Kommande inbetalningar:** milstolpar, licensbetalningar och bidrag som nämns i rapporterna.

## C. Föreslagen ordning

1. Valutaomräkning (A1, A6): litet ingrepp, rättar 25 bolags poäng och 12 händelser.
2. LTM-burn och märkning av senaste kvartalet (A2, A8).
3. Leasing i burn och kapitalbehov inklusive skulder (A3, A4).
4. Rapportdatum och bemyndiganden (B3, B4): finns redan i insamlad data.
5. Garantidata och emissionshistorik (A5, B1, B6).

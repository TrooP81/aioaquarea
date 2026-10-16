# Gap-analys inför hemmabruk – 16 oktober 2026

## Bedömning för ditt hus

**Systemet kan redan samla användbar information, men är ännu inte redo att själv styra värmepumpen för verifierad besparing med hög komfort.** Nuvarande bygge förbjuder vanlig styrning. Det är en uttrycklig produktgräns, inte en inställning som ska slås av.

Din förtydligade målbild är **22–23 °C under komfortfönstret, med ekonomiskt motiverad förvärmning över 23 °C före fönstret**. Husets termiska tröghet ska användas för att flytta värmeproduktion till billigare perioder. Huset har radiatorer och Panasonic luft/vatten; elen kommer från Tibber och uppgiven nätavgift är 0,71 kr/kWh.

Appens aktuella komfortband **21–25 °C** och mål **21 °C utanför komforttiderna** är därför inte i sig ett konstaterat profilfel. Den tidigare tolkningen av 22–23 °C som ett fast dygnsband var felaktig och har rättats. Det som behöver verifieras är att systemet skiljer på tidsberoende komfortkrav, utrymme för förvärmning och yttersta temperaturgränser. Att spara värden i detta observationsbygge ändrar inte pumpens faktiska reglering.

Närmaste praktiska leverans bör vara **tillförlitlig övervakning, rätt komfortprofil, rätt kostnadsunderlag och små manuella förbättringar med uppföljning**. Automatisk styrning blir ett senare, separat steg. Det finns inget verifierat underlag för ett löfte om en viss procent eller ett visst antal kronor i besparing.

## Vad som testats och vilken build som avses

- Läsande UI-genomgång på `http://localhost:4444`: Home, Timeline, Under the hood och Settings/Optimizer. Ingen inställning sparades och inga knappar för pollning, kalibrering, omplanering eller pumpstyrning användes.
- Två läsande omgångar av 21 GET-anrop. Senaste sparade omgång: **13:33:50–13:34:32 svensk tid**. Resultat: **18 HTTP 200, två HTTP 500 och en HTTP 503**. HTTP 200 är ett tekniskt svar, inte bevis för att funktionen är redo för styrning.
- Docker-inspektion av den namngivna heatpump-stacken, begränsade API-felrader och SHA-256-jämförelse av 14 centrala backendfiler. Alla 14 matchade körande API-image och lokal källa; de övriga backendprocesserna använder samma image.
- Begränsade offlineprober av aktuell ren logik och en ORM-session i minnet. Dessa är reproduktioner av specifika fel, inte en full regressionssvit eller ett fysiskt pump-prov.
- Ingen källkod för produkten ändrad, ingen deploy, omstart, datanollställning, träning, extern larmleverans eller fysisk styrning utförd av denna granskning. Endast rapport och läsande evidensverktyg har lagts till.

| Identitet | Verifierat värde |
|---|---|
| UI/API-version | 0.15.0 |
| Körande web-image | `sha256:73a4ffa43bcbb12ffa930658d65b79f62a0eb6cd83b933066785cfe771912abb` |
| Körande backend-image | `sha256:6d88b732f2e576b0a09a600d171e616e89d6b221e0ab8d87d22c5820336f59c3` |
| Image-revision | `cf1200f108104c0c36a86601908cd29b21c0bff2+scope.805670f227fa2d85d929d7b3e9b6079a9d299e6f933fcc8632cb40b3e682cfff` |
| Image skapad | Backend 12:55:36, web 12:56:00 svensk tid 2026-10-16 |
| Runtime Compose | `.agent-runs/20261016-fresh-start-safety/operational-restart/restart-compose.yml` i repositoryroten |
| Lokal källrevision | Började vid `cf1200f`; en annan pågående aktivitet skapade `c5c7d9ab88efa002a5bbc5044607e9e8754d5d77` under granskningen. De 14 hashkontrollerade filerna var oförändrade vid slutkontrollen. Granskningen gjorde ingen commit. |

Image-etiketterna anger uttryckligen ett urval av ändrade filer, inte ren HEAD-ekvivalens. Webbfynden nedan är observerade i den serverade sidan; backendfyndens kärnfiler är dessutom hashmatchade. Övriga källreferenser är källanalys och ska inte läsas som fysiskt verifierad funktion.

## Aktuellt live-läge

| Område | Observation | Betydelse |
|---|---|---|
| Pump/data | Färsk status, tank 51 °C, mål 52 °C, pågående varmvattenvärmning | Datainsamling fungerar i stickprovet. |
| Innetemperatur | Fyra färska givare, vald referens 21,8 °C, confidence `high`; rum cirka 21,8–22,8 °C | Bra mätgrund. Stickprovet fastställer inte om komfortkraven under komfortfönstret uppfylls. |
| El/väder | Tibber/SEK och SMHI; cirka 10 återstående kompletta pristimmar | Data finns. Begränsad prishorisont är redovisad, inte i sig ett fel före nästa publicering. |
| Styrning | `build_policy=observe_only`, `ordinary_writes_allowed=false`, `control_ready=false` | Systemet levererar ingen vanlig automatisk besparing genom kommandon i detta bygge. |
| Modeller | Demand, comfort och thermal alla `unqualified`, inget committed artifact; noll utvärderade prognoser | Dagens nystart har ännu inte gett huskalibrerad prognosevidens. |
| Datanystart | Registrerad 13:04:37 svensk tid samma dag | Den korta historiken är väntad. Den är inte ett bevis för en trasig träningsprocess. |
| Plan | `/api/plans` ger `[]`; UI visar ingen aktiv plan | Diagnostik- och styrberedskap måste hållas isär. |
| Kostnad | Cirka 3,9 kWh idag, 0 % prissatt, hela mängden väntar på prisanknytning | Noll känt belopp betyder inte gratis drift. Före reset saknas användbar intervallhistorik för hela dagen. |
| Drift | `/api/health` 200 men `/api/health/ready` 503 med `stale: [backup]` | Databaskontakt bevisar inte komplett driftberedskap. |
| Resultat/råd | Resultatsammanfattning och värmekurveråd ger 500 | Centrala funktioner för manuell förbättring fungerar inte. |

## Prioriterade gap och acceptanskriterier

### G01 – Blockerar aktiv styrning: separat styrversion saknas

**Bevis:** live `/api/optimizer/status` anger att vanlig styrning är förbjuden och kräver ny granskad build. Planner tömmer vanliga `actions` och publicerar diagnostik (`packages/optimizer/main.py:586–613`); executor vägrar vanlig dispatch (`packages/optimizer/executor_core.py:910–912`). Inlärning avstängd eller lyckad modellträning ger inte styrbehörighet.

**Behövs:** ett snävt installationsspecifikt styrkontrakt: vilka åtgärder som får ske, vilka fysiska gränser som gäller, hur man pausar och hur pumpen återgår. Börja med en liten tillåten åtgärdsmängd. Hela ML-lagret behöver inte vara ett villkor för en framtida enkel regelpilot om dess säkerhet kan visas självständigt.

**Klart när:** separat godkänt bygge, verifierad kommandoväg och fysisk effekt på din pump, provad återgång och manuell överstyrning samt en övervakad pilot med dokumenterade stoppkriterier. Detta har inte testats i denna läsande granskning.

### G02 – Hög före automation: verifiera tidsberoende komfort och ekonomisk förvärmning

**Observation och krav:** live Settings visar min 21/max 25 °C. Prognosen använder mål 21 °C utanför komfortblock och 23 °C inom dem. Du vill använda husets tröghet: förvärmning över 23 °C före komfortfönstret är tillåten när den ger lägre total kostnad. Att appens band är bredare än 22–23 °C bevisar alltså inget fel. Ett konstant mål på 21 °C utanför fönstret får samtidigt inte hindra en lönsam förvärmning.

**Kompletterande källkontroll efter ditt förtydligande:** byggstenar för förvärmning och termisk lagring finns redan i `packages/optimizer/rule_mixins.py`; MILP har tidsberoende komfortmål (`milp.py:268–284`). Den representerade strategin använder däremot ett gemensamt rumstak för hela horisonten (`represented_strategy.py:117–120,175–177`), och thermal-battery-regeln avstår när aktuell temperatur redan ligger nära komfortmålet (`rule_mixins.py:642–656`). Därmed är det konkreta gapet separata tidsberoende temperaturgränser och verifierad ekonomisk användning av trögheten. Vanliga husvärmeåtgärder räknas dessutom som `unproven_actions` i publicerbar rules-diagnostik (`rules_engine.py:688–692`). Förvärmningskodens existens bevisar inte fungerande automatisk radiatorstyrning. Denna komplettering är källanalys; liveproverna ovan har inte körts om.

**Behövs:** separata begrepp för (1) komfortfönster med 22–23 °C i valda rum, (2) tillåtet temperaturutrymme för förvärmning och avsvalning utanför fönstret och (3) yttersta temperaturgränser som alltid gäller. Gränsen för tillåten förvärmning är ännu inte angiven av dig; 23 °C ska inte automatiskt användas som den gränsen. Stäm av appens registrerade kurva och värmestopp mot den verkliga Panasonic-panelen. Nu registreras 41 °C framledning vid +5 °C ute, 20 °C vid +15 °C, värmestopp +12 °C samt ΔT 4 °C; dessa värden är inte fysiskt verifierade här och är inte konstaterade fel.

**Klart när:** UI, planering och utfall använder samma tidsberoende krav. I ett testfall med billig el före ett dyrt komfortfönster ska planeraren kunna välja över 23 °C före fönstret och sedan avsvalning till 22–23 °C under fönstret, inom överenskomna yttergränser. När extra energianvändning, värmeförluster, pumpens effektivitet och nätkostnaden gör detta dyrare ska samma planerare avstå. Uppmätt husrespons ska stödja prognosen. Inga inställningar har ändrats nu.

### G03 – Hög: resultatöversikt och värmekurveråd kraschar

**Bevis LIVE:** `/api/outcomes/summary?days=7` och `/api/thermal/heat-curve-advice` gav HTTP 500 i båda omgångarna. UI visar att utfall inte kunde laddas. Containerloggarna visar `sqlalchemy.orm.exc.DetachedInstanceError` för `DeviceStatusRecord`.

**Rotorsak:** `get_session(read_only=True)` gör rollback och stänger sessionen (`packages/core/database.py:65–66`). ORM-fälten expirer. Resultatvägen använder objekten efter exit (`packages/core/outcome_experiments.py:139–167`, vidare `device_data_quality.py:164`); rådvägen läser `status.outdoor_temp` efter exit (`packages/api/routers/models_router.py:1008–1010`, även gate vid 1041). En begränsad SQLAlchemy-probe i minnet återskapade samma fel. Befintliga tester med `SimpleNamespace` simulerar inte rollback/expiry (`tests/test_review_repairs.py:560–601`).

**Klart när:** båda routes returnerar 200 med riktig färsk ORM-status och gate; saknade/gamla data ger begriplig otillräcklig-data-status. Alla behövliga fält materialiseras före sessionsexit eller beräknas inom den. Läsande rollback och säkerhetsspärrar ska behållas. Regressionstest ska använda en session som faktiskt expirer fält.

### G04 – Hög: backupflöde saknas och driftvyn missar det

**Bevis LIVE:** ingen backup-container körs; runtime Compose har bara api/poller/optimizer/web och återanvänder datatjänster. Readiness ger 503 på backup. Senaste färdiga arkiv i repositoryts vanliga backupkatalog är från 13 oktober; inget nyare hittades där. Huvudvyn skriver ändå att inga operativa varningar finns och att data/planering ser friska ut.

**Källa:** runtime Compose services vid rader 3, 54, 98, 142; `packages/core/operational_alerts.py:151–154` omfattar inte backup; `web/components/OperationalAlerts.tsx:58–59` etiketterar tom lista som frisk drift.

**Klart när:** ett schemalagt backupflöde skyddar exakt den aktiva databasen; senaste lyckade backup visas och dess ålder övervakas; en isolerad återläsning har verifierats. UI och readiness ska visa samma backupfel. Inget backupjobb eller restoreprov startades här.

### G05 – Hög för nyttig plandiagnostik: producent av nödvändigt lastunderlag saknas

**Bevis:** produktcollector saknar `diagnostic_assumptions` (`packages/core/device_status_snapshot.py:118–128`), medan representerad strategi kräver explicit timvis native lastproveniens och stödd tillskottslast (`packages/optimizer/represented_strategy.py:29–58,326–347`). Rules kan då returnera utan diagnostik. `docs/diagnostic-planning.md:40–54` säger uttryckligen att normala produktindata inte kan skapa tillåten representerad diagnostik; endast syntetiska/offline-deklarationer övar vägen. Live saknar planer.

**Klart när:** verkligt insamlade indata kan skapa en spårbar diagnostik inom ett tydligt deklarerat användningsområde, eller UI visar exakt varför det inte går och vad som ska samlas in. Man får inte hitta på last-/COP-/tillskottsvärden för att passera valideringen. Nuvarande representerade strategi avser varmvattenförskjutning med oförändrad inbyggd husvärme; en verifierad kommandopolicy för aktiv radiatorvärme är ett ytterligare arbete.

### G06 – Hög för pengar: kalkylen behöver din verkliga marginalkostnad

**Bevis:** Tibber `total` används som pris, ENTSO-E använder börspris; dessa har olika kostnadsomfattning (`packages/poller/feeds.py:225–227,283–290,365–369`). Målfunktionen räknar elenergi × pris (`packages/optimizer/represented_strategy.py:823–826`). Separat nät-/effekttariffmodell saknas i granskat underlag.

**För dig:** lägg till nätavgiften **0,71 kr/kWh**, om den ligger utanför Tibber-priset och på samma momsgrund. Bekräfta vad de 71 örena inkluderar, elprisets avräkningsintervall och eventuell effektavgift; nätägare och dessa detaljer är ännu okända. Undvik att dubbelräkna skatt/moms eller påslag som redan ingår.

Uttrycket ”rörligt elpris” fastställer inte ensamt om fakturan följer månad, timme eller kvart. Om avtalet är månadsrörligt kan en flytt till ett billigare spotintervall inte räknas som motsvarande besparing på elhandelsfakturan. Faktureringsmodellen måste därför bekräftas och styra kalkylen.

Samma antal flyttade kWh ger samma fasta nätkostnad. Om förvärmning ökar elanvändningen med 1 kWh tillkommer däremot 0,71 kr i uppgiven nätkostnad, utöver kostnaden för elen. Därför ska jämförelsen göras på total extra/undviken energi och kostnad, inte bara billigaste timmen. Om effektavgift finns behövs även hushållets totala import och tariffens faktiska regler.

**Klart när:** faktisk kostnad stämmer mot en fakturaperiod och optimeringen redovisar Tibberdel, nätkostnad, eventuell effektkostnad, extra förvärmningsenergi och netto. Fasta månadsavgifter kan redovisas separat eftersom de inte ändras av en flyttad kWh.

### G07 – Hög om ditt avtal är kvartbaserat: prisupplösningen är timvis

**Bevis:** ENTSO-E tidsvägs till timmar (`packages/poller/feeds.py:232–255`); Tibber-inmatningen avvisar tidsstämplar med minut ≠ 0 (`:372–373`). Live levereras timrader. Din exakta avtalsavräkning är inte verifierad.

**Klart när:** avtalets verkliga intervall är fastställt. För kvartavtal ska insamling, energianknytning, kalkyl och planering bevara kvartspriser, inklusive negativa priser, luckor och sommar-/vintertidsdygn. Alternativet är tydligt avgränsad timdiagnostik med redovisad begränsning.

### G08 – Hög för komfort: referens/median och komfortutfall skyddar inte nödvändigtvis alla viktiga rum

**Bevis:** kontrollunderlaget väljer referens eller median (`packages/core/control_temperature.py:213–239`), och komfortbedömningen använder `basis_temperature` (`comfort_assessment.py:47–52`). Ren probe med rum 18/21/22 °C, median 21, band 20–22 och prognos 21 gav `on_target` trots ett rum på 18 °C. Detta är ett syntetiskt exempel; dina rum låg inte på 18 °C i stickprovet.

Utfall hämtar alla icke stale temperaturrader utan samma sensorurval (`plan_outcome.py:388–399`) och räknar andel samples (`:237–256`), inte tid i komfortband. En probe med 99 varma samples och ett kallt gav 99 % inom band utan hänsyn till hur länge kylan varade.

**Klart när:** explicita prioriterade rum och samma sensorpolicy används i prognos och uppföljning. Visa tidsvägd täckning, minuter/gradtimmar utanför 22–23 under komfortfönstret, längsta avvikelse och saknad data. Redovisa avvikelser från alltid gällande yttergränser separat. Tillåten förvärmning över 23 °C före fönstret ska inte räknas som komfortfel. Med en gemensam radiatorframledning krävs även korrekt injustering/termostater för att flera rum ska hålla sin komfort.

### G09 – Hög för ett trovärdigt besparingsbesked: baseline och resultatmätning behöver utvecklas

**Bevis:** nuvarande beräknad prisförskjutningsbesparing jämför använd energi med prisernas enkla medel (`packages/core/plan_outcome.py:212–224`). Koden och API-texten redovisar redan korrekt att detta inte är bevis för kommandorsakad besparing. Samma 1 kWh à 1 ger måttet 0 för prislistan [1,1] och 4 för [1,9] trots identisk användning. Vädermatchning är observationell (`:262–335`).

**Klart när:** en sammanhängande baseline med pumpens vanliga reglering finns, följd av avgränsade jämförelser med likvärdig rumskomfort, väder och varmvattenbehov. Redovisa netto kronor inklusive nät, kWh, datatäckning, osäkerhet och eventuella komfortförluster. Dagens reset och 0 % kostnadstäckning kan inte användas som baslinje för en besparingsprocent.

### G10 – Blockerar obevakad aktiv styrning: återgång vid moln-/processfel är inte fysiskt verifierad

**Bevis:** force-DHW ON/OFF skickas via separata molnkommandon utan lokal sluttid (`packages/core/services/aquarea.py:1087–1105`); restore är en separat länkad åtgärd (`safety_reverts.py:245–255`). Återförsök finns men behöver senare fungerande kommunikation. Nuvarande bygge skapar inga nya vanliga ON-kommandon. Positiv observation håller också befintliga återställningar; learning mode är därför inte en allmän fysisk återställningsknapp.

**Klart när:** avbrott i internet/Panasonic, krasch och omstart har en dokumenterad och praktiskt provad återgång. Pumpens egen reglering och skydd ska fungera oberoende av appen. Använd pumpens lämpliga tidsbegränsade funktioner där de ger verifierad effekt. Dokumentera manuell reservrutin. Live visar noll öppna restoreobligationer och en tidigare administrativ pensionering med `physical_state_verified=false`; detta är inget bevis för fysisk OFF.

### G11 – Medel/hög före automation: modell- och installationsbevis saknas efter nystarten

**Bevis LIVE:** alla tre modeller är okvalificerade, inga utvärderade prognoser, standardhastigheter för uppvärmning/avsvalning. COP är antagen, inte uppmätt pumpverkningsgrad. Uteunderlaget var cirka 14–15 °C och ger inte bevis för vinterdrift.

**Klart när:** berörd modellroll har representativt installationsunderlag och uppmätt prognosfel mot enkel referens. Befintliga kvalifikationskrav omfattar bland annat 14 dagar, 14 device-day-kluster, 20 issue cohorts, 60 giltiga par och tre uteklimatbin (`packages/ml/forecast_quality.py:87–95`). Fjorton kalenderdagar ensamt räcker inte, och rapportkvalificering ger inte styrbehörighet. En regelpilot som inte förlitar sig på ML behöver i stället sitt eget styrke- och säkerhetsbevis.

### G12 – Medel: felaktig sensorvarning leder felsökningen fel

**Bevis LIVE:** prognosen har `current_indoor=21.8`, referens-confidence `high`, sensorålder cirka två minuter, men säger att färsk betrodd observation saknas. Samtidigt är `forecast_status=fallback` och orsaken `insufficient_scored_forecasts`.

**Rotorsak:** `packages/core/comfort_assessment.py:38–44` ger samma sensortext för alla prognosstatusar utom `available`. Live-estimat för den aktuella vägen skickar prognosstatusen till funktionen (`packages/api/routers/models_router.py:1486–1495`). API:s `control_allowed=false` maskeras dessutom av observationspolicyn och ska inte ensamt tolkas som sensorfel.

**Klart när:** UI skiljer på givarfel, otillräckligt modellunderlag, gammal prognos och spärrad styrbuild. En färsk betrodd sensor ska inte begäras på nytt när den redan finns.

### G13 – Medel: drift efter omstart och oberoende larm är inte säkrade

**Bevis LIVE:** Docker restart-policy är `no` för DB och Redis, `unless-stopped` för API. Larmwebhooken drivs av poller och kräver DB (`packages/poller/main.py:600–605`, `operational_alerts.py:454–487`). Vid totalt värd-/Docker-/DB-avbrott kan den egna larmkedjan tystna.

**Klart när:** hela beroendekedjan återkommer efter kontrollerad Docker-/värdomstart, och en extern heartbeat/övervakare kan meddela utebliven drift. Avbrottsprov ska planeras separat och inte göras genom att störa den aktuella pumpen under denna granskning.

### G14 – Medel: lyckad backup får inte förväxlas med ett färskt misslyckat försök

**Bevis:** readiness tappar backupens statusdetaljer och bedömer bara klockan (`packages/api/routers/models_router.py:1589–1602,1623–1625`), medan backupskriptet uppdaterar heartbeat även vid fel (`scripts/backup_db.sh:19–25,58–72`). Offlineprobe av aktuell readiness-funktion gav `ready` även för färska `failed`, `replica_failed` och `verification_failed`, med övriga beroenden friska.

**Klart när:** senaste försök och senaste lyckade backup är skilda; misslyckad verifiering och obligatorisk replika ger rätt varning. Detta är ett separat fel från att backupjobbet just nu inte körs.

### G15 – Medel: varmvatten har inte ett eget fullständigt hushållskontrakt

**Bevis:** ready-by härleds från första timmen i rumskomfortblock (`packages/core/settings_service.py:905–924`). Duschinställningen visas som aktiverad live, men vanlig boost är förbjuden i bygget (`packages/optimizer/shower_mode.py:94–105`).

**Klart när:** separata varmvattenbehov, duschtider, flera duschar/gäster, tankvolym och uppmätt återladdningstid finns. UI ska ange att inställningen är lagrad men boost inte utförs i observationsbygget. Pumpens befintliga hygien-/skyddsfunktioner måste kartläggas och bevaras; ingen temperaturändring eller hygienverifiering gjordes här.

### G16 – Lägre prioritet: återstående pris-/statuspresentation och alternativa kostnadsvägar

- Timeline visade **0,89 kr som aktuell prisserie**, medan Home och aktuell timrad visade cirka **1,15 kr/kWh**. `web/components/ExplanationTimeline.tsx:112` väljer sista prisraden i stället för aktuell tid. Visa rätt tidsintervall.
- UI visar `Unrecognized Status (Degraded)` och kan samtidigt kalla motorn `Optimized`. Mappa kända statusar och använd tydliga ord för diagnostik och datainsamling.
- `/api/stats` snittar priser utan full area-/valuta-/käll-/sluttidsavgränsning och ersätter noll med 0,10 (`packages/api/routers/dashboard.py:540–545`). Aktuellt UI hittades inte använda denna väg. Samordna faktisk kostnadsberäkning mellan API-vägar och bevara noll/negativa priser.
- Prisbaseline inkluderar en timme vid exakt fönsterslut (`packages/core/plan_outcome.py:403–404`). Använd konsekvent halvöppet `[start,end)` för intervallens bidrag till medelbaseline.
- Vid flera Tibberhem väljs första hem med priser (`packages/poller/feeds.py:276–296,324–347`). Ett valt hem ska bindas till rätt installation.
- Lokal åtkomst är bunden till 127.0.0.1. För framtida mobil-/LAN-åtkomst behövs en beslutad VPN-/inloggningsväg. Gatewayns interna API-token ger inte användarinloggning åt en exponerad dashboard (`web/app/api/[...path]/route.ts:34–35`). Nuvarande lokala bindning är inte konstaterad osäker exponering.

## Rekommenderad leveransordning

| Steg | Innehåll | Beslutspunkt |
|---|---|---|
| 1. Pålitlig mätning | G03, G04, G12–G14, rätt pris/status i G16; kostnadstäckning redovisas korrekt | Felfri resultat-/rådsväg, fungerande backup/restore, begripliga larm och sammanhängande mätdata. |
| 2. Ditt hus och din faktiska kostnad | G02, G06–G09, G15: 22–23 °C under komfortfönstret, separat förvärmningsutrymme, verklig pumpkurva, radiatorer, nät71 öre, prisintervall och varmvatten | En jämförbar baseline och överenskomna tidsberoende komfort- och kostnadsmått. |
| 3. Manuella förbättringar | Små, tidsatta värmekurvetester med oförändrade skydd och uppföljning av kWh, kronor och rumskomfort | En förbättring ska vara mätbar utan att ett viktigt rum eller varmvattnet blir för kallt. |
| 4. Användbar diagnostik | G05 och relevant del av G11: riktiga indata, tydligt användningsområde, uppmätt prognosfel | Planförslag går att förklara och jämföra med vanlig pumpreglering. |
| 5. Begränsad styrpilot | G01 och G10, separat granskad styrversion, manuellt stopp och provade felvägar | Ingen obevakad styrning förrän pilotens komfort-, drift- och besparingskriterier är uppfyllda. |

Leveransmålet för radiatorerna är kostnadsoptimering som använder husets tröghet och planerar både förvärmning och efterföljande avsvalning. En dokumenterad verklig värmekurva och uppmätt husrespons ger underlag för detta. Planeraren ska jämföra total kostnad för alternativa temperaturförlopp med samma uppfyllda komfortfönster. Det finns inte underlag här för att ordinera nya värmekurvetemperaturer eller lova att varje förvärmning blir billigare.

Föreslagna kriterier att avtala före en pilot: vilka rum och komfortfönster som ska hålla 22–23 °C, tillåtet förvärmnings- och avsvalningsutrymme utanför dem, alltid gällande yttergränser, hur lång avvikelse som tolereras, vilka varmvattenbehov som alltid ska klaras, högsta tillåtna ingripande, stopp vid gamla givare/prisluckor samt hur netto kronor och kWh jämförs med baseline. Dessa kriterier ska fastställas med dig; de är inga redan godkända driftinställningar.

## Externa kontrollkällor

Energimarknadsinspektionen beskriver att smart värmestyrning behöver ta hänsyn till det egna elavtalet och eventuell effektavgift; lägre spotkostnad kan annars motverkas av högre nätkostnad. Det stöder behovet av G06, men ersätter inte en kontroll av just ditt avtal. [Ei: Styr din uppvärmning och kyla](https://ei.se/konsument/anvand-el-smartare/styr-din-elanvandning/styr-din-uppvarmning-och-kyla).

Nord Pools information om införandet av 15-minuters day-ahead-intervall ger bakgrund till varför prisintervall måste kontrolleras. Den visar inte ensam vilket avräkningsintervall ditt Tibberavtal använder. [Nord Pool: 15-minute MTU](https://www.nordpoolgroup.com/en/message-center-container/newsroom/exchange-message-list/2025/q3/market-coupling-steering-committee-confirms-go-live-of--15-minute-mtu-in-sdac-on-trading-day-30-september-2025-for-delivery-day-1-october-2025/).

## Evidens och gränser

- [Live GET-svar och exakta svarssträngar](live-get-evidence.json)
- [Läsande probeskript](probe-live-readonly.ps1)
- [Runtime-manifest utan miljövariabler](runtime-manifest.json)
- [14 matchade source/runtime-hashar](source-runtime-hashes.json)
- [Avgränsade felrader från API](api-error-extract.txt)

Tolkning: LIVE betyder observerat i den nu körande sidan/API:t eller dess Docker-metadata/loggar. Källanalys och små offlineprober visar specifik logik, inte alla exekveringsvägar eller fysisk effekt. Full backend-/webbsvit, omstart, återläsning, molnavbrott, fysisk panelavläsning, exakt elavtal/faktura och faktiskt uppnådd besparing har inte verifierats i denna runda. Äldre granskningsresultat har inte räknats som aktuellt godkännande.

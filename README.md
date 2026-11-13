# Dieselprognos

Uppskattar hur Preems listpris på diesel (företagskort, kr/l inkl. moms) troligen rör sig de närmaste dagarna,
och skapar en rapport som går att skicka vidare till kunder.

Allt bygger på öppna data och kan återskapas av vem som helst med Python.

## Kom igång

Kräver Python 3.11 eller senare.

**Windows (PowerShell):**

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python prognos.py
```

**macOS / Linux:**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python prognos.py
```

Körningen tar ungefär en minut. Resultatet hamnar i mappen `output/`:

| Fil | Innehåll |
|---|---|
| `rapport.html` | Rapporten. Öppna i valfri webbläsare; fungerar utan internet och kan skrivas ut som PDF. |
| `prognos_vecka.csv` | Veckomodellens test mot historiken (verklig och förutsagd förändring per vecka). |

Testerna körs med `.venv\Scripts\python -m pytest` (Windows) eller `.venv/bin/python -m pytest`.

## Vad rapporten visar

- **Om 3 dagar:** väntad förändring av listpriset om tre dagar.
- **Snittpris kommande 7 dagar:** väntat snittpris nästa sju dagar jämfört med i dag.
- **Troligt spann:** bygger på modellens fel det senaste året. Testat mot historiken har ungefär 8 fall av 10
  hamnat inom spannet; den faktiska träffprocenten står i rapporten.
- **Historiskt:** hur ofta modellen haft rätt riktning och hur stort medelfelet varit, jämfört med att bara gissa
  "oförändrat pris".

## Så fungerar det

Preems pris följer världsmarknadens dieselpris och dollarkursen med några dagars fördröjning. Ungefär tre
fjärdedelar av en marknadsrörelse syns i Preems pris redan dagen efter, resten inom 3–5 dagar.

1. Hämtar Preems prishistorik (sedan 2008) och dagens listpris från Preems webbplats.
2. Hämtar dieselpriset i New York (EIA) och dollarkursen (Riksbanken) och räknar om till kr/l.
3. Två enkla linjära modeller lär sig hur Preems pris brukar följa marknaden:
   - **kort sikt:** marknadens rörelse dag för dag senaste veckan → Preems pris om 3 dagar,
   - **vecka:** marknadens rörelse senaste veckorna + hur långt Preems pris ligger från marknaden → snittpriset
     kommande 7 dagar.
4. Båda testas mot historiken: modellen tränas bara på data som fanns före varje testdag, precis som i verkligheten.

## Begränsningar – läs detta

- **Riktningen är osäker.** Modellerna slår "oförändrat pris" bara med några procent. Det beror på att Preem för
  vidare marknadsrörelser snabbt; vart priset tar vägen längre fram avgörs av vad oljemarknaden gör de kommande
  dagarna, och det går inte att förutsäga säkert med öppna data. **Spannet är den mest pålitliga delen.**
- **Listpris, inte ert pris.** Rapporten gäller Preems officiella listpris för företagskort. Bulkpriset och ert
  avtalspris ligger på en annan nivå men rör sig i stort sett på samma sätt. Ett fast påslag kan läggas till senare.
- **Skatteändringar** (t.ex. 1 januari och 1 juli vissa år) ger stora engångshopp som modellen inte förutser.
- **Europeiskt dieselpris** (ICE Gasoil) är inte fritt tillgängligt. Ett börshandlat alternativ testades men gav
  inte bättre resultat än New York-priset.
- Om Preem ändrar sin webbsida kan nedladdningen sluta fungera. Ladda då ner prishistoriken manuellt från
  [preem.se/foretag/listpriser](https://www.preem.se/foretag/listpriser/) och kör
  `python prognos.py --preem fil.xls`.

## Datakällor

- Preem: [listpriser och prishistorik](https://www.preem.se/foretag/listpriser/)
- U.S. Energy Information Administration: [New York Harbor ULSD, dagspris](https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm)
- Sveriges riksbank: [valutakurser, USD/SEK](https://www.riksbank.se/sv/statistik/rantor-och-valutakurser/)

Prognosen är en statistisk uppskattning, inte ett löfte om framtida pris.

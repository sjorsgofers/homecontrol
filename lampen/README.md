# Lampen — LSC Smart Connect per ruimte op de Nest Hub

Een aanraakpagina voor de Google Nest Hub: een plattegrond van de woning met per
kamer een lampknop, plus klok, datum, "Alle lampen uit" en (optioneel) het weer.
Via de kamer zelf kom je bij de losse lampen en de helderheid. De pagina staat
24/7 op de Hub.

```
apparaat thuis (lampen.py) ──serveert & cast──► Nest Hub
                                                  │ tik
                                                  ▼
                               Google Apps Script (apps-script.gs)
                                                  │
                                                  ▼
                          Tuya Cloud ──► LSC Smart Connect-lampen
```

LSC Smart Connect (Action) draait op Tuya. Het Apps Script praat met de Tuya Cloud
API, zodat het Tuya-geheim nooit in de pagina staat. `lampen.py` serveert de pagina
op je thuisnetwerk en cast hem naar de Hub; je hebt dus geen website nodig.

Bekijken zonder koppeling: open `index.html?demo=1` in een browser.

---

## 1. Tuya cloud-project (eenmalig, ±15 min)

1. Maak een gratis account op **https://platform.tuya.com**.
2. **Cloud → Development → Create Cloud Project**
   - Industry en Development Method: *Smart Home*
   - Data Center: **Central Europe**
3. Zet bij de API-services **IoT Core** en **Authorization Token Management** aan.
   Kies **Standard Instruction** als om een besturingsmodel wordt gevraagd.
4. Noteer op *Overview*: **Access ID** en **Access Secret**.
5. **Devices → Link App Account → Add App Account**: scan de QR-code met de
   **LSC Smart Connect**-app (Profiel → scan-icoon rechtsboven), kies *Automatic link*.
   - Lukt dat niet: zet de lampen in de **Smart Life**-app (LSC-lampen werken daarin)
     en scan de code met Smart Life.

De ruimtes uit de app worden automatisch overgenomen.

> De gratis IoT Core-proefperiode moet je af en toe verlengen (*Cloud → Cloud
> Services → IoT Core → Extend Trial Period*). Werkt het ineens niet meer, kijk daar eerst.

## 2. Google Apps Script (eenmalig, ±5 min)

1. **https://script.google.com** → *Nieuw project*.
2. Vervang `Code.gs` door de inhoud van **`apps-script.gs`**.
3. ⚙️ *Projectinstellingen → Scripteigenschappen*:

   | Eigenschap    | Waarde |
   |---------------|--------|
   | `TUYA_ID`     | Access ID |
   | `TUYA_SECRET` | Access Secret |
   | `TUYA_REGIO`  | `eu` |
   | `SLEUTEL`     | zelfgekozen wachtwoord |
   | `RUIMTES`     | *(optioneel)* eigen indeling: `{"Woonkamer":["Plafond","Staande lamp"],"Keuken":["Spots"]}` |

4. Kies de functie **`test`** → *Uitvoeren* → toestemming geven. Het logboek toont
   per ruimte de gevonden lampen.
5. *Implementeren → Nieuwe implementatie → Web-app*: uitvoeren als **Ik**,
   toegang **Iedereen**. Kopieer de **/exec-URL**.

## 3. Op de Nest Hub zetten

Op een apparaat dat altijd aan staat en op hetzelfde wifi zit als de Hub
(Raspberry Pi, NAS, mini-pc, laptop):

```bash
git clone https://github.com/sjorsgofers/homecontrol.git
cd homecontrol/lampen
pip3 install pychromecast
python3 lampen.py --zoek                                  # namen van je Hubs
python3 lampen.py --hub "Woonkamer scherm" --api "<EXEC-URL>" --key "<SLEUTEL>"
```

Eerst zonder koppeling proberen: `python3 lampen.py --hub "Woonkamer scherm" --demo`.

`lampen.py` zet de pagina terug zodra de Hub naar klok/fotolijst springt. Speelt er
muziek of video, dan laat het de Hub met rust. Opties: `--poort 8321` (standaard),
`--poll 120` (seconden tussen statusupdates; lager kost meer Tuya-aanroepen) en
`--lat 52.09 --lon 5.12` (toont de buitentemperatuur via open-meteo.com; zonder
deze twee blijft het weer weg).

Automatisch starten op een Raspberry Pi:

```bash
sudo tee /etc/systemd/system/lampen.service >/dev/null <<'UNIT'
[Unit]
Description=Lampenpagina op Nest Hub
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/homecontrol/lampen/lampen.py --hub "Woonkamer scherm" --api "<EXEC-URL>" --key "<SLEUTEL>"
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl enable --now lampen
journalctl -u lampen -f
```

Meerdere Hubs: één service per Hub, elk met een eigen `--poort`.

## Als webapp op een iPad of telefoon

De pagina werkt ook los van de Hub en van `lampen.py`, bijvoorbeeld op een iPad
aan de muur. Hij staat online via GitHub Pages:

```
https://sjorsgofers.github.io/homecontrol/lampen/#id=<IMPLEMENTATIE-ID>&key=<SLEUTEL>
```

- `id` is het deel van de /exec-URL tussen `/s/` en `/exec`. Optioneel erachter:
  `&lat=<breedtegraad>&lon=<lengtegraad>` voor het weer.
- Zet deze gegevens achter `#` en niet achter `?`: wat achter `#` staat wordt niet
  naar de webserver gestuurd. Zet de link met sleutel nooit in de repo.
- Open de link in Safari en kies *Deel → Zet op beginscherm*. De pagina opent dan
  schermvullend als app en onthoudt de koppeling.
- Scherm aan laten: *Instellingen → Beeldscherm en helderheid → Automatisch slot →
  Nooit*.

Zonder gegevens in de link toont de pagina voorbeeldlampen (demo).

## Bedienen

- **Tik op een kamer**: alles in die kamer aan/uit.
- **Vegen in een kamer**: de kamer is een helderheidsbalk. Naar links dimmen, naar rechts
  feller; in een smalle, staande kamer (de Hal) omlaag dimmen en omhoog feller.
- **Kamer lang indrukken**: paneel met helderheid van de hele ruimte en de losse lampen.
- **Alle lampen uit**: de tegel links.
- **Klok lang indrukken**: lampen/ruimtes opnieuw ophalen (na een nieuwe lamp).
- **Schakelaartje onder de partyknop**: licht of donker thema; de keuze wordt per apparaat onthouden.
- Ruimtes uit de app die niet op de plattegrond staan, verschijnen als knopje linksonder
  (zelfde bediening: tik = aan/uit, lang indrukken = paneel).

## Scènes (partyknop)

Linksonder, naast het weer, zit een klein knopje met een discobolletje. Eén tik start de
scène **Party**, nog een tik start de scène **Standaard**. Dat zijn gewone
*tik-om-te-starten*-scènes uit de app; de namen staan bovenin het script van
`index.html` in `SCENES` en moeten gelijk zijn aan de namen in de app. De functie
`test` in het Apps Script logt welke scènes er zijn. Hiervoor moet in het
Tuya-project ook de API-service *Smart Home Scene Linkage* aan staan.

## Plattegrond aanpassen

De plattegrond staat bovenin het script van `index.html` in `PLATTEGROND`: de
buitenmuur als lijst hoekpunten en per kamer de plek, de maat en waar naam,
lampknop en status staan (eenheden op een vlak van 660 × 652). De `naam` van een
kamer moet gelijk zijn aan de ruimtenaam in de app; heet de ruimte daar anders,
zet die naam dan in `ook`, bijvoorbeeld `ook:['Huiskamer']`.

## Problemen

| Wat je ziet | Oplossing |
|---|---|
| "Onjuiste sleutel" | `--key` wijkt af van `SLEUTEL` in het script. |
| "Tuya token: …" | Access ID/Secret of regio klopt niet. |
| "permission deny" / "No permissions" | API-service niet aan (stap 1.3) of proefperiode verlopen. |
| Lamp ontbreekt | Staat hij in *Devices* van het Tuya-project? Daarna de klok lang indrukken. |
| Ruimtes kloppen niet | Zet ze zelf in `RUIMTES` (namen zoals in de app). |
| Kamer op de plattegrond blijft grijs met "–" | De naam wijkt af van de ruimtenaam in de app: zie *Plattegrond aanpassen*. |
| Hub springt terug naar de klok | Normaal; binnen een minuut staat de pagina terug. |
| Aanraken werkt niet op de Hub | Verander in `lampen.py` `force=True` in `force=False`. |
| Hub toont nog een oude versie van de pagina | `lampen.py` opnieuw starten; het sluit de oude pagina eerst af en cast dan opnieuw. |
| Weten wat de Hub doet | De pagina meldt zich bij `lampen.py`: regels met `Scherm <ip>: geladen / aanraking werkt / geschakeld / fout` in de uitvoer. |
| Hub toont een lege pagina | Firewall op het apparaat? Poort 8321 moet open staan op het thuisnetwerk. |

## Bestanden

- `index.html` — de pagina (werkt ook op telefoon/tablet)
- `manifest.webmanifest`, `icoon-*.png` — maken van de pagina een webapp voor het beginscherm
- `apps-script.gs` — Apps Script dat de Tuya Cloud API aanroept
- `lampen.py` — serveert de pagina en houdt hem op de Nest Hub

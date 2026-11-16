#!/usr/bin/env python3
"""
Toegang tot de brug (brug.py): wie mag het huis bedienen?

- Leden loggen in het dashboard in met een account bij Supabase. Het dashboard stuurt het
  inlogbewijs mee; de brug laat Supabase dat controleren en kijkt in de tabel 'leden' of
  dit account erbij hoort (naam en rol). Een account dat niet in die tabel staat mag niets.
- Gasten (de oppas) krijgen een link met een eigen code en een einddatum. De codes staan
  alleen als controlegetal in gasten.json naast de config; na de einddatum of na intrekken
  werkt de link niet meer. Gasten kunnen alles bedienen, maar geen gasten beheren.

Het adres en de openbare sleutel van het Supabase-project staan in lampen-config.json
('supabase_url' en 'supabase_key'). Die sleutel is de openbare ('publishable') en mag
iedereen zien; hij staat ook in het dashboard.
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

ONTHOUD = 600        # zo lang (seconden) vertrouwen we een gecontroleerd inlogbewijs
ONTHOUD_NEE = 30     # ... en een afgewezen bewijs

_slot = threading.Lock()
_leden = {}          # controlegetal van het inlogbewijs -> (geldig tot, wie of None)


class Geweigerd(Exception):
    """De aanvrager mag er niet in; de tekst gaat naar het dashboard."""


def _hash(tekst):
    return hashlib.sha256(tekst.encode("utf-8")).hexdigest()


def _haal(url, sleutel, bewijs):
    req = urllib.request.Request(url, headers={"apikey": sleutel, "Authorization": "Bearer " + bewijs})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _verloopt(bewijs):
    """Het tijdstip waarop het inlogbewijs zelf verloopt (uit het bewijs gelezen)."""
    try:
        deel = bewijs.split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(deel + "=" * (-len(deel) % 4)))["exp"])
    except Exception:
        return time.time() + 60


def lid(cfg, bewijs):
    """Geeft {'naam', 'rol', 'soort': 'lid'} voor een geldig inlogbewijs van een lid."""
    basis, sleutel = (cfg.get("supabase_url") or "").rstrip("/"), cfg.get("supabase_key") or ""
    if not basis or not sleutel:
        raise Geweigerd("Inloggen is op de brug nog niet ingesteld")
    bewijs = str(bewijs)
    if len(bewijs) > 4000:
        raise Geweigerd("Niet ingelogd")
    h, nu = _hash(bewijs), time.time()
    with _slot:
        bekend = _leden.get(h)
    if bekend and bekend[0] > nu:
        if bekend[1] is None:
            raise Geweigerd(bekend[2])
        return bekend[1]
    try:
        gebruiker = _haal(basis + "/auth/v1/user", sleutel, bewijs)
        rijen = _haal(basis + "/rest/v1/leden?select=naam,rol&user_id=eq." + str(gebruiker["id"]), sleutel, bewijs)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _onthoud(h, nu + ONTHOUD_NEE, None, "Niet ingelogd")
            raise Geweigerd("Niet ingelogd")
        raise Geweigerd("De inlogdienst antwoordt niet (%s)" % e.code)
    except Exception:
        raise Geweigerd("De inlogdienst is niet bereikbaar")
    if not rijen:
        _onthoud(h, nu + ONTHOUD_NEE, None, "Dit account heeft nog geen toegang")
        raise Geweigerd("Dit account heeft nog geen toegang")
    wie = {"naam": rijen[0].get("naam") or "", "rol": rijen[0].get("rol") or "lid", "soort": "lid"}
    _onthoud(h, min(nu + ONTHOUD, _verloopt(bewijs)), wie, None)
    return wie


def _onthoud(h, tot, wie, reden):
    with _slot:
        if len(_leden) > 500:
            _leden.clear()
        _leden[h] = (tot, wie, reden)


# ------------------------------------ gasten ------------------------------------

def _pad(map_):
    return os.path.join(map_, "gasten.json")


def _lees(map_):
    try:
        with open(_pad(map_), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _schrijf(map_, lijst):
    tijdelijk = _pad(map_) + ".nieuw"
    with open(tijdelijk, "w", encoding="utf-8") as f:
        json.dump(lijst, f)
    os.replace(tijdelijk, _pad(map_))


def gast(map_, code):
    """Geeft {'naam', 'rol': 'gast', 'soort': 'gast', 'tot'} voor een geldige gastcode."""
    h, nu = _hash(str(code)), time.time()
    with _slot:
        lijst = _lees(map_)
    for g in lijst:
        if secrets.compare_digest(g.get("hash", ""), h):
            if g.get("tot", 0) < nu:
                raise Geweigerd("Deze gasttoegang is verlopen")
            return {"naam": g.get("naam") or "Gast", "rol": "gast", "soort": "gast", "tot": int(g["tot"] * 1000)}
    raise Geweigerd("Deze gasttoegang bestaat niet (meer)")


def gasten(map_):
    """Lijst voor het beheerscherm (zonder de codes); verlopen gasten ruimen we op."""
    nu = time.time()
    with _slot:
        lijst = _lees(map_)
        over = [g for g in lijst if g.get("tot", 0) > nu - 7 * 86400]
        if len(over) != len(lijst):
            _schrijf(map_, over)
    return [{"id": g["id"], "naam": g.get("naam"), "tot": int(g["tot"] * 1000), "door": g.get("door"),
             "verlopen": g.get("tot", 0) < nu} for g in over]


def gast_maak(map_, naam, uren, door):
    """Maakt een gast aan; geeft de code één keer terug (die staat nergens leesbaar opgeslagen)."""
    naam = (str(naam or "").strip() or "Gast")[:40]
    uren = max(1.0, min(float(uren or 24), 31 * 24))
    code = secrets.token_urlsafe(24)
    nieuw = {"id": secrets.token_hex(6), "naam": naam, "hash": _hash(code), "tot": time.time() + uren * 3600,
             "gemaakt": time.time(), "door": door}
    with _slot:
        lijst = _lees(map_)
        if len(lijst) >= 30:
            raise Geweigerd("Er zijn al te veel gasten; trek er eerst een paar in")
        lijst.append(nieuw)
        _schrijf(map_, lijst)
    return {"id": nieuw["id"], "naam": naam, "tot": int(nieuw["tot"] * 1000), "code": code}


def gast_weg(map_, gast_id):
    with _slot:
        lijst = _lees(map_)
        over = [g for g in lijst if g.get("id") != gast_id]
        _schrijf(map_, over)
    return len(over) != len(lijst)

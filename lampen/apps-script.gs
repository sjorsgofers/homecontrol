/* =====================================================================
   Lampen — Google Apps Script (Code.gs)
   Stuurt LSC Smart Connect-lampen aan via de Tuya Cloud API.
   (LSC Smart Connect van Action is een Tuya-merk.)

   Instellen (zie README.md voor de volledige stappen):
   Project-instellingen > Scripteigenschappen:
     TUYA_ID      Access ID / Client ID van je Tuya cloud-project
     TUYA_SECRET  Access Secret / Client Secret
     TUYA_REGIO   eu (standaard), us, cn of in
     SLEUTEL      zelfgekozen wachtwoord; de pagina moet dit meesturen
     KACHEL       (optioneel) naam of apparaat-ID van de kachel; leeg = zelf zoeken
     RUIMTES      (optioneel) eigen indeling als JSON, bijvoorbeeld
                  {"Woonkamer":["Plafond","Staande lamp"],"Keuken":["Spots"]}
                  Namen of apparaat-ID's. Leeg = de ruimtes uit de app.

   Daarna: Implementeren > Nieuwe implementatie > Web-app,
   uitvoeren als: ikzelf, toegang: iedereen. Kopieer de /exec-URL.
   Draai eerst één keer 'test' in de editor om toestemming te geven
   en te zien welke lampen en ruimtes gevonden worden.
   ===================================================================== */

var REGIO_HOSTS = {
  eu: 'https://openapi.tuyaeu.com',
  us: 'https://openapi.tuyaus.com',
  cn: 'https://openapi.tuyacn.com',
  in: 'https://openapi.tuyain.com'
};

/* Tuya-categorieën die we als 'lamp' tonen (lampen, strips, plafondlampen,
   dimmers, schakelaars en stekkers waar een lamp aan hangt). */
var LAMP_CATEGORIEEN = ['dj', 'dd', 'xdd', 'fwd', 'dc', 'fsd', 'tgq', 'tgkg', 'kg', 'cz', 'pc', 'tyndj', 'gyd', 'sxd'];
var AAN_CODES = ['switch_led', 'switch_1', 'switch'];
var HELDER_CODES = ['bright_value_v2', 'bright_value', 'bright_value_1'];
var TEMP_CODES = ['temp_value_v2', 'temp_value'];      /* warm (0) tot koud (1000) wit */
var KLEUR_CODES = ['colour_data_v2', 'colour_data'];   /* kleur als {h, s, v} */
/* De kachel: een Tuya-kachel (of een stekker met 'kachel' in de naam) die alleen aan/uit gaat. */
var KACHEL_CATEGORIEEN = ['qn', 'kt', 'rs', 'dr'];
var KACHEL_NAAM = /kachel|heater|eurom|verwarming/i;
var KACHEL_AAN_CODES = ['switch', 'switch_1', 'Power', 'power'];

/* ---------------------------- web-app ---------------------------- */

function doGet(e) { return antwoord_(verwerk_(e && e.parameter || {})); }

function doPost(e) {
  var p = {};
  try { p = JSON.parse(e.postData.contents || '{}'); } catch (err) {}
  return antwoord_(verwerk_(p));
}

function antwoord_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function verwerk_(p) {
  try {
    var sleutel = prop_('SLEUTEL');
    if (!sleutel || String(p.key || '') !== sleutel) return { ok: false, fout: 'Onjuiste sleutel' };

    if (p.action === 'set') {
      var ids = p.ids;
      if (typeof ids === 'string') ids = ids.split(',');
      var aan = p.on === undefined || p.on === '' ? null : (p.on === true || p.on === 'true' || p.on === '1');
      var helder = p.bright === undefined || p.bright === '' ? null : Number(p.bright);
      var temp = p.temp === undefined || p.temp === '' ? null : Number(p.temp);       /* 0 = warm, 100 = koud */
      var kleur = p.kleur === undefined || p.kleur === '' ? null : Number(p.kleur);   /* kleurtoon 0-360 */
      var res = schakel_(ids || [], aan, helder, temp, kleur);
      CacheService.getScriptCache().remove('status');
      return { ok: true, resultaat: res };
    }
    if (p.action === 'agenda') return { ok: true, afspraken: agenda_() };
    if (p.action === 'kachel') {
      var kAan = p.on === undefined || p.on === '' ? null : (p.on === true || p.on === 'true' || p.on === '1');
      return { ok: true, kachel: kachel_(kAan) };
    }
    if (p.action === 'lokaal') return { ok: true, apparaten: lokaal_() };
    if (p.action === 'brug') {
      PropertiesService.getScriptProperties().setProperty('BRUG', String(p.url || ''));
      PROPS_ = null;
      return { ok: true, brug: String(p.url || '') };
    }
    if (p.action === 'scene') return { ok: true, scene: startScene_(p.name) };
    if (p.action === 'scenes') return { ok: true, scenes: scenes_().map(function (s) { return s.name; }) };
    if (p.action === 'refresh') {
      CacheService.getScriptCache().removeAll(['apparaten', 'status', 'scenes', 'huizen', 'codes2', 'kachel']);
    }
    return { ok: true, ruimtes: stand_(), brug: prop_('BRUG') };
  } catch (err) {
    return { ok: false, fout: String(err && err.message || err) };
  }
}

/* Handig om in de editor te draaien: logt wat er gevonden wordt. */
function test() {
  CacheService.getScriptCache().removeAll(['apparaten', 'status', 'tuya_token', 'scenes', 'huizen', 'codes2']);
  var r = stand_();
  r.forEach(function (ruimte) {
    Logger.log('== ' + ruimte.name + ' ==');
    ruimte.lamps.forEach(function (l) {
      Logger.log('  ' + l.name + '  [' + l.id + ']  ' + (l.online ? 'online' : 'OFFLINE') +
        '  ' + (l.on ? 'aan' : 'uit') + (l.bright != null ? '  ' + l.bright + '%' : ''));
    });
  });
  try {
    Logger.log('== Scenes ==');
    scenes_().forEach(function (s) { Logger.log('  ' + s.name); });
  } catch (err) { Logger.log('  Scenes ophalen mislukt: ' + err.message); }
}

/* ------------------------ stand van zaken ------------------------ */

function stand_() {
  var app = apparaten_();
  var status = status_(app.lampen.map(function (l) { return l.id; }));

  var lampInfo = {};
  app.lampen.forEach(function (l) {
    var s = status[l.id] || {};
    var aanCode = eersteCode_(s, AAN_CODES);
    var helderCode = eersteCode_(s, HELDER_CODES);
    var tempCode = eersteCode_(s, TEMP_CODES);
    var kleurCode = eersteCode_(s, KLEUR_CODES);
    lampInfo[l.id] = {
      id: l.id,
      name: l.name,
      online: l.online,
      on: aanCode ? s[aanCode] === true : false,
      bright: helderCode ? naarProcent_(helderCode, s[helderCode]) : null,
      temp: tempCode ? Math.max(0, Math.min(100, Math.round(Number(s[tempCode]) / 10))) : null,
      rgb: !!kleurCode,
      tint: kleurCode && s.work_mode === 'colour' ? tint_(s[kleurCode]) : null
    };
  });

  return app.ruimtes.map(function (r) {
    return {
      id: r.id,
      name: r.name,
      lamps: r.ids.map(function (id) { return lampInfo[id]; }).filter(Boolean)
    };
  }).filter(function (r) { return r.lamps.length; });
}

/* Lampen + indeling in ruimtes; 10 minuten gecachet. */
function apparaten_() {
  var cache = CacheService.getScriptCache();
  var c = cache.get('apparaten');
  if (c) return JSON.parse(c);

  var lijst = alleApparaten_().filter(function (d) {
    return LAMP_CATEGORIEEN.indexOf(d.category) >= 0;
  });
  var lampen = lijst.map(function (d) { return { id: d.id, name: d.name, online: !!d.online, uid: d.uid }; });
  var opId = {};
  var opNaam = {};
  lampen.forEach(function (l) { opId[l.id] = l; opNaam[l.name.trim().toLowerCase()] = l; });

  var ruimtes = [];
  var eigen = prop_('RUIMTES');
  if (eigen) {
    var cfg = JSON.parse(eigen);
    Object.keys(cfg).forEach(function (naam, i) {
      var ids = cfg[naam].map(function (x) {
        var l = opId[x] || opNaam[String(x).trim().toLowerCase()];
        return l && l.id;
      }).filter(Boolean);
      ruimtes.push({ id: 'r' + i, name: naam, ids: ids });
    });
  } else {
    ruimtes = ruimtesUitApp_(lampen, opId);
  }

  /* Lampen die nergens in staan komen in 'Overig'. */
  var ingedeeld = {};
  ruimtes.forEach(function (r) { r.ids.forEach(function (id) { ingedeeld[id] = true; }); });
  var los = lampen.filter(function (l) { return !ingedeeld[l.id]; }).map(function (l) { return l.id; });
  if (los.length) ruimtes.push({ id: 'overig', name: ruimtes.length ? 'Overig' : 'Alle lampen', ids: los });

  var uit = { lampen: lampen, ruimtes: ruimtes };
  cache.put('apparaten', JSON.stringify(uit), 600);
  return uit;
}

/* Alle apparaten van de gekoppelde app-account(s). */
function alleApparaten_() {
  var alles = [];
  var rowKey = '';
  for (var i = 0; i < 20; i++) {
    var r = tuya_('GET', '/v1.0/iot-01/associated-users/devices', { size: 100, last_row_key: rowKey });
    (r.devices || []).forEach(function (d) { alles.push(d); });
    if (!r.has_more || !r.last_row_key) break;
    rowKey = r.last_row_key;
  }
  return alles;
}

/* Ruimtes zoals ingedeeld in de LSC Smart Connect / Smart Life-app. */
function ruimtesUitApp_(lampen, opId) {
  var ruimtes = [];
  var uids = {};
  lampen.forEach(function (l) { if (l.uid) uids[l.uid] = true; });
  Object.keys(uids).forEach(function (uid) {
    var huizen = [];
    try { huizen = tuya_('GET', '/v1.0/users/' + uid + '/homes') || []; } catch (e) { return; }
    huizen.forEach(function (h) {
      var info;
      try { info = tuya_('GET', '/v1.0/homes/' + h.home_id + '/rooms'); } catch (e) { return; }
      (info && info.rooms || []).forEach(function (room) {
        var ids = [];
        try {
          var devs = tuya_('GET', '/v1.0/homes/' + h.home_id + '/rooms/' + room.room_id + '/devices') || [];
          ids = devs.map(function (d) { return d.id; }).filter(function (id) { return opId[id]; });
        } catch (e) {}
        if (ids.length) ruimtes.push({ id: String(room.room_id), name: room.name, ids: ids });
      });
    });
  });
  return ruimtes;
}

/* Actuele status per lamp; 15 s gecachet zodat meerdere schermen niet dubbel vragen. */
function status_(ids) {
  var cache = CacheService.getScriptCache();
  var c = cache.get('status');
  if (c) return JSON.parse(c);
  var uit = {};
  for (var i = 0; i < ids.length; i += 20) {
    var deel = ids.slice(i, i + 20);
    var r = tuya_('GET', '/v1.0/iot-03/devices/status', { device_ids: deel.join(',') }) || [];
    r.forEach(function (d) {
      var s = {};
      (d.status || []).forEach(function (x) { s[x.code] = x.value; });
      uit[d.id] = s;
    });
  }
  cache.put('status', JSON.stringify(uit), 15);
  onthoudCodes_(uit);
  return uit;
}

/* Per lamp welke Tuya-codes hij gebruikt voor aan/uit en helderheid (6 uur gecachet),
   zodat schakelen niet eerst de status van alle lampen hoeft op te halen. */
function codes_() {
  try { return JSON.parse(CacheService.getScriptCache().get('codes2') || '{}'); } catch (e) { return {}; }
}
function onthoudCodes_(status) {
  var codes = codes_();
  Object.keys(status).forEach(function (id) {
    var s = status[id], hc = eersteCode_(s, HELDER_CODES), oud = codes[id] || {};
    var b = hc ? bereik_(hc, s[hc]) : null;
    if (b && oud.bereik && oud.bereik[1] > b[1]) b = oud.bereik;
    codes[id] = { aan: eersteCode_(s, AAN_CODES), helder: hc, bereik: b,
                  temp: eersteCode_(s, TEMP_CODES), kleur: eersteCode_(s, KLEUR_CODES) };
  });
  CacheService.getScriptCache().put('codes2', JSON.stringify(codes), 21600);
  return codes;
}

/* ---------------------------- schakelen ---------------------------- */

function schakel_(ids, aan, helderProcent, temp, kleur) {
  if (temp === undefined) temp = null;
  if (kleur === undefined) kleur = null;
  /* De codes per lamp komen uit de cache; alleen als er een ontbreekt halen we de status op.
     Dat scheelt bij elke tik een extra ronde naar Tuya. */
  var codes = codes_();
  if (ids.some(function (id) { return !codes[id]; })) {
    try { codes = onthoudCodes_(status_(apparaten_().lampen.map(function (l) { return l.id; }))); } catch (e) {}
  }
  var verzoeken = [];
  var volgorde = [];
  ids.forEach(function (id) {
    var c = codes[id] || {};
    var cmds = [];
    if (aan !== null) cmds.push({ code: c.aan || 'switch_led', value: aan });
    if (helderProcent !== null && !isNaN(helderProcent)) {
      if (c.helder) cmds.push({ code: c.helder, value: vanProcent_(c.bereik, helderProcent) });
      if (aan === null && helderProcent > 0) cmds.unshift({ code: c.aan || 'switch_led', value: true });
    }
    if (temp !== null && !isNaN(temp) && c.temp) {
      cmds.push({ code: 'work_mode', value: 'white' });
      cmds.push({ code: c.temp, value: Math.round(Math.max(0, Math.min(100, temp)) * 10) });
    } else if (kleur !== null && !isNaN(kleur) && c.kleur) {
      cmds.push({ code: 'work_mode', value: 'colour' });
      cmds.push({ code: c.kleur, value: { h: Math.round(kleur) % 360, s: 1000, v: 1000 } });
    }
    var aanCode = c.aan || 'switch_led';
    if (aan === null && cmds.length && !cmds.some(function (x) { return x.code === aanCode; })) {
      cmds.unshift({ code: aanCode, value: true });
    }
    if (!cmds.length) return;
    verzoeken.push(verzoek_('POST', '/v1.0/devices/' + id + '/commands', null, { commands: cmds }));
    volgorde.push(id);
  });
  if (!verzoeken.length) return [];
  var antwoorden = UrlFetchApp.fetchAll(verzoeken);
  return antwoorden.map(function (a, i) {
    var j = {};
    try { j = JSON.parse(a.getContentText()); } catch (e) {}
    return { id: volgorde[i], ok: !!j.success, fout: j.success ? undefined : (j.msg || a.getResponseCode()) };
  });
}

/* ----------------------------- scenes ----------------------------- */

/* Huizen (homes) van de gekoppelde app-account(s); 6 uur gecachet. */
function huizen_() {
  var cache = CacheService.getScriptCache();
  var c = cache.get('huizen');
  if (c) return JSON.parse(c);
  var uids = {};
  apparaten_().lampen.forEach(function (l) { if (l.uid) uids[l.uid] = true; });
  var ids = [];
  Object.keys(uids).forEach(function (uid) {
    (tuya_('GET', '/v1.0/users/' + uid + '/homes') || []).forEach(function (h) { ids.push(String(h.home_id)); });
  });
  cache.put('huizen', JSON.stringify(ids), 21600);
  return ids;
}

/* De tik-om-te-starten-scenes uit de app; 10 minuten gecachet. */
function scenes_() {
  var cache = CacheService.getScriptCache();
  var c = cache.get('scenes');
  if (c) return JSON.parse(c);
  var uit = [];
  huizen_().forEach(function (home) {
    (tuya_('GET', '/v1.1/homes/' + home + '/scenes') || []).forEach(function (s) {
      uit.push({ id: s.scene_id, name: s.name, home: home });
    });
  });
  cache.put('scenes', JSON.stringify(uit), 600);
  return uit;
}

/* Start een scene op naam (hoofdletters maken niet uit). */
function startScene_(naam) {
  var zoek = String(naam || '').trim().toLowerCase();
  function vind() {
    return scenes_().filter(function (s) { return String(s.name).trim().toLowerCase() === zoek; })[0];
  }
  var s = vind();
  if (!s) { CacheService.getScriptCache().remove('scenes'); s = vind(); }
  if (!s) {
    throw new Error('Scene "' + naam + '" bestaat niet. Wel: ' +
      (scenes_().map(function (x) { return x.name; }).join(', ') || 'geen'));
  }
  tuya_('POST', '/v1.0/homes/' + s.home + '/scenes/' + s.id + '/trigger');
  CacheService.getScriptCache().remove('status');
  return s.name;
}

/* ----------------------------- agenda ----------------------------- */

/* Afspraken van vandaag en morgen uit Google Agenda; 5 minuten gecachet.
   Scripteigenschap AGENDA = agenda-ID (meestal een e-mailadres); leeg = de eigen agenda.
   Een agenda van een ander account moet gedeeld zijn met het account van dit script.
   Het script heeft hiervoor alleen leesrechten nodig (zie oauthScopes in appsscript.json). */
function agenda_() {
  var cache = CacheService.getScriptCache();
  var c = cache.get('agenda');
  if (c) return JSON.parse(c);
  var id = prop_('AGENDA');
  var cal = id ? CalendarApp.getCalendarById(id) : CalendarApp.getDefaultCalendar();
  if (!cal) throw new Error('Agenda "' + id + '" is niet gevonden of niet gedeeld met dit account');
  var nu = new Date();
  var tot = new Date(nu.getFullYear(), nu.getMonth(), nu.getDate() + 2);
  var uit = cal.getEvents(nu, tot).slice(0, 12).map(function (e) {
    return {
      titel: e.getTitle(),
      start: e.getStartTime().toISOString(),
      eind: e.getEndTime().toISOString(),
      heleDag: e.isAllDayEvent()
    };
  });
  cache.put('agenda', JSON.stringify(uit), 300);
  return uit;
}

/* ----------------------------- kachel ----------------------------- */

/* Zoekt de kachel tussen alle apparaten van het account; 10 minuten gecachet. */
function kachelApparaat_() {
  var cache = CacheService.getScriptCache();
  var c = cache.get('kachel');
  if (c) return JSON.parse(c);
  var alles = alleApparaten_();
  var wens = String(prop_('KACHEL') || '').trim().toLowerCase();
  var k = null;
  if (wens) {
    k = alles.filter(function (d) { return d.id === wens || String(d.name).trim().toLowerCase() === wens; })[0];
  } else {
    k = alles.filter(function (d) { return KACHEL_NAAM.test(d.name || ''); })[0] ||
        alles.filter(function (d) { return KACHEL_CATEGORIEEN.indexOf(d.category) >= 0; })[0];
  }
  var uit = k ? { id: k.id } : {
    /* Niet gevonden: laat zien wat er wel is (alles wat geen lamp is), om de naam te kunnen kiezen */
    andere: alles.filter(function (d) { return LAMP_CATEGORIEEN.indexOf(d.category) < 0; })
                 .map(function (d) { return { name: d.name, category: d.category }; })
  };
  cache.put('kachel', JSON.stringify(uit), k ? 600 : 60);
  return uit;
}

/* Stand van de kachel; met aan = true/false wordt hij eerst geschakeld. */
function kachel_(aan) {
  var k = kachelApparaat_();
  if (!k.id) return { ok: false, fout: 'Geen kachel gevonden', andere: k.andere };
  var d = tuya_('GET', '/v1.0/devices/' + k.id);
  var s = {};
  (d.status || []).forEach(function (x) { s[x.code] = x.value; });
  var code = eersteCode_(s, KACHEL_AAN_CODES);
  if (!code) return { ok: false, fout: 'De kachel heeft geen aan/uit-schakelaar', codes: Object.keys(s) };
  var staatAan = s[code] === true;
  if (aan !== null) {
    tuya_('POST', '/v1.0/devices/' + k.id + '/commands', null, { commands: [{ code: code, value: aan }] });
    staatAan = aan;
  }
  return { ok: true, naam: d.name, online: !!d.online, aan: staatAan };
}

/* ------------------- brug op het thuisnetwerk ------------------- */

/* Voor brug.py: per lamp de lokale sleutel en bij elke code het lokale nummer (dp),
   zodat de brug de lampen rechtstreeks over het thuisnetwerk kan aansturen. */
function lokaal_() {
  var ids = {};
  apparaten_().lampen.forEach(function (l) { ids[l.id] = true; });
  var lijst = alleApparaten_().filter(function (d) { return ids[d.id]; });
  var verzoeken = lijst.map(function (d) {
    return verzoek_('GET', '/v2.0/cloud/thing/' + d.id + '/shadow/properties');
  });
  var antwoorden = verzoeken.length ? UrlFetchApp.fetchAll(verzoeken) : [];
  return lijst.map(function (d, i) {
    var dps = {};
    try {
      var j = JSON.parse(antwoorden[i].getContentText());
      ((j.result && j.result.properties) || []).forEach(function (p) { dps[p.code] = p.dp_id; });
    } catch (e) {}
    return { id: d.id, name: d.name, key: d.local_key, dps: dps };
  });
}

/* --------------------------- helderheid --------------------------- */

function bereik_(code, huidig) {
  if (code === 'bright_value_v2' || Number(huidig) > 255) return [10, 1000];
  return [25, 255];
}
function naarProcent_(code, waarde) {
  var b = bereik_(code, waarde);
  var p = Math.round((Number(waarde) - b[0]) / (b[1] - b[0]) * 99) + 1;
  return Math.max(1, Math.min(100, p));
}
function vanProcent_(b, procent) {
  b = b || [10, 1000];
  var p = Math.max(1, Math.min(100, Number(procent)));
  return Math.round(b[0] + (p - 1) / 99 * (b[1] - b[0]));
}
function tint_(waarde) {
  try {
    var o = typeof waarde === 'string' ? JSON.parse(waarde) : waarde;
    return isNaN(Number(o.h)) ? null : Number(o.h);
  } catch (e) { return null; }
}
function eersteCode_(status, codes) {
  for (var i = 0; i < codes.length; i++) if (status.hasOwnProperty(codes[i])) return codes[i];
  return null;
}

/* ------------------------- Tuya Cloud API ------------------------- */

/* Alle scripteigenschappen in een keer lezen (per aanroep); dat is sneller dan stuk voor stuk. */
var PROPS_ = null;
function prop_(naam) {
  if (!PROPS_) PROPS_ = PropertiesService.getScriptProperties().getProperties();
  return PROPS_[naam] || '';
}
function host_() { return REGIO_HOSTS[(prop_('TUYA_REGIO') || 'eu').toLowerCase()] || REGIO_HOSTS.eu; }

function hex_(bytes) {
  return bytes.map(function (b) { return ('0' + (b & 0xff).toString(16)).slice(-2); }).join('');
}
function sha256_(tekst) {
  return hex_(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, tekst, Utilities.Charset.UTF_8));
}
function hmac_(tekst, geheim) {
  return hex_(Utilities.computeHmacSha256Signature(tekst, geheim, Utilities.Charset.UTF_8)).toUpperCase();
}

/* Pad + query, met de query-parameters alfabetisch (vereist voor de handtekening). */
function padMetQuery_(pad, query) {
  if (!query) return pad;
  var keys = Object.keys(query).filter(function (k) { return query[k] !== undefined && query[k] !== null && query[k] !== ''; }).sort();
  if (!keys.length) return pad;
  return pad + '?' + keys.map(function (k) { return k + '=' + query[k]; }).join('&');
}

/* Bouwt een ondertekend verzoek (voor UrlFetchApp.fetch / fetchAll). */
function verzoek_(methode, pad, query, body, zonderToken) {
  var id = prop_('TUYA_ID');
  var geheim = prop_('TUYA_SECRET');
  if (!id || !geheim) throw new Error('TUYA_ID / TUYA_SECRET ontbreken in de scripteigenschappen');
  var token = zonderToken ? '' : token_();
  var url = padMetQuery_(pad, query);
  var bodyTekst = body ? JSON.stringify(body) : '';
  var t = String(Date.now());
  var tekenTekst = methode + '\n' + sha256_(bodyTekst) + '\n\n' + url;
  var sign = hmac_(id + token + t + tekenTekst, geheim);
  var headers = { client_id: id, sign: sign, t: t, sign_method: 'HMAC-SHA256' };
  if (token) headers.access_token = token;
  var opts = { method: methode.toLowerCase(), headers: headers, muteHttpExceptions: true };
  if (bodyTekst) { opts.payload = bodyTekst; opts.contentType = 'application/json'; }
  opts.url = host_() + url;
  return opts;
}

function tuya_(methode, pad, query, body, poging) {
  var opts = verzoek_(methode, pad, query, body);
  var url = opts.url; delete opts.url;
  var j = JSON.parse(UrlFetchApp.fetch(url, opts).getContentText());
  if (!j.success) {
    /* 1010/1011 = token verlopen of ongeldig: één keer opnieuw proberen. */
    if ((j.code === 1010 || j.code === 1011) && !poging) {
      CacheService.getScriptCache().remove('tuya_token');
      TOKEN_ = null;
      return tuya_(methode, pad, query, body, 1);
    }
    throw new Error('Tuya ' + pad + ': ' + (j.msg || j.code));
  }
  return j.result;
}

var TOKEN_ = null;
function token_() {
  if (TOKEN_) return TOKEN_;
  var cache = CacheService.getScriptCache();
  var c = cache.get('tuya_token');
  if (c) { TOKEN_ = c; return c; }
  var opts = verzoek_('GET', '/v1.0/token', { grant_type: 1 }, null, true);
  var url = opts.url; delete opts.url;
  var j = JSON.parse(UrlFetchApp.fetch(url, opts).getContentText());
  if (!j.success) throw new Error('Tuya token: ' + (j.msg || j.code) + ' — controleer TUYA_ID, TUYA_SECRET en TUYA_REGIO');
  var duur = Math.max(60, Math.min(21600, (j.result.expire_time || 7200) - 120));
  cache.put('tuya_token', j.result.access_token, duur);
  TOKEN_ = j.result.access_token;
  return TOKEN_;
}

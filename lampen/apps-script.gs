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
      var res = schakel_(ids || [], aan, helder);
      CacheService.getScriptCache().remove('status');
      return { ok: true, resultaat: res };
    }
    if (p.action === 'refresh') {
      CacheService.getScriptCache().removeAll(['apparaten', 'status']);
    }
    return { ok: true, ruimtes: stand_() };
  } catch (err) {
    return { ok: false, fout: String(err && err.message || err) };
  }
}

/* Handig om in de editor te draaien: logt wat er gevonden wordt. */
function test() {
  CacheService.getScriptCache().removeAll(['apparaten', 'status', 'tuya_token']);
  var r = stand_();
  r.forEach(function (ruimte) {
    Logger.log('== ' + ruimte.name + ' ==');
    ruimte.lamps.forEach(function (l) {
      Logger.log('  ' + l.name + '  [' + l.id + ']  ' + (l.online ? 'online' : 'OFFLINE') +
        '  ' + (l.on ? 'aan' : 'uit') + (l.bright != null ? '  ' + l.bright + '%' : ''));
    });
  });
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
    lampInfo[l.id] = {
      id: l.id,
      name: l.name,
      online: l.online,
      on: aanCode ? s[aanCode] === true : false,
      bright: helderCode ? naarProcent_(helderCode, s[helderCode]) : null
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
  return uit;
}

/* ---------------------------- schakelen ---------------------------- */

function schakel_(ids, aan, helderProcent) {
  var status = {};
  try { status = status_(apparaten_().lampen.map(function (l) { return l.id; })); } catch (e) {}
  var verzoeken = [];
  var volgorde = [];
  ids.forEach(function (id) {
    var s = status[id] || {};
    var cmds = [];
    if (aan !== null) cmds.push({ code: eersteCode_(s, AAN_CODES) || 'switch_led', value: aan });
    if (helderProcent !== null && !isNaN(helderProcent)) {
      var hc = eersteCode_(s, HELDER_CODES);
      if (hc) cmds.push({ code: hc, value: vanProcent_(hc, s[hc], helderProcent) });
      if (aan === null && helderProcent > 0) cmds.unshift({ code: eersteCode_(s, AAN_CODES) || 'switch_led', value: true });
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
function vanProcent_(code, huidig, procent) {
  var b = bereik_(code, huidig);
  var p = Math.max(1, Math.min(100, Number(procent)));
  return Math.round(b[0] + (p - 1) / 99 * (b[1] - b[0]));
}
function eersteCode_(status, codes) {
  for (var i = 0; i < codes.length; i++) if (status.hasOwnProperty(codes[i])) return codes[i];
  return null;
}

/* ------------------------- Tuya Cloud API ------------------------- */

function prop_(naam) { return PropertiesService.getScriptProperties().getProperty(naam) || ''; }
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
      return tuya_(methode, pad, query, body, 1);
    }
    throw new Error('Tuya ' + pad + ': ' + (j.msg || j.code));
  }
  return j.result;
}

function token_() {
  var cache = CacheService.getScriptCache();
  var c = cache.get('tuya_token');
  if (c) return c;
  var opts = verzoek_('GET', '/v1.0/token', { grant_type: 1 }, null, true);
  var url = opts.url; delete opts.url;
  var j = JSON.parse(UrlFetchApp.fetch(url, opts).getContentText());
  if (!j.success) throw new Error('Tuya token: ' + (j.msg || j.code) + ' — controleer TUYA_ID, TUYA_SECRET en TUYA_REGIO');
  var duur = Math.max(60, Math.min(21600, (j.result.expire_time || 7200) - 120));
  cache.put('tuya_token', j.result.access_token, duur);
  return j.result.access_token;
}

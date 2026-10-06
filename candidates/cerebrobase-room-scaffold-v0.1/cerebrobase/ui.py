"""Server-rendered Norwegian HTML shells. Every dynamic value is escaped. No claim of real login, encryption,
recovery, saved files or operative unqualified tools."""
from __future__ import annotations

from html import escape
import secrets

ENV_LABEL = {"DEV": "Lokal test", "STAGING": "Privat staging", "PROD": "Produksjon"}
STUDIO_CANDIDATE = "0.1-alpha.1"
STUDIO_PREPARED = "6. oktober 2026"
CONTACT_NAMES = {"andreas_admin": "Andreas", "pilot": "Marianne"}
HUMAN_NAMES = {"Andreas (testidentitet)": "Andreas", "Marianne (testidentitet)": "Marianne"}
HUMAN_PAGE_TITLES = {"Testinngang": "Velkommen inn", "Mitt rom": "Ditt rom"}


def _e(v) -> str:
    return escape(str(v), quote=True)


def _human_name(display_name: str) -> str:
    return HUMAN_NAMES.get(display_name, display_name)


def page(title: str, body: str, *, env: str, build_id: str, principal=None, csrf: str | None = None,
         admin_link: bool = False) -> str:
    who = ""
    if principal is not None:
        logout = (f'<form class="inline" method="post" action="/logg-ut"><input type="hidden" name="csrf" '
                  f'value="{_e(csrf or "")}"><button class="quiet" type="submit">Logg ut</button></form>')
        admin = '<a class="admin-link" href="/admin">Admininngang</a>' if admin_link else ""
        who = (f'<nav class="top-nav" aria-label="Konto"><span class="who">{_e(_human_name(principal.display_name))}</span>'
               f'<a href="/rom">Ditt rom</a>{admin}{logout}</nav>')
    return f"""<!doctype html>
<html lang="nb">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(HUMAN_PAGE_TITLES.get(title, title))} · CerebroBase</title><link rel="stylesheet" href="/static/app.css"></head>
<body><a class="skip" href="#main">Hopp til innhold</a>
<div class="wrap">
<header class="top"><a class="brand" href="/">CerebroBase</a>{who}</header>
<main id="main">
{body}
</main>
<footer class="foot"><span>CerebroBase alpha · Kandidat {_e(STUDIO_CANDIDATE)} · Forberedt {_e(STUDIO_PREPARED)}</span>
<details class="changelog"><summary>Endringslogg</summary><p><strong>{_e(STUDIO_PREPARED)} · Kandidat {_e(STUDIO_CANDIDATE)}</strong></p>
<ul><li>Språket er ryddet og gjort mer menneskelig.</li><li>Innlogging og rommet er ført mot en roligere retning.</li>
<li>Postkasse er beholdt som fungerende romfunksjon.</li><li>Kandidatversjonen er synlig.</li></ul></details>
<details class="technical"><summary>Om denne testen</summary><p>{_e(ENV_LABEL.get(env, env))} ·
syntetiske identiteter, ikke ekte innlogging · bygg <span class="receipt">{_e(build_id)}</span></p></details></footer>
</div></body></html>"""


def login_body(prelogin: str, error: str | None = None) -> str:
    err = f'<p class="banner" role="alert">{_e(error)}</p>' if error else ""
    return f"""<section class="login-scene"><p class="eyebrow">Privat alpha</p><h1>Velkommen inn.</h1>
<p class="lead">Velg testrom</p>
{err}
<form method="post" action="/logg-inn" class="identities" aria-label="Velg testrom">
<input type="hidden" name="prelogin" value="{_e(prelogin)}">
<button type="submit" name="identity" value="fixture-andreas-admin"><span>Andreas</span><span aria-hidden="true">→</span></button>
<button type="submit" name="identity" value="fixture-marianne-pilot"><span>Marianne</span><span aria-hidden="true">→</span></button>
</form><p class="staging-note">Dette er et testoppsett med syntetiske identiteter, ikke ekte innlogging.</p></section>"""


def _unavailable(title: str) -> str:
    return (f'<li class="quiet-place"><span>{_e(title)}</span>'
            '<span class="availability">Ikke tilgjengelig ennå</span></li>')


def room_body(room, principal, *, is_admin_room: bool, postkasse_enabled: bool = False) -> str:
    post = (f'<a class="postkasse-entry" href="/rom/{_e(room["id"])}/postkasse"><span>Postkasse</span>'
            '<span aria-hidden="true">↗</span></a>' if postkasse_enabled else
            '<div class="postkasse-entry unavailable"><span>Postkasse</span><small>Ikke tilgjengelig ennå</small></div>')
    places = "".join(_unavailable(name) for name in ("Bokhylla", "Dagbok", "Filer", "Notater", "Bilder"))
    extra = ('<p class="admin-entry">Verktøy og drift: <a href="/admin">Admininngang</a></p>'
             if is_admin_room else "")
    return f"""<section class="room-scene"><p class="eyebrow">Ditt rom</p>
<h1>{_e(_human_name(principal.display_name))}</h1><p class="lead">Et rolig sted for det som hører til her.</p>
<nav aria-label="Romfunksjoner">{post}<ul class="places">{places}</ul></nav>
{extra}<details class="technical"><summary>Om rommet</summary><p>Rom-ID: <span class="receipt">{_e(room["id"])}</span></p>
<p>Filer og andre uferdige funksjoner lagrer ikke innhold her ennå.</p></details></section>"""


def _contact_name(alias: str) -> str:
    return CONTACT_NAMES.get(alias, "Kontakt")


def _message_status(role: str, state: str) -> str:
    if state == "ACKED_BY_RECIPIENT_PROTO":
        return "Lest"
    if role == "sent":
        return "Sendt"
    return "Mottatt"


def postkasse_body(room_id: str, messages: list[dict], contacts: dict[str, str], csrf: str) -> str:
    base = f"/rom/{_e(room_id)}/postkasse"
    names_by_room = {server_id: _contact_name(alias) for alias, server_id in contacts.items()}
    options = "".join(f'<option value="{_e(alias)}">{_e(_contact_name(alias))}</option>'
                      for alias in sorted(contacts))
    rows = "".join(
        f'<li class="message-row"><a href="{base}/{_e(m["message_id"])}">'
        f'{_e("Melding fra " + names_by_room.get(m["sender_room_id"], "en kontakt") if m["role"] == "incoming" else "Din melding")}'
        f'</a><span class="message-state">{_e(_message_status(m["role"], m["delivery_state"]))}</span>'
        f'<details class="technical"><summary>Tekniske detaljer</summary><span class="receipt">'
        f'{_e(m["message_id"])}</span></details></li>' for m in messages)
    form = (f'<form method="post" action="{base}"><input type="hidden" name="csrf" value="{_e(csrf)}">'
            f'<input type="hidden" name="dedupe_key" value="{secrets.token_urlsafe(24)}">'
            f'<label for="recipient">Kontakt</label><select id="recipient" name="recipient">{options}</select>'
            f'<label for="tekst">Kort melding</label><textarea id="tekst" name="tekst" maxlength="4096" '
            f'required></textarea><button type="submit">Send</button></form>' if options else
            '<p class="note">Du kan ikke sende meldinger her ennå.</p>')
    inbox = '<ul>' + rows + '</ul>' if rows else '<p class="empty">Ingen meldinger ennå.</p>'
    return (f'<p><a href="/rom/{_e(room_id)}">Til rommet</a></p><h1>Postkasse</h1>'
            f'<div class="mail-layout"><section class="mail-list"><h2>Meldinger</h2>'
            f'{inbox}</section>'
            f'<section class="compose"><h2>Ny melding</h2>{form}</section></div>')


def postkasse_message_body(room_id: str, message: dict, csrf: str) -> str:
    base = f'/rom/{_e(room_id)}/postkasse'
    mid = _e(message["message_id"])
    key = secrets.token_urlsafe(24)
    actions = (f'<form method="post" action="{base}/{mid}/ack">'
               f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
               f'<button type="submit">Bekreft mottatt</button></form>'
               f'<form method="post" action="{base}/{mid}/reply">'
               f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
               f'<input type="hidden" name="dedupe_key" value="{key}">'
               f'<label for="tekst">Svar</label><textarea id="tekst" name="tekst" maxlength="4096" required>'
               f'</textarea><button type="submit">Svar</button></form>' if message["role"] == "incoming" else "")
    return (f'<p><a href="{base}">Til Postkasse</a></p><section class="message-detail">'
            f'<p class="eyebrow">{_e("Mottatt melding" if message["role"] == "incoming" else "Sendt melding")}</p>'
            f'<h1>Melding</h1><p class="message-state">{_e(_message_status(message["role"], message["delivery_state"]))}</p>'
            f'<div class="message-text">{_e(message["payload"])}</div>'
            f'<details class="technical"><summary>Tekniske detaljer</summary><p>Meldings-ID: '
            f'<span class="receipt">{mid}</span></p></details></section>{actions}')


def admin_body(tools: list[dict], ops: dict, csrf: str, result: str | None = None) -> str:
    rows = []
    for t in tools:
        if t["status"] == "OPERATIVE":
            ctl = (f'<form method="post" action="/admin/verktoy/{_e(t["tool_id"])}"><input type="hidden" name="csrf" '
                   f'value="{_e(csrf)}"><label for="tx-{_e(t["tool_id"])}">Testtekst</label>'
                   f'<input id="tx-{_e(t["tool_id"])}" type="text" name="tekst" maxlength="256" value="hei">'
                   f' <button type="submit">Kjør testverktøy</button></form>')
            badge = '<span class="status ok">Tilgjengelig (kun syntetisk)</span>'
        elif t["status"] == "UNQUALIFIED":
            ctl = f'<span class="note">{_e(t["reason"] or "")}</span>'
            badge = '<span class="status off">Ikke kvalifisert — utilgjengelig</span>'
        else:
            ctl = '<span class="note">Slått av i konfigurasjonen.</span>'
            badge = '<span class="status off">Av</span>'
        rows.append(f'<li><div><strong>{_e(t["title"])}</strong><br>{badge}</div><div>{ctl}</div></li>')
    res = f'<p class="banner" role="status">{_e(result)}</p>' if result else ""
    checks = "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in ops["checks"].items())
    return f"""<div class="admin-band" role="note">Admininngang · applikasjonsrolle ADMIN · ingen shell/root,
ingen innsyn i piloters private innhold</div>
<h1>Verktøy og drift</h1>
{res}
<div class="grid">
<section class="card" aria-labelledby="h-tools"><h2 id="h-tools">Verktøy</h2>
<p class="note">Bare verktøy med kvalifisert port kan kjøres. Hver handling får en redigert kvittering.</p>
<ul class="tools">{''.join(rows)}</ul></section>
<section class="card" aria-labelledby="h-ops"><h2 id="h-ops">Drift</h2>
<dl class="ops"><dt>Miljø</dt><dd>{_e(ops["environment"])}</dd><dt>Bygg</dt><dd>{_e(ops["build_id"])}</dd>
<dt>Skjema</dt><dd>{_e(ops["schema_version"])}</dd><dt>Klar</dt><dd>{_e("ja" if ops["ready"] else "nei")}</dd>
<dt>Rom (antall)</dt><dd>{_e(ops["rooms"])}</dd><dt>Kontoer (antall)</dt><dd>{_e(ops["accounts"])}</dd>{checks}</dl>
</section></div>"""


def message_body(title: str, text: str) -> str:
    return f'<h1>{_e(title)}</h1><p class="lead">{_e(text)}</p><p><a href="/">Til start</a></p>'

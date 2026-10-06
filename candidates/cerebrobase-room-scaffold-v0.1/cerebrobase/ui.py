"""Server-rendered Norwegian HTML shells. Every dynamic value is escaped. No claim of real login, encryption,
recovery, saved files or operative unqualified tools."""
from __future__ import annotations

from html import escape

ENV_LABEL = {"DEV": "Utvikling (lokal)", "STAGING": "Staging (privat)", "PROD": "Produksjon"}


def _e(v) -> str:
    return escape(str(v), quote=True)


def page(title: str, body: str, *, env: str, build_id: str, principal=None, csrf: str | None = None,
         admin_link: bool = False) -> str:
    who = ""
    if principal is not None:
        logout = (f'<form class="inline" method="post" action="/logg-ut"><input type="hidden" name="csrf" '
                  f'value="{_e(csrf or "")}"><button class="quiet" type="submit">Logg ut</button></form>')
        admin = '<a href="/admin">Admininngang</a>' if admin_link else ""
        who = (f'<nav class="top-nav" aria-label="Konto"><span class="who">{_e(principal.display_name)}</span>'
               f'<a href="/rom">Mitt rom</a>{admin}{logout}</nav>')
    return f"""<!doctype html>
<html lang="nb">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} · Cerebrobase</title><link rel="stylesheet" href="/static/app.css"></head>
<body><a class="skip" href="#main">Hopp til innhold</a>
<div class="wrap">
<header class="top"><a class="brand" href="/">Cerebrobase</a>{who}</header>
<main id="main">
{body}
</main>
<footer class="foot">{_e(ENV_LABEL.get(env, env))} · bygg <span class="receipt">{_e(build_id)}</span> ·
testoppsett med syntetiske identiteter, ikke ekte innlogging.</footer>
</div></body></html>"""


def login_body(prelogin: str, error: str | None = None) -> str:
    err = f'<p class="banner" role="alert">{_e(error)}</p>' if error else ""
    return f"""<h1>Lokal testinngang</h1>
<p class="lead">Velg en testidentitet for å åpne et rom i dette lokale/staging-oppsettet.</p>
<p class="banner"><strong>Kun utvikling og staging.</strong> Dette er ikke ekte innlogging. Ingen ekte kontoer,
passord, nøkler eller private data finnes her.</p>
{err}
<form method="post" action="/logg-inn" class="identities" aria-label="Velg testidentitet">
<input type="hidden" name="prelogin" value="{_e(prelogin)}">
<button type="submit" name="identity" value="fixture-andreas-admin">Testidentitet: Andreas (ADMIN)</button>
<button type="submit" name="identity" value="fixture-marianne-pilot">Testidentitet: Marianne (PILOT)</button>
</form>"""


def _unavailable(title: str, text: str) -> str:
    return (f'<section class="card" aria-labelledby="h-{_e(title)}"><h2 id="h-{_e(title)}">{_e(title)}</h2>'
            f'<p><span class="status off">Ikke tilgjengelig ennå</span></p><p class="note">{_e(text)}</p></section>')


def room_body(room, principal, *, is_admin_room: bool) -> str:
    kind = "Adminrom" if is_admin_room else "Pilotrom"
    files = _unavailable("Private filer", "Filområdet kommer med CB-P02. Ingen filer er lagret, og ingenting kan "
                                          "lastes opp eller gjenopprettes her ennå.")
    if is_admin_room:
        extra = ('<section class="card" aria-labelledby="h-adm"><h2 id="h-adm">Verktøy og drift</h2>'
                 '<p class="note">Verktøypanel og driftsoverblikk ligger bak den separate admininngangen.</p>'
                 '<p><a class="button" href="/admin">Åpne admininngang</a></p></section>')
        books = ""
    else:
        extra = ""
        books = _unavailable("Stambok og dagbok", "Speil av Stambok/dagbok kommer med CB-P03 når en lovlig kilde "
                                                  "er avklart. Ingen innhold vises her ennå.")
    post = _unavailable("Postkasse", "Postkasse er en egen tjeneste og er ikke koblet til dette rommet ennå.")
    return f"""<h1>{_e(principal.display_name)} · {kind}</h1>
<p class="lead">Ditt private rom <span class="receipt">{_e(room["id"])}</span>. Bare medlemmer av rommet har tilgang;
navigasjonen gir ingen rettigheter i seg selv.</p>
<div class="grid">{files}{books}{post}{extra}</div>"""


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

"""Bilingual UI: Serbian (default) and English.

The Serbian source string is the lookup key, the way gettext uses the source
language as the message id. Two things follow from that, both deliberate:

  * A string with no translation yet renders in Serbian rather than showing a
    raw key or an empty label, so a gap is a cosmetic shortfall and never a
    broken page.
  * There are no key names to invent or keep in sync with the templates. The
    text in the template is the text in the catalogue.

Templates call `tr("...")`, which is registered as a Jinja global. The chosen
language lives in the session and is set by GET /lang/<code>.
"""

from __future__ import annotations

from flask import g, redirect, request, session, url_for

from translations import TRANSLATIONS, TRANSLATIONS_EN_TO_SR

DEFAULT_LANG = "sr"
LANGUAGES = {"sr": "Srpski", "en": "English"}


def get_lang() -> str:
    """The active language for this request.

    Resolution order: an explicit ?lang= on the query string, then the
    session, then the Accept-Language header, then the default.
    """
    if "lang" in g:
        return g.lang

    lang = request.args.get("lang")
    if lang not in LANGUAGES:
        lang = session.get("lang")
    if lang not in LANGUAGES:
        best = request.accept_languages.best_match(list(LANGUAGES)) if request else None
        lang = best or DEFAULT_LANG

    g.lang = lang
    return lang


def tr(text: str, **kwargs) -> str:
    """Translate a Serbian source string into the active language.

    Keyword arguments are substituted with str.format, so a message that
    carries a number or a name works in both languages:

        tr("Upisano {n} faktura.", n=42)

    A missing translation returns the Serbian source unchanged.
    """
    if get_lang() == "en":
        text = TRANSLATIONS.get(text, text)
    else:
        # A few modules were named in English in the original system; map them
        # back so the Serbian interface does not read as half-translated.
        text = TRANSLATIONS_EN_TO_SR.get(text, text)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            # A malformed placeholder must not take a page down.
            return text
    return text


def init_app(app) -> None:
    """Register the Jinja helpers and the language switch route."""

    # Named `tr`, not `t`: the templates already use `t` as a loop variable
    # in a dozen places, and shadowing the helper there is a silent breakage.
    app.jinja_env.globals["tr"] = tr
    app.jinja_env.globals["get_lang"] = get_lang
    app.jinja_env.globals["LANGUAGES"] = LANGUAGES
    app.jinja_env.globals["month_name"] = month_name
    app.jinja_env.globals["month_short"] = month_short
    app.jinja_env.globals["month_shorts"] = month_shorts
    app.jinja_env.globals["day_name"] = day_name
    app.jinja_env.globals["months"] = months

    @app.route("/lang/<code>")
    def set_language(code):
        if code in LANGUAGES:
            session["lang"] = code
            session.permanent = True
        target = request.referrer or url_for("index")
        return redirect(target)

    @app.context_processor
    def inject_lang():
        return {"current_lang": get_lang()}

# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
# Month and day names were duplicated as a module-level MONTHS_SR constant in
# eight blueprints. A module-level list cannot vary per request, so they are
# functions here and the blueprints call them at render time.

MONTHS = {
    "sr": ["januar", "februar", "mart", "april", "maj", "jun",
           "jul", "avgust", "septembar", "oktobar", "novembar", "decembar"],
    "en": ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"],
}

MONTHS_SHORT = {
    "sr": ["jan", "feb", "mar", "apr", "maj", "jun",
           "jul", "avg", "sep", "okt", "nov", "dec"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}

DAYS = {
    "sr": ["Ponedeljak", "Utorak", "Sreda", "Četvrtak", "Petak", "Subota", "Nedelja"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}


def months(capitalized: bool = False) -> list[str]:
    """The twelve month names in the active language."""
    names = MONTHS[get_lang()]
    return [n.capitalize() for n in names] if capitalized else list(names)


def month_name(month: int, capitalized: bool = False) -> str:
    """Month name for a 1-based month number."""
    names = MONTHS[get_lang()]
    name = names[(int(month) - 1) % 12]
    return name.capitalize() if capitalized else name


def month_short(month: int) -> str:
    """Three-letter month name for a 1-based month number."""
    return MONTHS_SHORT[get_lang()][(int(month) - 1) % 12]


def month_shorts() -> list[str]:
    """All twelve abbreviations, for the templates that hand them to JavaScript."""
    return list(MONTHS_SHORT[get_lang()])


def day_name(weekday: int) -> str:
    """Day name for a Monday-is-zero weekday index."""
    return DAYS[get_lang()][int(weekday) % 7]

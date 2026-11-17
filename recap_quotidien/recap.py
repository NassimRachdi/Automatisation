#!/usr/bin/env python3
"""Récapitulatif quotidien par e-mail des événements d'un ou plusieurs calendriers ICS.

Configuration par variables d'environnement (voir README.md) :
    ICS_URLS        Un ou plusieurs calendriers, un par ligne (ou séparés par « ; »).
                    Format : « Nom|https://.../calendar.ics » ou juste l'URL.
                    Un chemin de fichier local .ics est aussi accepté.
    SMTP_HOST       ex. smtp.gmail.com
    SMTP_PORT       ex. 587
    SMTP_USER       identifiant SMTP (adresse e-mail)
    SMTP_PASSWORD   mot de passe d'application
    MAIL_TO         destinataire(s), séparés par des virgules
    MAIL_FROM       (optionnel) expéditeur, par défaut SMTP_USER
    TIMEZONE        (optionnel) par défaut Europe/Paris

Usage :
    python recap.py                 # envoie le mail du jour
    python recap.py --dry-run       # génère apercu.html sans envoyer
    python recap.py --date 2026-11-18 --dry-run
"""

from __future__ import annotations

import argparse
import html
import os
import smtplib
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
import requests

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]
COULEURS = ["#2563eb", "#16a34a", "#d97706", "#9333ea", "#dc2626", "#0891b2"]


@dataclass
class Evenement:
    titre: str
    debut: datetime
    fin: datetime
    journee_entiere: bool
    lieu: str
    calendrier: str
    couleur: str
    en_ligne: str  # lien Teams/Zoom éventuel


def date_fr(d: date) -> str:
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {d.year}"


def parse_sources(brut: str) -> list[tuple[str, str]]:
    sources = []
    for ligne in brut.replace(";", "\n").splitlines():
        ligne = ligne.strip()
        if not ligne:
            continue
        if "|" in ligne:
            nom, url = ligne.split("|", 1)
        else:
            nom, url = f"Calendrier {len(sources) + 1}", ligne
        sources.append((nom.strip(), url.strip()))
    return sources


def charger_calendrier(url: str) -> icalendar.Calendar:
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if url.startswith(("http://", "https://")):
        r = requests.get(url, timeout=30, headers={"User-Agent": "recap-quotidien/1.0"})
        r.raise_for_status()
        contenu = r.content
    else:
        contenu = Path(url).read_bytes()
    return icalendar.Calendar.from_ical(contenu)


def vers_datetime(valeur, tz: ZoneInfo) -> tuple[datetime, bool]:
    """Retourne (datetime dans tz, est_journee_entiere)."""
    if isinstance(valeur, datetime):
        if valeur.tzinfo is None:
            valeur = valeur.replace(tzinfo=tz)
        return valeur.astimezone(tz), False
    return datetime.combine(valeur, time.min, tzinfo=tz), True


def trouver_lien_visio(composant) -> str:
    texte = " ".join(str(composant.get(k, "")) for k in ("DESCRIPTION", "LOCATION", "URL"))
    for marqueur in ("https://teams.microsoft.com/", "https://teams.live.com/",
                     "https://zoom.us/", "https://meet.google.com/"):
        i = texte.find(marqueur)
        if i != -1:
            fin = i
            while fin < len(texte) and texte[fin] not in " \n\r\t<>\"'":
                fin += 1
            return texte[i:fin]
    return ""


def evenements_du_jour(sources, jour: date, tz: ZoneInfo) -> tuple[list[Evenement], list[str]]:
    debut_jour = datetime.combine(jour, time.min, tzinfo=tz)
    fin_jour = debut_jour + timedelta(days=1)
    evenements, erreurs = [], []

    for i, (nom, url) in enumerate(sources):
        couleur = COULEURS[i % len(COULEURS)]
        try:
            cal = charger_calendrier(url)
        except Exception as exc:  # on continue avec les autres calendriers
            erreurs.append(f"{nom} : impossible de charger le calendrier ({exc.__class__.__name__})")
            continue

        for ev in recurring_ical_events.of(cal).between(debut_jour, fin_jour):
            if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                continue
            debut, entiere = vers_datetime(ev["DTSTART"].dt, tz)
            if "DTEND" in ev:
                fin, _ = vers_datetime(ev["DTEND"].dt, tz)
            elif "DURATION" in ev:
                fin = debut + ev["DURATION"].dt
            else:
                fin = debut + (timedelta(days=1) if entiere else timedelta(0))
            # recurring_ical_events renvoie aussi les événements qui chevauchent la journée
            if fin <= debut_jour and fin != debut:
                continue
            evenements.append(Evenement(
                titre=str(ev.get("SUMMARY", "(sans titre)")).strip() or "(sans titre)",
                debut=debut,
                fin=fin,
                journee_entiere=entiere,
                lieu=str(ev.get("LOCATION", "")).strip(),
                calendrier=nom,
                couleur=couleur,
                en_ligne=trouver_lien_visio(ev),
            ))

    # suppression des doublons (même événement présent dans 2 calendriers)
    vus, uniques = set(), []
    for e in sorted(evenements, key=lambda e: (not e.journee_entiere, e.debut, e.titre)):
        cle = (e.titre.lower(), e.debut, e.fin)
        if cle not in vus:
            vus.add(cle)
            uniques.append(e)
    return uniques, erreurs


def duree_txt(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h{m:02d}"
    return f"{h}h" if h else f"{m} min"


def construire_mail(jour: date, evts: list[Evenement], erreurs: list[str],
                    demain: list[Evenement]) -> tuple[str, str, str]:
    """Retourne (sujet, texte brut, html)."""
    horaires = [e for e in evts if not e.journee_entiere]
    journee = [e for e in evts if e.journee_entiere]
    occupe = sum((e.fin - e.debut for e in horaires), timedelta())

    if evts:
        sujet = f"📅 Ta journée du {date_fr(jour)} — {len(evts)} événement{'s' if len(evts) > 1 else ''}"
    else:
        sujet = f"📅 Ta journée du {date_fr(jour)} — rien de prévu"

    # ---------- texte brut ----------
    lignes = [f"Récapitulatif du {date_fr(jour)}", ""]
    if not evts:
        lignes.append("Aucun événement aujourd'hui. Profite-en ! 🎉")
    for e in journee:
        lignes.append(f"• Toute la journée : {e.titre}" + (f" ({e.lieu})" if e.lieu else ""))
    for e in horaires:
        l = f"• {e.debut:%H:%M}–{e.fin:%H:%M}  {e.titre}"
        if e.lieu:
            l += f"  📍 {e.lieu}"
        lignes.append(l)
    if horaires:
        lignes += ["", f"Temps occupé : {duree_txt(occupe)} — "
                       f"de {horaires[0].debut:%H:%M} à {max(e.fin for e in horaires):%H:%M}"]
    if demain:
        premier = next((e for e in demain if not e.journee_entiere), demain[0])
        heure = "toute la journée" if premier.journee_entiere else f"à {premier.debut:%H:%M}"
        lignes += ["", f"Demain : {len(demain)} événement(s), le premier {heure} : {premier.titre}"]
    for err in erreurs:
        lignes += ["", f"⚠️ {err}"]
    texte = "\n".join(lignes)

    # ---------- HTML ----------
    esc = html.escape

    def carte(e: Evenement) -> str:
        if e.journee_entiere:
            heure = "<b>Toute la journée</b>"
        else:
            heure = (f"<b>{e.debut:%H:%M}</b> – {e.fin:%H:%M}"
                     f"<br><span style='color:#6b7280;font-size:12px'>{duree_txt(e.fin - e.debut)}</span>")
        details = []
        if e.lieu:
            details.append(f"📍 {esc(e.lieu)}")
        if e.en_ligne:
            details.append(f"<a href='{esc(e.en_ligne)}' style='color:#2563eb'>🎥 Rejoindre en ligne</a>")
        details.append(f"<span style='color:{e.couleur}'>● {esc(e.calendrier)}</span>")
        return f"""
        <tr>
          <td style="padding:10px 12px;vertical-align:top;width:90px;font-size:14px;
                     border-left:4px solid {e.couleur};background:#f9fafb">{heure}</td>
          <td style="padding:10px 12px;vertical-align:top;background:#f9fafb">
            <div style="font-size:15px;font-weight:600;color:#111827">{esc(e.titre)}</div>
            <div style="font-size:13px;color:#4b5563;margin-top:4px">{' &nbsp;·&nbsp; '.join(details)}</div>
          </td>
        </tr>
        <tr><td colspan="2" style="height:8px"></td></tr>"""

    if evts:
        corps = "".join(carte(e) for e in journee + horaires)
        corps = f"<table role='presentation' style='width:100%;border-collapse:collapse'>{corps}</table>"
    else:
        corps = "<p style='font-size:15px'>Aucun événement aujourd'hui. Profite-en ! 🎉</p>"

    resume = ""
    if horaires:
        resume = (f"<p style='font-size:14px;color:#374151;margin:0 0 16px'>"
                  f"<b>{len(horaires)}</b> créneau{'x' if len(horaires) > 1 else ''} · "
                  f"<b>{duree_txt(occupe)}</b> occupées · de <b>{horaires[0].debut:%H:%M}</b> "
                  f"à <b>{max(e.fin for e in horaires):%H:%M}</b></p>")

    bloc_demain = ""
    if demain:
        premier = next((e for e in demain if not e.journee_entiere), demain[0])
        heure = "toute la journée" if premier.journee_entiere else f"à <b>{premier.debut:%H:%M}</b>"
        bloc_demain = (f"<div style='margin-top:20px;padding:12px;border-radius:8px;background:#eff6ff;"
                       f"font-size:14px;color:#1e3a8a'>👀 <b>Demain</b> : {len(demain)} événement"
                       f"{'s' if len(demain) > 1 else ''} — premier {heure} : {esc(premier.titre)}</div>")

    bloc_erreurs = "".join(
        f"<p style='font-size:13px;color:#b91c1c'>⚠️ {esc(err)}</p>" for err in erreurs)

    page = f"""<!doctype html>
<html lang="fr"><body style="margin:0;background:#f3f4f6;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif">
  <div style="max-width:600px;margin:0 auto;padding:24px 16px">
    <div style="background:#ffffff;border-radius:12px;padding:24px">
      <div style="font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em">Récapitulatif du jour</div>
      <h1 style="font-size:22px;color:#111827;margin:4px 0 16px">{esc(date_fr(jour).capitalize())}</h1>
      {resume}
      {corps}
      {bloc_demain}
      {bloc_erreurs}
    </div>
    <p style="text-align:center;font-size:11px;color:#9ca3af;margin-top:12px">Envoyé automatiquement par recap-quotidien</p>
  </div>
</body></html>"""
    return sujet, texte, page


def envoyer(sujet: str, texte: str, page: str) -> None:
    hote = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    mdp = os.environ["SMTP_PASSWORD"]
    dest = [a.strip() for a in os.environ["MAIL_TO"].split(",") if a.strip()]

    msg = EmailMessage()
    msg["Subject"] = sujet
    msg["From"] = os.environ.get("MAIL_FROM") or user
    msg["To"] = ", ".join(dest)
    msg.set_content(texte)
    msg.add_alternative(page, subtype="html")

    if port == 465:
        with smtplib.SMTP_SSL(hote, port, timeout=30) as s:
            s.login(user, mdp)
            s.send_message(msg)
    else:
        with smtplib.SMTP(hote, port, timeout=30) as s:
            s.starttls()
            s.login(user, mdp)
            s.send_message(msg)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date", help="Date à récapituler (AAAA-MM-JJ), par défaut aujourd'hui")
    p.add_argument("--dry-run", action="store_true", help="N'envoie pas : écrit apercu.html")
    args = p.parse_args()

    tz = ZoneInfo(os.environ.get("TIMEZONE") or "Europe/Paris")
    jour = date.fromisoformat(args.date) if args.date else datetime.now(tz).date()

    sources = parse_sources(os.environ.get("ICS_URLS", ""))
    if not sources:
        print("Erreur : la variable ICS_URLS est vide.", file=sys.stderr)
        return 1

    evts, erreurs = evenements_du_jour(sources, jour, tz)
    demain, _ = evenements_du_jour(sources, jour + timedelta(days=1), tz)
    if len(erreurs) == len(sources):
        print("Erreur : aucun calendrier n'a pu être chargé :\n  " + "\n  ".join(erreurs), file=sys.stderr)
        return 1

    sujet, texte, page = construire_mail(jour, evts, erreurs, demain)

    if args.dry_run:
        Path("apercu.html").write_text(page, encoding="utf-8")
        print(sujet, "\n", texte, "\n\n→ aperçu écrit dans apercu.html", sep="")
        return 0

    envoyer(sujet, texte, page)
    print(f"Mail envoyé : {sujet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

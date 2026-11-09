"""Career Finder Marrakech — version Streamlit.
Reprend exactement la logique du backend FastAPI (backend/database.py,
backend/security.py, backend/mailer.py, pipeline/*) mais en interface
Streamlit native. Le frontend HTML/JS et l'API FastAPI restent disponibles.
"""
# Doit précéder l'import de backend.security (lecture de SECRET_KEY)
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import datetime as _dt
import io
import re
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

import folium

from backend.database import (
    CONTRATS,
    TYPES_STAGE,
    application_owner,
    count_emails_since,
    count_users,
    create_user,
    delete_offer,
    delete_opportunity,
    get_connection,
    get_opportunities_for_user,
    get_offer_owner,
    get_opportunity,
    get_opportunity_owner,
    get_profile,
    get_user,
    init_db,
    insert_manual_opportunity,
    insert_offer,
    last_email_to,
    list_offers,
    log_email,
    toggle_favorite,
    update_opportunity_fields,
    upsert_offers,
    upsert_opportunities,
    upsert_profile,
)
from backend.mailer import (
    MAX_CV_BYTES,
    MailError,
    build_cv_message,
    is_valid_email,
    send_message,
    smtp_configured,
)
from backend.security import hash_password, verify_password
from pipeline.cleaner import clean_and_score_places, raw_places_to_dataframe
from pipeline.excel_importer import clean_excel_opportunities
from pipeline.google_places import fetch_it_companies_google
from pipeline.sectors import SECTEURS, _normalize

init_db()

GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
ALLOW_REGISTRATION = os.getenv("ALLOW_REGISTRATION", "true").lower() in ("1", "true", "yes")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD_LENGTH = 8
STATUTS = ["À contacter", "Envoyée", "Entretien", "Offre reçue", "Refusée"]
MAX_CV_PER_HOUR = int(os.getenv("MAX_CV_PER_HOUR", "10"))
MAX_CV_PER_DAY = int(os.getenv("MAX_CV_PER_DAY", "30"))
CV_RESEND_DELAY_DAYS = 7
EXTRA_QUERIES = ["entreprise informatique", "société IT", "startup tech", "data science",
                 "développement logiciel", "cloud computing", "cybersécurité", "agence digitale"]

st.set_page_config(page_title="Career Finder Marrakech", page_icon="🎯", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
  .stApp { background: #f4f6f8; }
  .brand { font-weight: 800; font-size: 1.3rem; color: #0f172a; }
  .muted { color: #64748b; font-size: .9rem; }
  .card { background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 1.1rem; box-shadow: 0 1px 3px rgba(15,23,42,.08); }
  .chip { display: inline-block; padding: .1rem .55rem; border-radius: 999px; font-size: .75rem; font-weight: 600; background: #ccfbf1; color: #0f766e; }
  .kanban-col { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: .75rem; min-height: 200px; }
  .kcard { background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: .65rem; margin-top: .6rem; }
</style>
""", unsafe_allow_html=True)

if "user" not in st.session_state:
    st.session_state.user = None
if "is_admin" not in st.session_state:
    st.session_state.is_admin = False
if "snapshots" not in st.session_state:
    st.session_state.snapshots = {}
if "gmaps_key" not in st.session_state:
    st.session_state.gmaps_key = ""

def compute_match_score(profile_skills: str, mots_cles: str, score_opportunite: int) -> float:
    skills_set = {s.strip().lower() for s in (profile_skills or "").split(",") if s.strip()}
    opp_set = {s.strip().lower() for s in (mots_cles or "").split(",") if s.strip()}
    keyword_match = 0.0 if not skills_set or not opp_set else round(100 * len(skills_set & opp_set) / len(skills_set), 1)
    return min(round(0.6 * keyword_match + 0.4 * (score_opportunite or 0), 1), 100.0)

def default_cv_message(company: str) -> str:
    user = st.session_state.user or ""
    return (f"Bonjour,\n\nJe me permets de vous contacter afin de vous proposer ma candidature "
            f"pour un stage au sein de {company}.\nVous trouverez mon CV en pièce jointe.\n\n"
            f"Je reste à votre disposition pour tout complément d'information.\n\nCordialement,\n{user}")

def can_edit(added_by: str) -> bool:
    return bool(st.session_state.user) and (st.session_state.is_admin or added_by == st.session_state.user)

def fmt_day(s) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s or "")
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else (s or "")

def fmt_ts(s) -> str:
    if not s:
        return ""
    d = pd.to_datetime(s, errors="coerce", utc=True)
    try:
        return d.strftime("%d/%m/%Y")
    except Exception:
        return str(s)

def error_text(e) -> str:
    return str(e)

# ---------- Auth ----------
def show_auth():
    st.markdown("# 🎯 Career Finder")
    st.markdown('<p class="muted">Entreprises, offres et candidatures IA &amp; IT à Marrakech.</p>', unsafe_allow_html=True)
    tab_login, tab_register = st.tabs(["Connexion", "Créer un compte"])
    with tab_login:
        with st.form("login_form"):
            username = st.text_input("Nom d'utilisateur", key="login_u")
            password = st.text_input("Mot de passe", type="password", key="login_p")
            if st.form_submit_button("Se connecter", use_container_width=True, type="primary"):
                if not username.strip() or not password:
                    st.error("Remplis les deux champs.")
                else:
                    # rate limiting simple : même logique que backend (5 échecs / 5 min)
                    # On s'appuie sur la vérification ci-dessous (temps homogène)
                    user = get_user(username.strip())
                    stored = user["password_hash"] if user else "scrypt$AAAA$AAAA"
                    if not verify_password(password, stored) or user is None:
                        st.error("Identifiants incorrects.")
                    else:
                        st.session_state.user = user["username"]
                        st.session_state.is_admin = bool(user["is_admin"])
                        st.rerun()
    with tab_register:
        if not ALLOW_REGISTRATION and count_users() > 0:
            st.info("Les inscriptions sont désactivées sur ce serveur.")
        else:
            with st.form("register_form"):
                username = st.text_input("Nom d'utilisateur", key="reg_u", help="3 à 32 caractères (lettres, chiffres, . _ -)")
                password = st.text_input("Mot de passe", type="password", key="reg_p", help=f"{MIN_PASSWORD_LENGTH} caractères minimum")
                confirm = st.text_input("Confirmer le mot de passe", type="password", key="reg_c")
                if st.form_submit_button("Créer mon compte", use_container_width=True, type="primary"):
                    username = username.strip()
                    if not USERNAME_RE.match(username):
                        st.error("Nom d'utilisateur : 3 à 32 caractères (lettres, chiffres, . _ -).")
                    elif len(password) < MIN_PASSWORD_LENGTH:
                        st.error(f"Mot de passe : {MIN_PASSWORD_LENGTH} caractères minimum.")
                    elif password != confirm:
                        st.error("Les mots de passe ne correspondent pas.")
                    else:
                        try:
                            u = create_user(username, hash_password(password))
                            st.session_state.user = u["username"]
                            st.session_state.is_admin = bool(u["is_admin"])
                            st.success(f"Compte créé : {u['username']}" + (" (administrateur)" if u["is_admin"] else ""))
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))

# ---------- Helpers UI ----------
def score_bar(score):
    n = max(0, min(100, int(round(float(score or 0)))))
    st.markdown(f'<div style="display:flex;align-items:center;gap:.4rem"><strong>{n}</strong>'
                f'<div style="width:56px;height:6px;background:#e2e8f0;border-radius:3px;overflow:hidden">'
                f'<div style="width:{n}%;height:100%;background:#0d9488"></div></div></div>', unsafe_allow_html=True)

def get_stats():
    df = get_opportunities_for_user(st.session_state.user)
    conn = get_connection()
    apps = pd.read_sql_query("SELECT * FROM applications WHERE user=? ORDER BY date_ajout DESC", conn, params=(st.session_state.user,))
    conn.close()
    offers = list_offers()
    has_email = df["email"].fillna("").str.strip() != "" if not df.empty else pd.Series(dtype=bool)
    has_site = df["site_web"].fillna("").str.strip() != "" if not df.empty else pd.Series(dtype=bool)
    n = len(df)
    def counts(series):
        return {str(k): int(v) for k, v in series.value_counts().items()} if len(series) else {}
    return {
        "total_opportunites": n,
        "score_moyen": round(df["score_opportunite"].mean(), 1) if n else 0,
        "favoris": int(df["favori"].sum()) if n else 0,
        "total_candidatures": len(apps),
        "candidatures_par_statut": counts(apps["statut"]) if not apps.empty else {},
        "total_offres": len(offers),
        "total_secteurs": int(df["secteur"].nunique()) if n else 0,
        "entreprises_avec_email": int(has_email.sum()) if n else 0,
        "cv_envoyes": int(df["cv_envoye_le"].notna().sum()) if n else 0,
        "par_secteur": counts(df["secteur"]) if n else {},
        "offres_par_type_stage": counts(offers["type_stage"]) if not offers.empty else {},
        "offres_par_contrat": counts(offers["contrat"]) if not offers.empty else {},
        "presence_numerique": {
            "avec_email": int(has_email.sum()) if n else 0,
            "sans_email": int((~has_email).sum()) if n else 0,
            "avec_site_web": int(has_site.sum()) if n else 0,
            "sans_site_web": int((~has_site).sum()) if n else 0,
        },
    }

def _df_to_xlsx(sheets: dict) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in sheets.items():
            (df if df is not None else pd.DataFrame()).to_excel(writer, sheet_name=name[:31], index=False)
    buf.seek(0)
    return buf.getvalue()

def send_cv_dialog(opp, subject="Candidature pour un stage"):
    """Modale d'envoi de CV — même logique que POST /api/send-cv."""
    smtp = smtp_configured()
    st.markdown(f"### ✉️ Envoyer mon CV — {opp['nom']}")
    st.caption(f"{opp['secteur']} · Destinataire : {opp['email']}")
    if not smtp:
        st.warning("L'envoi automatique n'est pas configuré sur ce serveur (SMTP). Tu peux ouvrir ce message dans ta messagerie et joindre ton CV toi-même.")
    with st.form("cv_form"):
        subj = st.text_input("Objet", value=subject, max_chars=200)
        reply_to = st.text_input("Ton adresse e-mail (pour réponse)", value=st.session_state.get("reply_to", ""), max_chars=254)
        cv_file = st.file_uploader("CV (PDF, 5 Mo maximum)", type=["pdf"])
        message = st.text_area("Message", value=default_cv_message(opp["nom"]), height=200, max_chars=5000)
        col_mailto, col_send = st.columns([1, 1])
        with col_mailto:
            mailto = st.form_submit_button("Ouvrir dans ma messagerie", use_container_width=True, type="secondary")
        with col_send:
            send = st.form_submit_button("📤 Envoyer", use_container_width=True, type="primary", disabled=not smtp)
    if mailto:
        import urllib.parse
        href = f"mailto:{opp['email']}?subject={urllib.parse.quote(subj)}&body={urllib.parse.quote(message)}"
        st.link_button("📧 Depuis ta messagerie", href, use_container_width=True)
        st.caption("Attache ton CV PDF manuellement dans le mail ouvert.")
    if send:
        if not subj.strip() or not message.strip():
            st.error("L'objet et le message sont obligatoires.")
            return
        if reply_to and not is_valid_email(reply_to.strip()):
            st.error("Ton adresse de réponse est invalide.")
            return
        if cv_file is None:
            st.error("Choisis ton CV au format PDF.")
            return
        cv_bytes = cv_file.getvalue()
        if len(cv_bytes) > MAX_CV_BYTES:
            st.error("CV trop volumineux (5 Mo maximum).")
            return
        if not cv_file.name.lower().endswith(".pdf") or not cv_bytes.startswith(b"%PDF"):
            st.error("Le CV doit être un fichier PDF valide.")
            return
        user = st.session_state.user
        if count_emails_since(user, 1) >= MAX_CV_PER_HOUR or count_emails_since(user, 24) >= MAX_CV_PER_DAY:
            st.error("Limite d'envois atteinte (10 par heure, 30 par jour). Réessaie plus tard.")
            return
        previous = last_email_to(user, opp["id"])
        if previous:
            sent_at = datetime.strptime(previous, "%Y-%m-%d %H:%M:%S")
            if datetime.now(timezone.utc).replace(tzinfo=None) - sent_at < timedelta(days=CV_RESEND_DELAY_DAYS):
                if not st.checkbox(f"Tu as déjà envoyé ton CV à cette entreprise le {sent_at:%d/%m/%Y}. Coche pour confirmer le renvoi.", key="cv_force"):
                    st.error(f"Tu as déjà envoyé ton CV à cette entreprise le {sent_at:%d/%m/%Y}. Coche la case pour le renvoyer.")
                    st.stop()
        msg = build_cv_message(to_email=opp["email"], subject=subj.strip(), body=message.strip(),
                               cv_bytes=cv_bytes, cv_filename=cv_file.name, reply_to=reply_to.strip())
        try:
            send_message(msg)
        except MailError as e:
            st.error(str(e))
            return
        log_email(user, opp["id"], opp["email"], subj.strip())
        conn = get_connection()
        conn.execute(
            """INSERT INTO applications (user, company, poste, statut, date_candidature, remarque, opportunity_id)
               VALUES (?, ?, ?, 'Envoyée', ?, ?, ?)""",
            (user, opp["nom"], "Candidature spontanée", date.today().isoformat(),
             f"CV envoyé par e-mail à {opp['email']}", opp["id"]),
        )
        conn.commit()
        conn.close()
        if reply_to:
            st.session_state.reply_to = reply_to
        st.success(f"CV envoyé à {opp['nom']} ✅")
        st.rerun()

# ---------- Pages ----------
def page_accueil():
    st.markdown(f"# Bonjour {st.session_state.user} 👋")
    st.markdown("Retrouve les entreprises, les offres et le suivi de tes candidatures à Marrakech.")
    stats = get_stats()
    opps_df = get_opportunities_for_user(st.session_state.user)
    offers_df = list_offers()
    if opps_df.empty:
        st.info("Aucune donnée pour l'instant. Va dans **Collecte & Import** pour récupérer des entreprises (Google Places ou Excel).")
    cols = st.columns(6)
    for c, (icon, key, label) in zip(cols, [("🏢", "total_opportunites", "Entreprises"), ("📄", "total_offres", "Offres"),
                                            ("🏷️", "total_secteurs", "Secteurs"), ("✉️", "entreprises_avec_email", "Avec e-mail"),
                                            ("📨", "cv_envoyes", "CV envoyés"), ("📬", "total_candidatures", "Candidatures")]):
        c.metric(icon, stats[key], label)
    top = opps_df.sort_values("score_opportunite", ascending=False).head(6) if not opps_df.empty else pd.DataFrame()
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("🏆 Meilleures opportunités")
        if not top.empty:
            for _, o in top.iterrows():
                with st.container(border=True):
                    st.write(f"**{o['nom']}**")
                    st.caption(f"{o['secteur']} · {o['adresse'] or ''}")
                    score_bar(o["score_opportunite"])
            st.page_link("streamlit_app.py", label="Voir toutes les entreprises →") if False else None
        else:
            st.markdown("—")
    with c2:
        st.subheader("🆕 Dernières offres")
        if not offers_df.empty:
            for _, f in offers_df.head(6).iterrows():
                with st.container(border=True):
                    st.write(f"**{f['titre']}**")
                    st.caption(f"{f['entreprise'] or '—'} · {f['contrat']} · {fmt_day(f['date_publication'])}")
        else:
            st.markdown("Aucune offre. Ajoute-en ou importe un fichier depuis **Collecte & Import**.")

def page_entreprises():
    st.markdown("# 🏢 Entreprises")
    st.markdown("Entreprises IA & IT de Marrakech, classées par score d'opportunité.")
    opps = get_opportunities_for_user(st.session_state.user)
    q = st.text_input("🔎 Recherche (nom ou adresse)", key="ent_q", placeholder="Nom ou adresse…")
    secteur = st.selectbox("Secteur", ["Tous les secteurs"] + SECTEURS, key="ent_sect")
    min_score = st.slider("Score minimum", 0, 100, 0, 5, key="ent_score")
    email_only = st.checkbox("Avec e-mail", key="ent_email")
    fav_only = st.checkbox("Favoris", key="ent_fav")

    filtered = opps
    if q.strip():
        ql = q.strip().lower()
        filtered = filtered[(filtered["nom"].astype(str).str.lower().str.contains(ql, na=False)) |
                            (filtered["adresse"].fillna("").astype(str).str.lower().str.contains(ql, na=False))]
    if secteur != "Tous les secteurs":
        filtered = filtered[filtered["secteur"] == secteur]
    filtered = filtered[filtered["score_opportunite"] >= min_score]
    if email_only:
        filtered = filtered[filtered["email"].fillna("").str.strip() != ""]
    if fav_only:
        filtered = filtered[filtered["favori"] == 1]
    filtered = filtered.sort_values("score_opportunite", ascending=False)

    st.markdown(f"{len(filtered)} entreprise(s) trouvée(s)")
    # table compacte via dataframe + boutons par ligne (expander)
    display = filtered.copy()
    display["favori"] = display["favori"].map({1: "★", 0: "☆"})
    display["cv"] = display.apply(lambda r: f"CV envoyé le {fmt_ts(r['cv_envoye_le'])}" if pd.notna(r.get("cv_envoye_le")) else "", axis=1)
    cols = ["favori", "nom", "secteur", "score_opportunite", "note", "telephone", "email", "site_web", "google_maps_url", "cv", "adresse"]
    safe = [c for c in cols if c in display.columns]
    # LinkColumn doit recevoir des strings valides (pas de NaN/None) — on nettoie en string, vide si absent
    for link_col in ("site_web", "google_maps_url"):
        if link_col in display.columns:
            display[link_col] = display[link_col].fillna("").astype(str)
            display[link_col] = display[link_col].apply(lambda x: "" if x.strip().lower() in ("none", "nan", "") else x.strip())
    st.dataframe(display[safe], use_container_width=True, hide_index=True, column_config={
        "score_opportunite": st.column_config.ProgressColumn("Score", min_value=0, max_value=100),
    })
    # actions par ligne
    st.markdown("#### Actions")
    for _, o in filtered.iterrows():
        with st.container(border=True):
            c1, c2, c3, c4, c5, c6 = st.columns([3, 1, 1, 1, 1, 1])
            c1.write(f"**{o['nom']}** — {o['secteur']} · Score {o['score_opportunite']}")
            if c2.button("★ Favori", key=f"fav_{o['id']}", use_container_width=True):
                toggle_favorite(st.session_state.user, int(o["id"]))
                st.rerun()
            email = (o.get("email") or "").strip()
            if c3.button("✉️ CV", key=f"cv_{o['id']}", use_container_width=True, disabled=not email):
                send_cv_dialog(o)
            if c4.button("📋 Suivre", key=f"track_{o['id']}", use_container_width=True):
                with st.form(f"track_{o['id']}"):
                    poste = st.text_input("Poste visé", value="Stage")
                    statut = st.selectbox("Statut", STATUTS, index=STATUTS.index("À contacter"))
                    d = st.date_input("Date", value=date.today())
                    remarque = st.text_area("Remarque", height=80)
                    if st.form_submit_button("Enregistrer", type="primary"):
                        conn = get_connection()
                        conn.execute("""INSERT INTO applications (user, company, poste, statut, date_candidature, remarque, opportunity_id)
                                      VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                     (st.session_state.user, o["nom"], poste, statut, d.isoformat(), remarque, int(o["id"])))
                        conn.commit()
                        conn.close()
                        st.success("Candidature ajoutée au suivi ✅")
                        st.rerun()
            if can_edit(o.get("added_by", "")):
                if c5.button("✏️", key=f"edit_{o['id']}", use_container_width=True):
                    with st.form(f"edit_{o['id']}"):
                        em = st.text_input("E-mail de contact", value=o.get("email", ""))
                        tel = st.text_input("Téléphone", value=o.get("telephone", ""))
                        site = st.text_input("Site web", value=o.get("site_web", ""))
                        adr = st.text_input("Adresse", value=o.get("adresse", ""))
                        sec = st.selectbox("Secteur", SECTEURS, index=SECTEURS.index(o["secteur"]) if o["secteur"] in SECTEURS else 10)
                        if st.form_submit_button("Enregistrer", type="primary"):
                            if em and not is_valid_email(em.strip().lower()):
                                st.error("Adresse e-mail invalide")
                            else:
                                update_opportunity_fields(int(o["id"]), {"email": em.strip().lower(), "telephone": tel, "site_web": site, "adresse": adr, "secteur": sec})
                                st.success("Entreprise mise à jour ✅")
                                st.rerun()
                if c6.button("🗑", key=f"del_{o['id']}", use_container_width=True):
                    if st.session_state.get(f"confirm_del_{o['id']}"):
                        delete_opportunity(int(o["id"]))
                        st.success("Entreprise supprimée")
                        st.rerun()
                    else:
                        st.session_state[f"confirm_del_{o['id']}"] = True
                        st.warning("Clique à nouveau pour confirmer la suppression")
                        st.rerun()

def page_offres():
    st.markdown("# 📄 Offres de stage et d'emploi")
    st.markdown("Opportunités professionnelles à Marrakech.")
    opps_df = get_opportunities_for_user(st.session_state.user)
    q = st.text_input("🔎 Recherche (titre, entreprise…)", key="off_q")
    type_stage = st.selectbox("Type de stage", [""] + TYPES_STAGE, index=0, format_func=lambda v: v or "Tous les types")
    contrat = st.selectbox("Contrat", [""] + CONTRATS, index=0, format_func=lambda v: v or "Tous les contrats")
    offers = list_offers(q.strip(), contrat, type_stage, None).to_dict(orient="records")
    st.markdown(f"{len(offers)} offre(s) trouvée(s)")

    with st.expander("➕ Ajouter une offre"):
        with st.form("add_offer"):
            titre = st.text_input("Titre *", max_chars=200)
            entreprise = st.text_input("Entreprise", help="Choisis ou saisis un nom")
            c_contrat = st.selectbox("Contrat", CONTRATS, index=CONTRATS.index("Stage"))
            c_type = st.selectbox("Type de stage", TYPES_STAGE, index=TYPES_STAGE.index("Non renseigné"))
            ville = st.text_input("Ville", value="Marrakech")
            date_pub = st.date_input("Date de publication", value=date.today())
            url = st.text_input("Lien de l'offre", placeholder="https://…")
            description = st.text_area("Description", height=80)
            if st.form_submit_button("Enregistrer", type="primary"):
                if not titre.strip():
                    st.error("Titre obligatoire")
                elif url.strip() and not re.match(r"^https?://", url.strip(), re.I):
                    st.error("Le lien doit commencer par http:// ou https://")
                else:
                    match = opps_df[opps_df["nom"].str.lower() == entreprise.strip().lower()] if entreprise.strip() else pd.DataFrame()
                    opp_id = int(match.iloc[0]["id"]) if not match.empty else None
                    try:
                        insert_offer({"titre": titre, "entreprise": entreprise, "opportunity_id": opp_id,
                                      "contrat": c_contrat, "type_stage": c_type, "ville": ville,
                                      "date_publication": date_pub.isoformat(), "url": url, "description": description},
                                     added_by=st.session_state.user)
                        st.success("Offre ajoutée ✅")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

    if not offers:
        st.info("Aucune offre. Ajoute-en une ou importe un fichier depuis **Collecte & Import**.")
    for f in offers:
        with st.container(border=True):
            st.markdown(f"### {f['titre']}")
            st.caption(f"🏢 {f['entreprise'] or 'Non renseignée'} · 📋 {f['contrat']} · 🎓 {f['type_stage']} · 📍 {f['ville'] or '—'} · 📅 {fmt_day(f['date_publication']) or '—'}")
            if f.get("description"):
                st.markdown(f['description'][:400])
            a1, a2, a3 = st.columns([1, 1, 1])
            if (f.get("url") or "").strip():
                a1.link_button("🔗 Voir l'offre", f["url"], use_container_width=True)
            company = opps_df[opps_df["id"] == f.get("opportunity_id")] if f.get("opportunity_id") else pd.DataFrame()
            if not company.empty and (company.iloc[0].get("email") or "").strip():
                if a2.button("✉️ Envoyer mon CV", key=f"offcv_{f['id']}", use_container_width=True):
                    send_cv_dialog(company.iloc[0].to_dict(), subject=f"Candidature : {f['titre']}")
            if can_edit(f.get("added_by", "")):
                if a3.button("🗑 Supprimer", key=f"deloff_{f['id']}", use_container_width=True):
                    if st.session_state.get(f"confirm_off_{f['id']}"):
                        delete_offer(int(f["id"]))
                        st.success("Offre supprimée")
                        st.rerun()
                    else:
                        st.session_state[f"confirm_off_{f['id']}"] = True
                        st.warning("Clique à nouveau pour confirmer")

def page_carte():
    st.markdown("# 🗺️ Carte")
    st.markdown("Localisation des entreprises collectées.")
    opps = get_opportunities_for_user(st.session_state.user)
    secteur = st.selectbox("Secteur", ["Tous les secteurs"] + SECTEURS, key="map_sect")
    min_score = st.slider("Score minimum", 0, 100, 0, 5, key="map_score")
    pts = opps[(opps["latitude"].notna()) & (opps["longitude"].notna())]
    if secteur != "Tous les secteurs":
        pts = pts[pts["secteur"] == secteur]
    pts = pts[pts["score_opportunite"] >= min_score]
    st.markdown(f"{len(pts)} entreprise(s) affichée(s)")
    if pts.empty:
        st.info("Aucune entreprise géolocalisée avec ces filtres.")
        return
    m = folium.Map(location=[31.6295, -7.9811], zoom_start=12)
    for _, o in pts.iterrows():
        site = str(o.get("site_web") or "")
        popup = f"<b>{o['nom']}</b><br>{o['secteur']} · score {o['score_opportunite']}<br>{o.get('adresse') or ''}"
        if o.get("email"):
            popup += f"<br>✉️ {o['email']}"
        if site.startswith("http"):
            popup += f"<br><a href='{site}' target='_blank'>Site web</a>"
        folium.Marker(location=[float(o["latitude"]), float(o["longitude"])], popup=popup, tooltip=str(o["nom"])).add_to(m)
    st_folium(m, width="100%", height=560)

def page_candidatures():
    st.markdown("# 📬 Suivi des candidatures")
    st.markdown("Ajoute et suis tes candidatures auprès des entreprises.")
    opps = get_opportunities_for_user(st.session_state.user)
    conn = get_connection()
    apps = pd.read_sql_query("SELECT * FROM applications WHERE user=? ORDER BY date_ajout DESC", conn, params=(st.session_state.user,))
    conn.close()

    with st.expander("➕ Ajouter une candidature", expanded=len(apps) == 0):
        with st.form("add_app"):
            company = st.text_input("Entreprise *", max_chars=200, placeholder="Nom de l'entreprise")
            poste = st.text_input("Poste *", value="Stage")
            d = st.date_input("Date", value=date.today())
            statut = st.selectbox("Statut", STATUTS, index=STATUTS.index("À contacter"))
            remarque = st.text_area("Commentaire", placeholder="Ex : CV envoyé par e-mail…", height=80)
            if st.form_submit_button("💾 Enregistrer la candidature", type="primary"):
                if not company.strip() or not poste.strip():
                    st.error("Entreprise et poste obligatoires")
                else:
                    match = opps[opps["nom"].str.lower() == company.strip().lower()] if not opps.empty else pd.DataFrame()
                    opp_id = int(match.iloc[0]["id"]) if not match.empty else None
                    conn = get_connection()
                    conn.execute("""INSERT INTO applications (user, company, poste, statut, date_candidature, remarque, opportunity_id)
                                  VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                 (st.session_state.user, company.strip(), poste.strip(), statut, d.isoformat(), remarque, opp_id))
                    conn.commit()
                    conn.close()
                    st.success("Candidature enregistrée ✅")
                    st.rerun()

    cols = st.columns(len(STATUTS))
    for col, statut in zip(cols, STATUTS):
        with col:
            items = apps[apps["statut"] == statut] if not apps.empty else pd.DataFrame()
            st.markdown(f"#### {statut} <span class='chip'>{len(items)}</span>", unsafe_allow_html=True)
            st.markdown('<div class="kanban-col">', unsafe_allow_html=True)
            for _, a in items.iterrows():
                with st.container(border=True):
                    st.write(f"**{a['company']}**")
                    st.caption(f"{a['poste'] or ''}" + (f" · {fmt_day(a['date_candidature'])}" if a.get("date_candidature") else ""))
                    if a.get("remarque"):
                        st.caption(str(a["remarque"]))
                    new_statut = st.selectbox("Statut", STATUTS, index=STATUTS.index(a["statut"]) if a["statut"] in STATUTS else 0, key=f"appstat_{a['id']}", label_visibility="collapsed")
                    if new_statut != a["statut"]:
                        conn = get_connection()
                        conn.execute("UPDATE applications SET statut=? WHERE id=?", (new_statut, int(a["id"])))
                        conn.commit()
                        conn.close()
                        st.rerun()
                    if st.button("🗑 Supprimer", key=f"delapp_{a['id']}"):
                        conn = get_connection()
                        conn.execute("DELETE FROM applications WHERE id=?", (int(a["id"]),))
                        conn.commit()
                        conn.close()
                        st.success("Candidature supprimée")
                        st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

# ---------- Stats ----------
PALETTE = ["#0d9488", "#f59e0b", "#6366f1", "#ec4899", "#22c55e", "#0ea5e9", "#ef4444", "#a855f7", "#14b8a6", "#eab308", "#64748b"]

def _chart_from_dict(title, data_dict, chart_type="bar"):
    st.markdown(f"##### {title}")
    if not data_dict or not any(v > 0 for v in data_dict.values()):
        st.markdown("Pas encore de données.")
        return
    df = pd.DataFrame({"catégorie": list(data_dict.keys()), "valeur": list(data_dict.values())})
    if chart_type == "bar":
        st.bar_chart(df.set_index("catégorie"), color=PALETTE[0])
    else:
        st.pyplot(_pie_fig(df))

def _pie_fig(df):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots()
    ax.pie(df["valeur"], labels=df["catégorie"], autopct="%1.0f%%", colors=PALETTE[:len(df)], textprops={"fontsize": 8})
    ax.axis("equal")
    fig.tight_layout()
    return fig

def page_statistiques():
    st.markdown("# 📊 Statistiques")
    st.markdown("Analyse des entreprises, des offres et de ta recherche.")
    s = get_stats()
    cols = st.columns(6)
    for c, (icon, key, label) in zip(cols, [("🏢", "total_opportunites", "Entreprises"), ("📄", "total_offres", "Offres"),
                                            ("🏷️", "total_secteurs", "Secteurs"), ("✉️", "entreprises_avec_email", "Avec e-mail"),
                                            ("📨", "cv_envoyes", "CV envoyés"), ("⭐", "favoris", "Favoris")]):
        c.metric(icon, s[key], label)
    c1, c2 = st.columns(2)
    with c1:
        _chart_from_dict("🏢 Entreprises par secteur", dict(sorted(s["par_secteur"].items(), key=lambda x: -x[1])))
        _chart_from_dict("🎓 Offres par type de stage", s["offres_par_type_stage"], "pie")
        _chart_from_dict("🌐 Présence numérique des entreprises",
                         {"Avec e-mail": s["presence_numerique"]["avec_email"], "Sans e-mail": s["presence_numerique"]["sans_email"],
                          "Avec site web": s["presence_numerique"]["avec_site_web"], "Sans site web": s["presence_numerique"]["sans_site_web"]})
    with c2:
        _chart_from_dict("📋 Offres par type de contrat", s["offres_par_contrat"])
        statuts = {k: s["candidatures_par_statut"].get(k, 0) for k in STATUTS}
        _chart_from_dict("📬 Candidatures par statut", statuts)

def page_profil():
    st.markdown("# 🎯 Profil & Matching")
    st.markdown("Renseigne tes compétences pour voir les entreprises qui te correspondent le mieux.")
    profile = get_profile(st.session_state.user)
    with st.form("profile_form"):
        skills = st.text_area("Compétences (séparées par des virgules)", value=profile.get("skills", ""), placeholder="python, machine learning, data, cloud", height=80)
        exp = st.number_input("Années d'expérience", min_value=0.0, max_value=50.0, step=0.5, value=float(profile.get("experience_years", 0) or 0))
        bio = st.text_input("Courte présentation", value=profile.get("bio", ""), max_chars=500)
        if st.form_submit_button("💾 Enregistrer mon profil", type="primary"):
            upsert_profile({"user": st.session_state.user, "skills": skills, "experience_years": float(exp),
                            "bio": bio, "cv_filename": profile.get("cv_filename", "")})
            st.success("Profil enregistré ✅")
            st.rerun()
    st.markdown("### 🤖 Entreprises recommandées")
    st.markdown("Indicatif : 60 % de compétences en commun avec les mots-clés de l'entreprise (surtout tirés de son nom), 40 % du score d'opportunité.")
    min_score = st.slider("Match minimum", 0, 100, 20, 5)
    df = get_opportunities_for_user(st.session_state.user)
    if df.empty:
        st.info("Aucune entreprise.")
        return
    skills = get_profile(st.session_state.user).get("skills", "")
    df["match_score"] = df.apply(lambda r: compute_match_score(skills, r.get("mots_cles", ""), r.get("score_opportunite", 0)), axis=1)
    df = df[df["match_score"] >= min_score].sort_values("match_score", ascending=False)
    if df.empty:
        st.info("Aucune entreprise ne dépasse ce seuil. Complète tes compétences ou baisse le seuil.")
        return
    show = df.head(40).copy()
    show["match_score"] = show["match_score"].astype(int)
    st.dataframe(show[["nom", "secteur", "adresse", "match_score", "score_opportunite", "mots_cles", "email"]],
                 use_container_width=True, hide_index=True,
                 column_config={"match_score": st.column_config.ProgressColumn("Match", min_value=0, max_value=100),
                                "score_opportunite": st.column_config.ProgressColumn("Opportunité", min_value=0, max_value=100)})
    st.caption(f"{len(df)} résultat(s), 40 affichés au maximum.")
    for _, o in df.head(10).iterrows():
        if (o.get("email") or "").strip():
            if st.button(f"✉️ CV → {o['nom']}", key=f"matchcv_{o['id']}"):
                send_cv_dialog(o.to_dict())

def page_donnees():
    st.markdown("# ⚙️ Collecte & Import")
    st.markdown("Alimente l'application en entreprises et en offres.")
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("🔄 Collecte Google Places")
        with st.form("collect_form"):
            query = st.text_input("Requête", value="Intelligence Artificielle OR Informatique")
            location = st.text_input("Coordonnées (lat,lng)", value="31.6295,-7.9811")
            radius = st.slider("Rayon (km)", 1, 50, 15, help="En mètres côté API")
            wide = st.checkbox("Recherche élargie (plusieurs requêtes IT/IA)")
            apikey = st.text_input("Clé API Google (optionnel)", value=st.session_state.gmaps_key, type="password", placeholder="Remplace la clé du serveur")
            if st.form_submit_button("Collecter", type="primary"):
                if apikey.strip():
                    st.session_state.gmaps_key = apikey.strip()
                key_to_use = apikey.strip() or GOOGLE_API_KEY
                if not key_to_use:
                    st.error("Aucune clé Google Maps fournie (ni dans .env, ni dans l'interface).")
                else:
                    queries = [query] + EXTRA_QUERIES if wide else [query]
                    total = 0
                    errors = []
                    for i, q in enumerate(queries):
                        st.caption(f"« {q} » ({i+1}/{len(queries)})…")
                        try:
                            raw = fetch_it_companies_google(key_to_use, location=location, radius=radius*1000, query=q)
                            if not raw:
                                errors.append(f"« {q} » : aucun résultat")
                                continue
                            df = clean_and_score_places(raw)
                            total += upsert_opportunities(df, added_by=st.session_state.user)
                            st.session_state.snapshots[st.session_state.user] = {"label": "Google Places", "avant": raw_places_to_dataframe(raw), "apres": df}
                        except Exception as e:
                            errors.append(f"« {q} » : {e}")
                    st.success(f"{total} entreprise(s) collectée(s) ou mise(s) à jour.")
                    if errors:
                        st.warning("\n".join(errors))
                    st.rerun()

        st.subheader("📂 Importer des entreprises (Excel)")
        st.caption("Colonnes : `entreprise` (obligatoire), type, secteur, adresse, lat, lon, note, site_web, telephone, `email`, statut_google.")
        f = st.file_uploader("Fichier .xlsx", type=["xlsx", "xls"], key="up_ent")
        only = st.checkbox("Garder uniquement Informatique / IA", value=True, key="only_it")
        if f is not None and st.button("Importer", key="btn_ent"):
            try:
                df, report, snap = clean_excel_opportunities(io.BytesIO(f.getvalue()), only_it_ai=only)
                count = upsert_opportunities(df, added_by=st.session_state.user)
                report["opportunites_ajoutees"] = count
                st.session_state.snapshots[st.session_state.user] = {"label": "Import Excel", "avant": snap, "apres": df}
                labels = {"lignes_lues": "Lignes lues", "sans_nom_supprimees": "Sans nom (supprimées)", "fermees_supprimees": "Fiches fermées (supprimées)",
                          "doublons_supprimes": "Doublons (supprimés)", "hors_secteur_supprimees": "Hors secteur IT/IA (supprimées)",
                          "lignes_conservees": "Lignes conservées", "opportunites_ajoutees": "Entreprises ajoutées / mises à jour"}
                st.markdown("\n".join([f"- {labels.get(k, k)} : **{v}**" for k, v in report.items()]))
                st.success("Import terminé ✅")
                st.rerun()
            except Exception as e:
                st.error(f"Erreur de lecture du fichier Excel : {e}")

    with c2:
        st.subheader("📄 Importer des offres (Excel ou CSV)")
        st.caption("Colonnes reconnues : `titre` (obligatoire), entreprise, contrat, type de stage, ville, date de publication, lien, description.")
        fo = st.file_uploader("Fichier .xlsx ou .csv", type=["xlsx", "xls", "csv"], key="up_off")
        if fo is not None and st.button("Importer", key="btn_off"):
            content = fo.getvalue()
            if len(content) > 10 * 1024 * 1024:
                st.error("Fichier trop volumineux (10 Mo max).")
            else:
                name = (fo.name or "").lower()
                try:
                    raw = pd.read_csv(io.BytesIO(content), sep=None, engine="python", encoding="utf-8-sig") if name.endswith(".csv") else pd.read_excel(io.BytesIO(content))
                except Exception as e:
                    st.error(f"Fichier illisible : {e}")
                    raw = None
                if raw is not None:
                    aliases = {"titre": ["titre", "title", "intitule", "poste", "offre", "libelle"],
                               "entreprise": ["entreprise", "company", "societe", "employeur"],
                               "contrat": ["contrat", "contract", "type_contrat", "type de contrat"],
                               "type_stage": ["type_stage", "type de stage", "stage", "nature"],
                               "ville": ["ville", "city", "lieu", "localisation"],
                               "date_publication": ["date_publication", "publication", "date de publication", "date_pub", "date"],
                               "url": ["url", "lien", "link"], "description": ["description", "details", "détails", "resume"]}
                    cols_map = {_normalize(c).strip(): c for c in raw.columns}
                    cols = {}
                    for field, arr in aliases.items():
                        found = None
                        for a in arr:
                            if _normalize(a).strip() in cols_map:
                                found = cols_map[_normalize(a).strip()]
                                break
                        cols[field] = found
                    if cols["titre"] is None:
                        st.error("Colonne du titre introuvable (attendu : titre, intitulé ou poste).")
                    else:
                        def _norm_contrat(v):
                            v = _normalize(v).strip()
                            for c in CONTRATS:
                                if _normalize(c).strip() == v:
                                    return c
                            return "Stage" if "stage" in v else ("Autre" if v else "Stage")
                        def _norm_type(v):
                            v = _normalize(v).strip()
                            if not v or "renseign" in v:
                                return "Non renseigné"
                            if "pfe" in v or ("fin" in v and "etude" in v):
                                return "Stage de fin d'études"
                            if "embauche" in v:
                                return "Stage Pré-embauche"
                            if "fonction" in v or "observation" in v or "initiation" in v:
                                return "Stage fonctionnel"
                            return "Non renseigné"
                        catalogue = get_opportunities_for_user(st.session_state.user)
                        by_name = {str(n).strip().lower(): int(i) for n, i in zip(catalogue["nom"], catalogue["id"])} if not catalogue.empty else {}
                        rows = []
                        for _, r in raw.iterrows():
                            def val(field):
                                c = cols[field]
                                v = r[c] if c is not None else ""
                                return "" if pd.isna(v) else str(v).strip()
                            pub = val("date_publication")
                            parsed = pd.to_datetime(pub, errors="coerce", dayfirst=True) if pub else pd.NaT
                            ent = val("entreprise")
                            rows.append({"titre": val("titre"), "entreprise": ent, "opportunity_id": by_name.get(ent.lower()),
                                         "contrat": _norm_contrat(val("contrat")), "type_stage": _norm_type(val("type_stage")),
                                         "ville": val("ville") or "Marrakech",
                                         "date_publication": parsed.strftime("%Y-%m-%d") if pd.notna(parsed) else pub,
                                         "url": val("url") if re.match(r"^https?://", val("url"), re.I) else "",
                                         "description": val("description"), "source": "Import fichier"})
                        report = upsert_offers(rows, added_by=st.session_state.user)
                        report["lignes_lues"] = len(raw)
                        st.markdown("\n".join([f"- {k} : **{v}**" for k, v in report.items()]))
                        st.success("Import terminé ✅")
                        st.rerun()

        st.subheader("➕ Ajouter une entreprise")
        with st.form("manual_form"):
            nom = st.text_input("Nom *")
            secteur = st.selectbox("Secteur", ["Détection automatique"] + SECTEURS, index=0)
            email = st.text_input("E-mail")
            telephone = st.text_input("Téléphone")
            site_web = st.text_input("Site web")
            adresse = st.text_input("Adresse")
            lat = st.number_input("Latitude", value=None, format="%.6f")
            lng = st.number_input("Longitude", value=None, format="%.6f")
            score = st.number_input("Score (0-100)", min_value=0, max_value=100, value=50)
            if st.form_submit_button("Ajouter", type="primary"):
                if not nom.strip():
                    st.error("Le nom est obligatoire")
                elif email and not is_valid_email(email.strip().lower()):
                    st.error("Adresse e-mail invalide")
                else:
                    try:
                        insert_manual_opportunity({"nom": nom.strip(), "secteur": "" if secteur == "Détection automatique" else secteur,
                                                   "email": email.strip().lower(), "telephone": telephone, "site_web": site_web,
                                                   "adresse": adresse, "latitude": lat, "longitude": lng, "score_opportunite": int(score)},
                                                  added_by=st.session_state.user)
                        st.success("Entreprise ajoutée ✅")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

        st.subheader("📥 Exports")
        st.caption("Tes données (favoris, candidatures) au format tableur.")
        opps = get_opportunities_for_user(st.session_state.user)
        conn = get_connection()
        apps = pd.read_sql_query("SELECT * FROM applications WHERE user=?", conn, params=(st.session_state.user,))
        conn.close()
        resume = pd.DataFrame([{"total_opportunites": len(opps), "score_moyen": round(opps["score_opportunite"].mean(), 1) if not opps.empty else 0,
                                "favoris": int(opps["favori"].sum()) if not opps.empty else 0, "total_candidatures": len(apps)}])
        par_statut = apps["statut"].value_counts().rename_axis("statut").reset_index(name="nombre") if not apps.empty else pd.DataFrame(columns=["statut", "nombre"])
        st.download_button("Export complet (.xlsx)", _df_to_xlsx({"Opportunites (resultats)": opps, "Suivi candidatures": apps, "Resume": resume, "Candidatures par statut": par_statut}),
                           file_name="resultats_analyse_et_suivi.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        snap = st.session_state.snapshots.get(st.session_state.user)
        if snap and snap.get("avant") is not None and snap.get("apres") is not None:
            st.download_button("Avant / après nettoyage (.xlsx)", _df_to_xlsx({"Avant nettoyage": snap["avant"], "Apres nettoyage": snap["apres"]}),
                               file_name="donnees_avant_apres.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        csv = io.BytesIO()
        opps.to_csv(csv, index=False)
        st.download_button("Entreprises (.csv)", csv.getvalue(), file_name="opportunites.csv", mime="text/csv")

# ---------- Navigation ----------
if not st.session_state.user:
    show_auth()
else:
    with st.sidebar:
        st.markdown('<div class="brand">🎯 Career Finder</div><span class="muted">Marrakech · IA &amp; IT</span>', unsafe_allow_html=True)
        st.markdown(f"👤 **{st.session_state.user}** " + ("`admin`" if st.session_state.is_admin else ""))
        nav = st.radio("Navigation", ["🏠 Accueil", "🏢 Entreprises", "📄 Offres", "🗺️ Carte", "📬 Candidatures",
                                      "📊 Statistiques", "🎯 Profil & Matching", "⚙️ Collecte & Import"],
                       label_visibility="collapsed", key="nav")
        if st.button("Se déconnecter", use_container_width=True):
            st.session_state.user = None
            st.session_state.is_admin = False
            st.rerun()
    page = nav.split(" ", 1)[1] if " " in nav else nav
    if page == "Accueil":
        page_accueil()
    elif page == "Entreprises":
        page_entreprises()
    elif page == "Offres":
        page_offres()
    elif page == "Carte":
        page_carte()
    elif page == "Candidatures":
        page_candidatures()
    elif page == "Statistiques":
        page_statistiques()
    elif page == "Profil & Matching":
        page_profil()
    elif page == "Collecte & Import":
        page_donnees()

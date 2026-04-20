import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

const PROVIDER_OPTIONS = ["anthropic", "openai", "google", "mistral"];
const ENTITY_OPTIONS   = [
  "PERSON","EMAIL_ADDRESS","PHONE_NUMBER","IBAN_CODE","CREDIT_CARD",
  "LOCATION","DATE_TIME","IP_ADDRESS","URL","SIRET","VAT_NUMBER",
  "FR_NIR","SALARY","CONTRACT",
];

function fmt(n) { return Number(n || 0).toLocaleString("fr-FR"); }

const TOKEN_LABELS = {
  PERSON: "Personne", EMAIL_ADDRESS: "Email", PHONE_NUMBER: "Téléphone",
  IBAN_CODE: "IBAN", CREDIT_CARD: "Carte bancaire", LOCATION: "Lieu",
  DATE_TIME: "Date/heure", IP_ADDRESS: "IP", URL: "URL",
  SIRET: "SIRET", VAT_NUMBER: "N° TVA", FR_NIR: "NIR (sécu)",
  SALARY: "Salaire", CONTRACT: "Contrat",
};

function Toggle({ checked, onChange }) {
  return (
    <button
      onClick={() => onChange(!checked)}
      className={`relative w-10 h-5 rounded-full transition-colors duration-200 focus:outline-none ${checked ? "bg-proxy-accent" : "bg-proxy-card"}`}
    >
      <span
        className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform duration-200 ${checked ? "translate-x-5" : ""}`}
      />
    </button>
  );
}

function Section({ title, children }) {
  return (
    <div className="rounded-xl border border-proxy-card bg-proxy-panel p-5">
      <h2 className="font-display font-semibold text-base mb-4">{title}</h2>
      {children}
    </div>
  );
}

export default function OrgSettings() {
  const [org,       setOrg]       = useState(null);
  const [members,   setMembers]   = useState([]);
  const [settings,  setSettings]  = useState(null);
  const [loading,   setLoading]   = useState(true);
  const [error,     setError]     = useState(null);
  const [saved,     setSaved]     = useState(false);
  const [inviteUrl, setInviteUrl] = useState(null);
  const [inviting,  setInviting]  = useState(false);
  const inviteEmailRef = useRef();

  // sessionStorage uniquement : le token disparait a la fermeture de l'onglet.
  // Limitation connue : vulnerable au XSS comme tout stockage JS-accessible.
  // v2 : migrer vers httpOnly cookie (Set-Cookie: token=...; HttpOnly; Secure; SameSite=Strict)
  const token = sessionStorage.getItem("privacy_proxy_token");
  const hdrs  = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };

  async function load() {
    try {
      const [o, m] = await Promise.all([
        fetch(`${API_URL}/api/organizations/me`,      { headers: hdrs }).then(r => r.json()),
        fetch(`${API_URL}/api/organizations/members`, { headers: hdrs }).then(r => r.json()),
      ]);
      setOrg(o);
      setMembers(m);
      setSettings(o.settings ?? {
        allowed_providers: [...PROVIDER_OPTIONS],
        redaction_entities: [],
        max_tokens_per_user: 0,
      });
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  function toggleProvider(p) {
    setSettings(s => {
      const list = s.allowed_providers ?? [];
      return {
        ...s,
        allowed_providers: list.includes(p) ? list.filter(x => x !== p) : [...list, p],
      };
    });
  }

  function toggleEntity(e) {
    setSettings(s => {
      const list = s.redaction_entities ?? [];
      return {
        ...s,
        redaction_entities: list.includes(e) ? list.filter(x => x !== e) : [...list, e],
      };
    });
  }

  async function saveSettings() {
    try {
      await fetch(`${API_URL}/api/organizations/settings`, {
        method: "PUT",
        headers: hdrs,
        body: JSON.stringify(settings),
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      alert("Erreur : " + e.message);
    }
  }

  async function removeMember(userId) {
    if (!confirm("Retirer ce membre ?")) return;
    try {
      await fetch(`${API_URL}/api/organizations/members/${userId}`, {
        method: "DELETE",
        headers: hdrs,
      });
      setMembers(m => m.filter(x => x.user_id !== userId));
    } catch (e) {
      alert("Erreur : " + e.message);
    }
  }

  async function invite() {
    const email = inviteEmailRef.current?.value?.trim();
    if (!email) return;
    setInviting(true);
    try {
      const r = await fetch(`${API_URL}/api/organizations/invite`, {
        method: "POST",
        headers: hdrs,
        body: JSON.stringify({ email, role: "member" }),
      });
      const data = await r.json();
      setInviteUrl(data.token ? `${window.location.origin}/?invite=${data.token}` : data.detail ?? "Erreur");
    } catch (e) {
      setInviteUrl("Erreur : " + e.message);
    } finally {
      setInviting(false);
    }
  }

  if (loading) return (
    <div className="min-h-screen bg-proxy-bg flex items-center justify-center">
      <div className="w-8 h-8 rounded-full border-2 border-proxy-accent border-t-transparent animate-spin" />
    </div>
  );

  if (error) return (
    <div className="min-h-screen bg-proxy-bg flex items-center justify-center text-red-400 text-sm">
      Erreur : {error}
    </div>
  );

  const role = org?.role ?? "member";

  return (
    <div className="min-h-screen bg-proxy-bg text-proxy-text font-sans">
      {/* Top bar */}
      <header className="h-14 border-b border-proxy-card flex items-center px-6 gap-4">
        <Link to="/" className="text-proxy-muted hover:text-proxy-text transition-colors text-sm">
          ← Retour au chat
        </Link>
        <span className="text-proxy-card">|</span>
        <h1 className="font-display font-bold text-lg">
          Paramètres — {org?.name ?? "Organisation"}
        </h1>
        {role === "admin" && (
          <span className="ml-auto text-xs bg-proxy-accent/20 text-proxy-accent px-2 py-0.5 rounded-full border border-proxy-accent/30">
            Admin
          </span>
        )}
      </header>

      <main className="max-w-3xl mx-auto p-6 space-y-6">

        {/* Providers */}
        {role === "admin" && (
          <Section title="Fournisseurs LLM autorisés">
            <div className="grid grid-cols-2 gap-3">
              {PROVIDER_OPTIONS.map(p => (
                <label key={p} className="flex items-center justify-between rounded-lg bg-proxy-bg border border-proxy-card px-4 py-3 cursor-pointer hover:border-proxy-accent/40 transition-colors">
                  <span className="capitalize text-sm">{p}</span>
                  <Toggle
                    checked={(settings?.allowed_providers ?? []).includes(p)}
                    onChange={() => toggleProvider(p)}
                  />
                </label>
              ))}
            </div>
          </Section>
        )}

        {/* Entities */}
        {role === "admin" && (
          <Section title="Entités à pseudonymiser en priorité">
            <p className="text-xs text-proxy-muted mb-3">
              Laissez vide pour utiliser le jeu d&apos;entités par défaut. Cochez pour forcer la détection de certaines entités.
            </p>
            <div className="grid grid-cols-2 gap-2">
              {ENTITY_OPTIONS.map(e => (
                <label key={e} className="flex items-center gap-3 rounded-lg bg-proxy-bg border border-proxy-card px-3 py-2 cursor-pointer hover:border-proxy-accent/40 transition-colors">
                  <input
                    type="checkbox"
                    checked={(settings?.redaction_entities ?? []).includes(e)}
                    onChange={() => toggleEntity(e)}
                    className="accent-proxy-accent w-4 h-4"
                  />
                  <span className="text-sm">{TOKEN_LABELS[e] ?? e}</span>
                  <span className="ml-auto text-xs text-proxy-muted font-mono">{e}</span>
                </label>
              ))}
            </div>
          </Section>
        )}

        {/* Token quota */}
        {role === "admin" && (
          <Section title="Quota par utilisateur">
            <div className="flex items-center gap-4">
              <input
                type="number"
                value={settings?.max_tokens_per_user ?? 0}
                onChange={e => setSettings(s => ({ ...s, max_tokens_per_user: parseInt(e.target.value) || 0 }))}
                className="w-40 bg-proxy-bg border border-proxy-card rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-proxy-accent/60 transition-colors"
                min={0}
                step={1000}
              />
              <span className="text-proxy-muted text-sm">tokens/mois (0 = illimité)</span>
            </div>
          </Section>
        )}

        {/* Save button */}
        {role === "admin" && (
          <button
            onClick={saveSettings}
            className="px-6 py-2.5 bg-proxy-accent hover:bg-proxy-accent-light text-white rounded-lg text-sm font-semibold transition-colors duration-150"
          >
            {saved ? "✓ Enregistré" : "Enregistrer les paramètres"}
          </button>
        )}

        {/* Members */}
        <Section title={`Membres (${members.length})`}>
          <div className="space-y-2">
            {members.map(m => (
              <div key={m.user_id} className="flex items-center gap-3 rounded-lg bg-proxy-bg border border-proxy-card px-4 py-3">
                <div className="w-8 h-8 rounded-full bg-proxy-accent/20 flex items-center justify-center text-proxy-accent text-sm font-semibold">
                  {m.user_id[0]?.toUpperCase()}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium truncate">{m.user_id}</p>
                  <p className="text-xs text-proxy-muted">
                    {m.role} · {fmt(m.tokens_this_month ?? 0)} tokens ce mois
                  </p>
                </div>
                <span className={`text-xs px-2 py-0.5 rounded-full border ${
                  m.role === "admin"
                    ? "bg-proxy-accent/20 text-proxy-accent border-proxy-accent/30"
                    : "bg-proxy-card text-proxy-muted border-proxy-card"
                }`}>
                  {m.role}
                </span>
                {role === "admin" && m.role !== "admin" && (
                  <button
                    onClick={() => removeMember(m.user_id)}
                    className="text-proxy-muted hover:text-red-400 transition-colors text-xs px-2 py-1 rounded"
                  >
                    Retirer
                  </button>
                )}
              </div>
            ))}
          </div>
        </Section>

        {/* Invite */}
        {role === "admin" && (
          <Section title="Inviter un membre">
            <div className="flex gap-3">
              <input
                ref={inviteEmailRef}
                type="email"
                placeholder="email@exemple.com"
                className="flex-1 bg-proxy-bg border border-proxy-card rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-proxy-accent/60 transition-colors placeholder:text-proxy-muted"
              />
              <button
                onClick={invite}
                disabled={inviting}
                className="px-4 py-2 bg-proxy-accent hover:bg-proxy-accent-light text-white rounded-lg text-sm font-semibold transition-colors duration-150 disabled:opacity-50"
              >
                {inviting ? "…" : "Inviter"}
              </button>
            </div>
            {inviteUrl && (
              <div className="mt-3 rounded-lg bg-proxy-bg border border-proxy-accent/30 p-3">
                <p className="text-xs text-proxy-muted mb-1">Lien d&apos;invitation (valable 7 jours) :</p>
                <p className="text-xs font-mono text-proxy-accent break-all">{inviteUrl}</p>
              </div>
            )}
          </Section>
        )}

      </main>
    </div>
  );
}

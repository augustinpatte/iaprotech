import React, { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000"
const PROTECTION_STORAGE_PREFIX = "privacy-proxy:protection-mode:"
const MODEL_STORAGE_PREFIX = "privacy-proxy:model:"
const UPLOAD_PROTECTION_STORAGE_KEY = "privacy-proxy:upload-protection-mode"
const LANGS = ["fr", "en", "es"]
const PROVIDER_TABS = [
  { id: "openai", label: "OpenAI", envName: "OPENAI_API_KEY", placeholder: "sk-..." },
  { id: "anthropic", label: "Anthropic", envName: "ANTHROPIC_API_KEY", placeholder: "sk-ant-..." },
  { id: "google", label: "Gemini", envName: "GEMINI_API_KEY", placeholder: "AIza..." },
  { id: "mistral", label: "Mistral", envName: "MISTRAL_API_KEY", placeholder: "..." },
]
const VALID_MODEL_IDS = new Set([
  "claude-sonnet-4-5",
  "claude-opus-4-5",
  "gpt-4o",
  "gemini-1.5-pro",
  "mistral-large-latest",
])

function sectionCardStyle() {
  return {
    background: "var(--c-panel)",
    border: "1px solid var(--c-border)",
    borderRadius: 12,
    padding: "20px 22px",
  }
}

function Field({ label, children, hint }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <label style={{ fontSize: 12, color: "var(--c-text-muted)", fontWeight: 600 }}>{label}</label>
      {children}
      {hint ? <div style={{ fontSize: 11, color: "var(--c-text-faint)" }}>{hint}</div> : null}
    </div>
  )
}

function inputStyle(disabled = false) {
  return {
    width: "100%",
    padding: "10px 12px",
    borderRadius: 8,
    border: "1px solid var(--c-border)",
    background: disabled ? "var(--c-card)" : "var(--c-surface)",
    color: "var(--c-text)",
    fontSize: 14,
    outline: "none",
    opacity: disabled ? 0.8 : 1,
  }
}

function ActionButton({ children, onClick, tone = "primary", disabled = false }) {
  const palette = tone === "danger"
    ? { bg: "#3b0f12", bd: "#7f1d1d", fg: "#fca5a5" }
    : tone === "secondary"
      ? { bg: "var(--c-surface)", bd: "var(--c-border)", fg: "var(--c-text)" }
      : { bg: "linear-gradient(135deg,#1a6fcc,#2d8ff0)", bd: "transparent", fg: "#fff" }

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        padding: "10px 14px",
        borderRadius: 8,
        border: `1px solid ${palette.bd}`,
        background: palette.bg,
        color: palette.fg,
        fontSize: 13,
        fontWeight: 600,
        cursor: disabled ? "default" : "pointer",
        opacity: disabled ? 0.6 : 1,
      }}
    >
      {children}
    </button>
  )
}

function PasswordModal({ open, onClose, onSubmit, loading, error }) {
  const { t } = useTranslation()
  const [currentPassword, setCurrentPassword] = useState("")
  const [newPassword, setNewPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")

  useEffect(() => {
    if (!open) {
      setCurrentPassword("")
      setNewPassword("")
      setConfirmPassword("")
    }
  }, [open])

  if (!open) return null

  return (
    <div style={{ position: "fixed", inset: 0, background: "#00000088", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 200, padding: 24 }}>
      <div style={{ ...sectionCardStyle(), width: "100%", maxWidth: 460 }}>
        <div style={{ fontSize: 18, fontWeight: 700, color: "var(--c-text)", marginBottom: 18 }}>
          {t("settings.change_password")}
        </div>
        <div style={{ display: "grid", gap: 14 }}>
          <Field label={t("settings.current_password")}>
            <input type="password" value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} style={inputStyle()} />
          </Field>
          <Field label={t("settings.new_password")} hint={t("settings.password_hint")}>
            <input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} style={inputStyle()} />
          </Field>
          <Field label={t("settings.password_confirmation")}>
            <input type="password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} style={inputStyle()} />
          </Field>
          {error ? <div style={{ color: "#fca5a5", fontSize: 13 }}>{error}</div> : null}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
            <ActionButton tone="secondary" onClick={onClose} disabled={loading}>{t("common.cancel")}</ActionButton>
            <ActionButton
              onClick={() => onSubmit({ current_password: currentPassword, new_password: newPassword, confirm_password: confirmPassword })}
              disabled={loading}
            >
              {loading ? t("settings.updating") : t("settings.update")}
            </ActionButton>
          </div>
        </div>
      </div>
    </div>
  )
}

export default function Settings({ token, username }) {
  const { t, i18n } = useTranslation()
  const [profile, setProfile] = useState(null)
  const [org, setOrg] = useState(null)
  const [memberCount, setMemberCount] = useState(0)
  const [projects, setProjects] = useState([])
  const [models, setModels] = useState([])
  const [email, setEmail] = useState("")
  const [loading, setLoading] = useState(true)
  const [savingEmail, setSavingEmail] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [deletingProjects, setDeletingProjects] = useState(false)
  const [passwordModalOpen, setPasswordModalOpen] = useState(false)
  const [passwordSaving, setPasswordSaving] = useState(false)
  const [passwordError, setPasswordError] = useState("")
  const [status, setStatus] = useState("")
  const [invitations, setInvitations] = useState(null)
  const [inviteEmail, setInviteEmail] = useState("")
  const [inviteMessage, setInviteMessage] = useState("")
  const [sendingInvite, setSendingInvite] = useState(false)
  const [inviteLink, setInviteLink] = useState("")
  const [apiKeyTab, setApiKeyTab] = useState("openai")
  const [apiKeyDrafts, setApiKeyDrafts] = useState({
    openai: "",
    anthropic: "",
    google: "",
    mistral: "",
  })
  const [configuredApiKeys, setConfiguredApiKeys] = useState({
    openai: false,
    anthropic: false,
    google: false,
    mistral: false,
  })
  const [savingApiKeys, setSavingApiKeys] = useState(false)

  const authHeaders = useMemo(() => ({ Authorization: `Bearer ${token}` }), [token])
  const language = i18n.language?.slice(0, 2) || "fr"
  const protectionKey = `${PROTECTION_STORAGE_PREFIX}${username ?? "anonymous"}`
  const modelKey = `${MODEL_STORAGE_PREFIX}${username ?? "anonymous"}`
  const [protectionMode, setProtectionMode] = useState(() => localStorage.getItem(protectionKey) || "privacy")
  const [defaultModel, setDefaultModel] = useState(() => {
    const saved = localStorage.getItem(modelKey) || ""
    return saved && VALID_MODEL_IDS.has(saved) ? saved : ""
  })

  useEffect(() => {
    async function load() {
      setLoading(true)
      try {
        const [profileRes, orgRes, projectsRes, modelsRes, membersRes, invitesRes] = await Promise.allSettled([
          fetch(`${API_URL}/api/users/me`, { headers: authHeaders }),
          fetch(`${API_URL}/api/organizations/me`, { headers: authHeaders }),
          fetch(`${API_URL}/api/projects`, { headers: authHeaders }),
          fetch(`${API_URL}/api/models`, { headers: authHeaders }),
          fetch(`${API_URL}/api/organizations/members`, { headers: authHeaders }),
          fetch(`${API_URL}/api/invitations/my`, { headers: authHeaders }),
        ])

        if (profileRes.status === "fulfilled" && profileRes.value.ok) {
          const profileData = await profileRes.value.json()
          setProfile(profileData)
          setEmail(profileData.email || "")
          setConfiguredApiKeys({
            openai: Boolean(profileData.provider_api_keys_configured?.openai),
            anthropic: Boolean(profileData.provider_api_keys_configured?.anthropic),
            google: Boolean(profileData.provider_api_keys_configured?.google),
            mistral: Boolean(profileData.provider_api_keys_configured?.mistral),
          })
        }
        if (orgRes.status === "fulfilled" && orgRes.value.ok) setOrg(await orgRes.value.json())
        if (projectsRes.status === "fulfilled" && projectsRes.value.ok) {
          const projectsData = await projectsRes.value.json()
          setProjects(projectsData.projects || [])
        }
        if (modelsRes.status === "fulfilled" && modelsRes.value.ok) {
          const modelsData = await modelsRes.value.json()
          const available = Object.entries(modelsData.models || {}).map(([id, meta]) => ({
            id,
            label: `${id} (${meta.provider})`,
          }))
          setModels(available)
        }
        if (membersRes.status === "fulfilled" && membersRes.value.ok) {
          const membersData = await membersRes.value.json()
          setMemberCount(Array.isArray(membersData) ? membersData.length : (membersData.members?.length || 0))
        }
        if (invitesRes.status === "fulfilled" && invitesRes.value.ok) {
          setInvitations(await invitesRes.value.json())
        } else {
          setInvitations(null)
        }

        if (
          profileRes.status !== "fulfilled" || !profileRes.value.ok
        ) {
          setStatus(t("settings.load_partial_warning"))
        }
      } finally {
        setLoading(false)
      }
    }

    load()
  }, [authHeaders, t])

  function setLang(nextLang) {
    i18n.changeLanguage(nextLang)
    localStorage.setItem("language", nextLang)
  }

  function setProtection(nextMode) {
    setProtectionMode(nextMode)
    localStorage.setItem(protectionKey, nextMode)
    localStorage.setItem(UPLOAD_PROTECTION_STORAGE_KEY, nextMode)
  }

  function setModelPreference(nextModel) {
    const safeModel = nextModel && VALID_MODEL_IDS.has(nextModel) ? nextModel : ""
    setDefaultModel(safeModel)
    if (safeModel) localStorage.setItem(modelKey, safeModel)
    else localStorage.removeItem(modelKey)
  }

  async function saveEmail() {
    setSavingEmail(true)
    setStatus("")
    try {
      const res = await fetch(`${API_URL}/api/users/me`, {
        method: "PATCH",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`)
      setProfile(data)
      setStatus(t("settings.email_updated"))
    } catch (err) {
      setStatus(err.message)
    } finally {
      setSavingEmail(false)
    }
  }

  function updateApiKeyDraft(provider, value) {
    setApiKeyDrafts((prev) => ({ ...prev, [provider]: value }))
  }

  async function saveApiKeys() {
    setSavingApiKeys(true)
    setStatus("")
    try {
      const res = await fetch(`${API_URL}/api/users/me`, {
        method: "PATCH",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({ provider_api_keys: apiKeyDrafts }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`)
      setProfile(data)
      setConfiguredApiKeys({
        openai: Boolean(data.provider_api_keys_configured?.openai),
        anthropic: Boolean(data.provider_api_keys_configured?.anthropic),
        google: Boolean(data.provider_api_keys_configured?.google),
        mistral: Boolean(data.provider_api_keys_configured?.mistral),
      })
      setApiKeyDrafts({
        openai: "",
        anthropic: "",
        google: "",
        mistral: "",
      })
      setStatus("Clés API enregistrées. Elles restent masquées dans l'interface.")
    } catch (err) {
      setStatus(err.message)
    } finally {
      setSavingApiKeys(false)
    }
  }

  async function submitPassword(payload) {
    setPasswordSaving(true)
    setPasswordError("")
    try {
      const res = await fetch(`${API_URL}/api/users/me/change-password`, {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      })
      const data = await res.json()
      if (!res.ok) {
        const detail = typeof data.detail === "string"
          ? data.detail
          : (data.detail?.message || `HTTP ${res.status}`)
        throw new Error(detail)
      }
      setPasswordModalOpen(false)
      setStatus(t("settings.password_updated"))
    } catch (err) {
      setPasswordError(err.message)
    } finally {
      setPasswordSaving(false)
    }
  }

  async function exportData() {
    setExporting(true)
    setStatus("")
    try {
      const res = await fetch(`${API_URL}/api/users/me/export`, { headers: authHeaders })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail ?? `HTTP ${res.status}`)
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement("a")
      anchor.href = url
      anchor.download = `${username || "export"}_privacy_proxy_export.json`
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(url)
    } catch (err) {
      setStatus(err.message)
    } finally {
      setExporting(false)
    }
  }

  async function deleteAllProjects() {
    if (!window.confirm(t("settings.delete_projects_confirm"))) return
    setDeletingProjects(true)
    setStatus("")
    try {
      const res = await fetch(`${API_URL}/api/users/me/projects`, {
        method: "DELETE",
        headers: authHeaders,
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`)
      setProjects([])
      setStatus(t("settings.projects_deleted", { count: data.deleted_projects ?? 0 }))
    } catch (err) {
      setStatus(err.message)
    } finally {
      setDeletingProjects(false)
    }
  }

  async function sendInvitation() {
    setSendingInvite(true)
    setStatus("")
    setInviteLink("")
    try {
      const res = await fetch(`${API_URL}/api/invitations/send`, {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({ email: inviteEmail, message: inviteMessage }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`)
      setInviteLink(data.invite_link || "")
      setInviteEmail("")
      setInviteMessage("")
      setInvitations((prev) => prev ? {
        ...prev,
        invitations_used: data.invitations_used,
        max_invitations: data.max_invitations,
        items: [
          {
            token: data.token,
            email: inviteEmail,
            message: inviteMessage,
            status: "pending",
            created_at: new Date().toISOString(),
            invite_link: data.invite_link,
          },
          ...(prev.items || []),
        ],
      } : prev)
    } catch (err) {
      setStatus(err.message)
    } finally {
      setSendingInvite(false)
    }
  }

  async function cancelInvitation(token) {
    setStatus("")
    try {
      const res = await fetch(`${API_URL}/api/invitations/${token}`, {
        method: "DELETE",
        headers: authHeaders,
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`)
      setInvitations((prev) => prev ? {
        ...prev,
        invitations_used: Math.max(0, (prev.invitations_used || 0) - 1),
        items: (prev.items || []).filter((item) => item.token !== token),
      } : prev)
    } catch (err) {
      setStatus(err.message)
    }
  }

  if (loading) {
    return <div style={{ padding: 32, color: "var(--c-text-muted)" }}>{t("settings.loading")}</div>
  }

  const activeProvider = PROVIDER_TABS.find((provider) => provider.id === apiKeyTab) || PROVIDER_TABS[0]
  const currentApiKeyValue = apiKeyDrafts[activeProvider.id] || ""
  const isCurrentProviderConfigured = Boolean(configuredApiKeys[activeProvider.id])

  return (
    <div style={{ flex: 1, overflowY: "auto", background: "var(--c-bg)", padding: "24px 32px" }}>
      <div style={{ marginBottom: 24 }}>
        <h1 className="font-title" style={{ fontSize: 24, fontWeight: 700, color: "var(--c-text)", marginBottom: 6 }}>
          {t("settings.title")}
        </h1>
        <div style={{ color: "var(--c-text-faint)", fontSize: 13 }}>
          {t("settings.subtitle")}
        </div>
      </div>

      {status ? (
        <div style={{ marginBottom: 16, padding: "10px 14px", borderRadius: 8, background: "var(--c-card)", border: "1px solid var(--c-border)", color: "var(--c-text)" }}>
          {status}
        </div>
      ) : null}

      <div style={{ display: "grid", gap: 18 }}>
        <section style={sectionCardStyle()}>
          <div style={{ fontSize: 17, fontWeight: 700, color: "var(--c-text)", marginBottom: 16 }}>{t("settings.account_section")}</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2,minmax(0,1fr))", gap: 16 }}>
            <Field label={t("settings.username")}>
              <input value={profile?.username || ""} readOnly style={inputStyle(true)} />
            </Field>
            <Field label={t("settings.created_at")}>
              <input value={profile?.created_at ? new Date(profile.created_at).toLocaleString() : ""} readOnly style={inputStyle(true)} />
            </Field>
            <Field label={t("settings.email")}>
              <input value={email} onChange={(e) => setEmail(e.target.value)} style={inputStyle()} />
            </Field>
            <Field label={t("settings.password")}>
              <div style={{ display: "flex", alignItems: "center", height: "100%" }}>
                <ActionButton tone="secondary" onClick={() => setPasswordModalOpen(true)}>{t("settings.change_password")}</ActionButton>
              </div>
            </Field>
          </div>
          <div style={{ marginTop: 14 }}>
            <ActionButton onClick={saveEmail} disabled={savingEmail}>{savingEmail ? t("settings.saving") : t("settings.save_email")}</ActionButton>
          </div>
        </section>

        <section style={sectionCardStyle()}>
          <div style={{ fontSize: 17, fontWeight: 700, color: "var(--c-text)", marginBottom: 16 }}>{t("settings.preferences_section")}</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2,minmax(0,1fr))", gap: 16 }}>
            <Field label={t("topbar.language")}>
              <select value={language} onChange={(e) => setLang(e.target.value)} style={inputStyle()}>
                {LANGS.map((lang) => <option key={lang} value={lang}>{lang.toUpperCase()}</option>)}
              </select>
            </Field>
            <Field label={t("settings.default_protection")}>
              <select value={protectionMode} onChange={(e) => setProtection(e.target.value)} style={inputStyle()}>
                <option value="fast">{t("protection.fast")}</option>
                <option value="privacy">{t("protection.privacy")}</option>
                <option value="strict">{t("protection.strict")}</option>
              </select>
            </Field>
            <Field label={t("settings.default_model")}>
              <select value={defaultModel} onChange={(e) => setModelPreference(e.target.value)} style={inputStyle()}>
                <option value="">{t("models.auto")}</option>
                {models.map((model) => <option key={model.id} value={model.id}>{model.label}</option>)}
              </select>
            </Field>
            <Field label={t("settings.theme")}>
              <select value="dark" disabled style={inputStyle(true)}>
                <option value="dark">{t("settings.theme_dark")}</option>
              </select>
            </Field>
          </div>
        </section>

        <section style={sectionCardStyle()}>
          <div style={{ fontSize: 17, fontWeight: 700, color: "var(--c-text)", marginBottom: 8 }}>Clés API providers</div>
          <div style={{ color: "var(--c-text-faint)", fontSize: 13, marginBottom: 16 }}>
            Renseigne ici les clés que tu récupéreras plus tard. Elles sont sauvegardées côté backend et ne sont plus réaffichées en clair.
          </div>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 16 }}>
            {PROVIDER_TABS.map((provider) => {
              const active = provider.id === activeProvider.id
              const configured = Boolean(configuredApiKeys[provider.id])
              return (
                <button
                  key={provider.id}
                  onClick={() => setApiKeyTab(provider.id)}
                  style={{
                    padding: "10px 14px",
                    borderRadius: 999,
                    border: active ? "1px solid #2d8ff0" : "1px solid var(--c-border)",
                    background: active ? "rgba(45,143,240,0.14)" : "var(--c-surface)",
                    color: "var(--c-text)",
                    fontSize: 13,
                    fontWeight: 600,
                    cursor: "pointer",
                  }}
                >
                  {provider.label}{configured ? " · configurée" : ""}
                </button>
              )
            })}
          </div>
          <div style={{ display: "grid", gap: 14 }}>
            <Field
              label={`${activeProvider.label} (${activeProvider.envName})`}
              hint={isCurrentProviderConfigured ? "Une clé est déjà enregistrée pour ce provider. Saisis-en une nouvelle seulement si tu veux la remplacer." : "Aucune clé enregistrée pour ce provider pour l'instant."}
            >
              <input
                type="password"
                value={currentApiKeyValue}
                onChange={(e) => updateApiKeyDraft(activeProvider.id, e.target.value)}
                placeholder={activeProvider.placeholder}
                style={inputStyle()}
              />
            </Field>
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
              <ActionButton onClick={saveApiKeys} disabled={savingApiKeys}>
                {savingApiKeys ? "Enregistrement..." : "Enregistrer les clés API"}
              </ActionButton>
              <ActionButton
                tone="secondary"
                onClick={() => setApiKeyDrafts({ openai: "", anthropic: "", google: "", mistral: "" })}
                disabled={savingApiKeys}
              >
                Vider la saisie
              </ActionButton>
            </div>
          </div>
        </section>

        <section style={sectionCardStyle()}>
          <div style={{ fontSize: 17, fontWeight: 700, color: "var(--c-text)", marginBottom: 16 }}>{t("settings.organization_section")}</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2,minmax(0,1fr))", gap: 16 }}>
            <Field label={t("settings.organization_name")}>
              <input value={org?.name || t("settings.none")} readOnly style={inputStyle(true)} />
            </Field>
            <Field label={t("settings.my_role")}>
              <input value={org?.my_role || profile?.role || ""} readOnly style={inputStyle(true)} />
            </Field>
            <Field label={t("settings.current_plan")}>
              <input value={org?.plan || ""} readOnly style={inputStyle(true)} />
            </Field>
            <Field label={t("settings.member_count")}>
              <input value={String(memberCount)} readOnly style={inputStyle(true)} />
            </Field>
          </div>
          <div style={{ marginTop: 16, padding: "12px 14px", borderRadius: 8, border: "1px solid var(--c-border)", background: "var(--c-surface)" }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: "var(--c-text)", marginBottom: 6 }}>
              Plan : {profile?.plan || t("settings.none")}
            </div>
            <div style={{ fontSize: 12, color: "var(--c-text-faint)", display: "grid", gap: 4 }}>
              <div>Tokens ce mois : {(profile?.tokens_month || 0).toLocaleString()} / {(profile?.plan_limits?.max_tokens_month || 0).toLocaleString()}</div>
              <div>Invitations : {profile?.invitations_used || 0} / {profile?.max_invitations || 0} utilisées</div>
              <div>Modèles disponibles : {Array.isArray(profile?.plan_limits?.models_allowed) ? profile.plan_limits.models_allowed.join(", ") : "Tous"}</div>
              <div>Taille max fichier : {profile?.plan_limits?.max_file_size_mb || 0} Mo</div>
              <div>Modes de protection : {(profile?.plan_limits?.protection_modes || []).join(", ")}</div>
            </div>
          </div>
        </section>

        <section style={sectionCardStyle()}>
          <div style={{ fontSize: 17, fontWeight: 700, color: "var(--c-text)", marginBottom: 16 }}>{t("settings.data_section")}</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2,minmax(0,1fr))", gap: 16, marginBottom: 16 }}>
            <Field label={t("settings.my_projects")}>
              <input value={String(projects.length)} readOnly style={inputStyle(true)} />
            </Field>
            <Field label={t("settings.retention_policy")}>
              <input value={t("settings.retention_value", { days: profile?.retention_days ?? 0 })} readOnly style={inputStyle(true)} />
            </Field>
          </div>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            <ActionButton onClick={exportData} disabled={exporting}>{exporting ? t("settings.exporting") : t("settings.export_data")}</ActionButton>
            <ActionButton tone="danger" onClick={deleteAllProjects} disabled={deletingProjects}>
              {deletingProjects ? t("settings.deleting") : t("settings.delete_projects")}
            </ActionButton>
          </div>
        </section>

        <section style={sectionCardStyle()}>
          <div style={{ fontSize: 17, fontWeight: 700, color: "var(--c-text)", marginBottom: 16 }}>Invitations</div>
          {profile?.plan ? (
            <>
              <div style={{ marginBottom: 14, color: "var(--c-text-muted)", fontSize: 13 }}>
                Invitations disponibles : {Math.max(0, (invitations?.max_invitations ?? profile?.max_invitations ?? 0) - (invitations?.invitations_used ?? profile?.invitations_used ?? 0))} / {invitations?.max_invitations ?? profile?.max_invitations ?? 0}
              </div>
              <div style={{ display: "grid", gap: 12, marginBottom: 14 }}>
                <Field label="Email invité">
                  <input value={inviteEmail} onChange={(e) => setInviteEmail(e.target.value)} style={inputStyle()} />
                </Field>
                <Field label="Message (optionnel)">
                  <textarea value={inviteMessage} onChange={(e) => setInviteMessage(e.target.value)} style={{ ...inputStyle(), minHeight: 88, resize: "vertical" }} />
                </Field>
                <div>
                  <ActionButton onClick={sendInvitation} disabled={sendingInvite || !inviteEmail}>
                    {sendingInvite ? "Envoi..." : "Inviter quelqu'un"}
                  </ActionButton>
                </div>
                {inviteLink && (
                  <div style={{ padding: "12px 14px", borderRadius: 8, background: "var(--c-card)", border: "1px solid var(--c-border)", fontSize: 12, color: "var(--c-text)" }}>
                    {inviteLink}
                  </div>
                )}
              </div>
              <div style={{ display: "grid", gap: 10 }}>
                {(invitations?.items || []).map((item) => (
                  <div key={item.token} style={{ border: "1px solid var(--c-border)", borderRadius: 8, padding: "12px 14px", background: "var(--c-surface)", display: "flex", justifyContent: "space-between", gap: 16 }}>
                    <div>
                      <div style={{ color: "var(--c-text)", fontSize: 13, fontWeight: 600 }}>{item.email}</div>
                      <div style={{ color: "var(--c-text-faint)", fontSize: 12 }}>
                        {item.created_at ? new Date(item.created_at).toLocaleString() : "-"} · {item.status}
                      </div>
                    </div>
                    {item.status === "pending" && (
                      <ActionButton tone="danger" onClick={() => cancelInvitation(item.token)}>Annuler</ActionButton>
                    )}
                  </div>
                ))}
              </div>
            </>
          ) : (
            <div style={{ color: "var(--c-text-muted)", fontSize: 13 }}>
              Vous n'avez pas encore de plan actif. Contactez l'administrateur.
            </div>
          )}
        </section>
      </div>

      <PasswordModal
        open={passwordModalOpen}
        onClose={() => setPasswordModalOpen(false)}
        onSubmit={submitPassword}
        loading={passwordSaving}
        error={passwordError}
      />
    </div>
  )
}

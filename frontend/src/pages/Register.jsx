import React, { useEffect, useState } from "react"
import { Link, useSearchParams } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { API_URL, ShieldSvg, Spinner } from "../App.jsx"

export default function Register() {
  const { t } = useTranslation()
  const [searchParams] = useSearchParams()
  const [username, setUsername] = useState("")
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [confirm, setConfirm] = useState("")
  const [inviteInfo, setInviteInfo] = useState("")
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(false)
  const [created, setCreated] = useState(false)
  const inviteToken = searchParams.get("token") || ""

  useEffect(() => {
    if (!inviteToken) return
    setInviteInfo(`Invitation token: ${inviteToken.slice(0, 8)}...`)
  }, [inviteToken])

  async function submit(e) {
    e.preventDefault()
    setError("")
    if (password !== confirm) {
      setError(t("login.password_mismatch"))
      return
    }

    setLoading(true)
    try {
      const response = await fetch(`${API_URL}/api/auth/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username,
          email,
          password,
          invite_token: inviteToken || null,
        }),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        const detail = typeof data.detail === "string"
          ? data.detail
          : (data.detail?.message || data.message || `HTTP ${response.status}`)
        throw new Error(detail)
      }
      setCreated(true)
    } catch (submitError) {
      setError(submitError.message)
    } finally {
      setLoading(false)
    }
  }

  if (created) {
    return (
      <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--c-bg)" }}>
        <div style={{ width: 520, padding: 40, background: "var(--c-surface)", borderRadius: 16, border: "1px solid var(--c-border)" }}>
          <div style={{ fontSize: 28, marginBottom: 12 }}>✅</div>
          <h1 className="font-title" style={{ fontSize: 22, color: "var(--c-text)", marginBottom: 12 }}>
            Compte créé avec succès !
          </h1>
          <p style={{ color: "var(--c-text-muted)", fontSize: 14, lineHeight: 1.6, marginBottom: 18 }}>
            Votre demande d'accès a été transmise à l'administrateur.
          </p>
          <p style={{ color: "var(--c-text-muted)", fontSize: 14, lineHeight: 1.6, marginBottom: 24 }}>
            Vous recevrez un accès dès validation.
          </p>
          <Link to="/" style={{ color: "var(--c-accent-lt)", textDecoration: "none", fontWeight: 600 }}>
            Retour à la connexion
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--c-bg)" }}>
      <form onSubmit={submit} style={{ width: 420, padding: 40, background: "var(--c-surface)", borderRadius: 16, border: "1px solid var(--c-border)" }}>
        <div style={{ textAlign: "center", marginBottom: 28 }}>
          <ShieldSvg size={48} className="shield-glow" />
          <h1 className="font-title" style={{ fontSize: 22, fontWeight: 700, color: "var(--c-text)", marginTop: 12 }}>
            Créer mon compte
          </h1>
          <p style={{ color: "var(--c-text-muted)", fontSize: 13, marginTop: 4 }}>
            Accès soumis à validation administrateur.
          </p>
        </div>

        {inviteInfo && (
          <div style={{ marginBottom: 16, padding: "10px 14px", borderRadius: 8, background: "var(--c-card)", border: "1px solid var(--c-border)", color: "var(--c-text-muted)", fontSize: 12 }}>
            {inviteInfo}
          </div>
        )}
        {error && (
          <div style={{ marginBottom: 16, padding: "10px 14px", borderRadius: 8, background: "#2a0e0e", border: "1px solid #7f1d1d", color: "#fca5a5", fontSize: 13 }}>
            {error}
          </div>
        )}

        <label style={labelStyle}>Username</label>
        <input value={username} onChange={(e) => setUsername(e.target.value)} required style={inputStyle} />

        <label style={labelStyle}>Email</label>
        <input value={email} onChange={(e) => setEmail(e.target.value)} required type="email" style={inputStyle} />

        <label style={labelStyle}>Mot de passe</label>
        <input value={password} onChange={(e) => setPassword(e.target.value)} required type="password" style={inputStyle} />

        <label style={labelStyle}>Confirmation</label>
        <input value={confirm} onChange={(e) => setConfirm(e.target.value)} required type="password" style={{ ...inputStyle, marginBottom: 20 }} />

        <button type="submit" disabled={loading} style={submitStyle}>
          {loading ? <Spinner size={16} /> : null}
          {loading ? "Création..." : "Créer mon compte"}
        </button>

        <div style={{ marginTop: 18, textAlign: "center" }}>
          <Link to="/" style={{ color: "var(--c-text-muted)", textDecoration: "none", fontSize: 13 }}>
            Retour à la connexion
          </Link>
        </div>
      </form>
    </div>
  )
}

const labelStyle = {
  display: "block",
  marginBottom: 4,
  color: "var(--c-text-muted)",
  fontSize: 12,
  fontWeight: 500,
}

const inputStyle = {
  width: "100%",
  marginBottom: 14,
  padding: "10px 14px",
  background: "var(--c-panel)",
  border: "1px solid var(--c-border)",
  borderRadius: 8,
  color: "var(--c-text)",
  fontSize: 14,
  outline: "none",
}

const submitStyle = {
  width: "100%",
  padding: "11px 0",
  background: "linear-gradient(135deg,#1a6fcc,#2d8ff0)",
  border: "none",
  borderRadius: 8,
  color: "#fff",
  fontFamily: "var(--font-title)",
  fontWeight: 700,
  fontSize: 15,
  cursor: "pointer",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 8,
}

import React, { useState, useCallback, useEffect } from "react"
import { useTranslation } from "react-i18next"
import { Routes, Route, Navigate, useNavigate, Link } from "react-router-dom"
import TopBar from "./components/TopBar.jsx"
import ProjectSidebar from "./components/ProjectSidebar.jsx"
import RightPanel from "./components/RightPanel.jsx"
import ChatInterface from "./components/ChatInterface.jsx"
import Dashboard from "./pages/Dashboard.jsx"
import AdminDashboard from "./pages/AdminDashboard.jsx"
import UserDashboard from "./pages/UserDashboard.jsx"
import Settings from "./pages/Settings.jsx"
import Register from "./pages/Register.jsx"

export const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000"
// TOKEN_KEY : cle utilisee dans sessionStorage pour stocker le JWT.
// sessionStorage : persiste uniquement le temps de l'onglet (pas entre onglets).
// Limitation connue : accessible via JS => vulnerable au XSS.
// v2 : remplacer par un httpOnly cookie (non lisible en JS) cote backend.
export const TOKEN_KEY = "privacy_proxy_token"

export function ShieldSvg({ size = 28, className = "" }) {
  return (
    <svg width={size} height={size} viewBox="0 0 28 28" fill="none" className={className}>
      <path d="M14 2L4 6.5V13c0 6.075 4.3 11.752 10 13.25C19.7 24.752 24 19.075 24 13V6.5L14 2Z"
        fill="#1a6fcc" fillOpacity=".18" stroke="#2d8ff0" strokeWidth="1.5" strokeLinejoin="round"/>
      <path d="M10 14l2.5 2.5L18 11" stroke="#2d8ff0" strokeWidth="1.8"
        strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  )
}

export function Spinner({ size = 18 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      className="animate-spin" style={{ display: "inline-block" }}>
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" opacity=".2"/>
      <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" strokeWidth="3"
        strokeLinecap="round"/>
    </svg>
  )
}

function decodeJwt(token) {
  try {
    const b64 = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")
    return JSON.parse(atob(b64))
  } catch { return {} }
}

// -- Bootstrap ---------------------------------------------------------------
function BootstrapPage({ onBootstrapped }) {
  const { t } = useTranslation()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [confirm, setConfirm]   = useState("")
  const [error, setError]       = useState("")
  const [loading, setLoading]   = useState(false)

  async function submit(e) {
    e.preventDefault()
    if (password !== confirm) { setError(t("login.password_mismatch")); return }
    if (password.length < 8) { setError(t("login.password_min")); return }
    setLoading(true); setError("")
    try {
      const res = await fetch(`${API_URL}/api/auth/bootstrap`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      })
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail ?? `Erreur ${res.status}`) }
      onBootstrapped()
    } catch (err) { setError(err.message) }
    finally { setLoading(false) }
  }

  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--c-bg)" }}>
      <form onSubmit={submit} style={{ width: 380, padding: 40, background: "var(--c-surface)", borderRadius: 16, border: "1px solid var(--c-border)" }}>
        <div style={{ textAlign: "center", marginBottom: 32 }}>
          <ShieldSvg size={48} className="shield-glow" />
          <h1 className="font-title" style={{ fontSize: 22, fontWeight: 700, color: "var(--c-text)", marginTop: 12 }}>{t("bootstrap.title")}</h1>
          <p style={{ color: "var(--c-text-muted)", fontSize: 13, marginTop: 4 }}>{t("bootstrap.subtitle")}</p>
        </div>
        {error && (
          <div style={{ background: "#2a0e0e", border: "1px solid #7f1d1d", borderRadius: 8, padding: "10px 14px", color: "#fca5a5", fontSize: 13, marginBottom: 16 }}>
            {error}
          </div>
        )}
        <label style={{ display: "block", marginBottom: 4, color: "var(--c-text-muted)", fontSize: 12, fontWeight: 500 }}>{t("login.username")}</label>
        <input value={username} onChange={e => setUsername(e.target.value)} autoFocus required
          style={{ width: "100%", marginBottom: 14, padding: "10px 14px", background: "var(--c-panel)", border: "1px solid var(--c-border)", borderRadius: 8, color: "var(--c-text)", fontSize: 14, outline: "none" }} />
        <label style={{ display: "block", marginBottom: 4, color: "var(--c-text-muted)", fontSize: 12, fontWeight: 500 }}>{t("login.password")}</label>
        <input type="password" value={password} onChange={e => setPassword(e.target.value)} required
          style={{ width: "100%", marginBottom: 14, padding: "10px 14px", background: "var(--c-panel)", border: "1px solid var(--c-border)", borderRadius: 8, color: "var(--c-text)", fontSize: 14, outline: "none" }} />
        <label style={{ display: "block", marginBottom: 4, color: "var(--c-text-muted)", fontSize: 12, fontWeight: 500 }}>{t("bootstrap.confirm_password")}</label>
        <input type="password" value={confirm} onChange={e => setConfirm(e.target.value)} required
          style={{ width: "100%", marginBottom: 24, padding: "10px 14px", background: "var(--c-panel)", border: "1px solid var(--c-border)", borderRadius: 8, color: "var(--c-text)", fontSize: 14, outline: "none" }} />
        <button type="submit" disabled={loading}
          style={{ width: "100%", padding: "11px 0", background: "linear-gradient(135deg,#1a6fcc,#2d8ff0)", border: "none", borderRadius: 8, color: "#fff", fontFamily: "var(--font-title)", fontWeight: 700, fontSize: 15, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", gap: 8 }}>
          {loading ? <Spinner size={16} /> : null}
          {loading ? t("bootstrap.creating") : t("bootstrap.submit")}
        </button>
      </form>
    </div>
  )
}

// -- Login -------------------------------------------------------------------
function LoginForm({ onLogin, registrationOpen = true }) {
  const { t } = useTranslation()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError]       = useState("")
  const [accountState, setAccountState] = useState(null)
  const [loading, setLoading]   = useState(false)

  async function submit(e) {
    e.preventDefault()
    setLoading(true); setError(""); setAccountState(null)
    try {
      const body = new URLSearchParams({ username, password })
      const res  = await fetch(`${API_URL}/api/auth/token`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        if (res.status === 403 && (d.status === "pending" || d.status === "suspended")) {
          setAccountState(d)
          return
        }
        throw new Error(
          typeof d.detail === "string"
            ? d.detail
            : (d.detail?.detail || d.detail?.message || `Erreur ${res.status}`)
        )
      }
      const { access_token } = await res.json()
      sessionStorage.setItem(TOKEN_KEY, access_token)
      onLogin(access_token)
    } catch (err) { setError(err.message) }
    finally { setLoading(false) }
  }

  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--c-bg)" }}>
      <form onSubmit={submit} style={{ width: 340, padding: 40, background: "var(--c-surface)", borderRadius: 16, border: "1px solid var(--c-border)" }}>
        <div style={{ textAlign: "center", marginBottom: 32 }}>
          <ShieldSvg size={48} className="shield-glow" style={{ margin: "0 auto 12px" }} />
          <h1 className="font-title" style={{ fontSize: 22, fontWeight: 700, color: "var(--c-text)", marginTop: 12 }}>{t("login.title")}</h1>
          <p style={{ color: "var(--c-text-muted)", fontSize: 13, marginTop: 4 }}>{t("login.subtitle")}</p>
        </div>
        {accountState?.status === "pending" && (
          <div style={{ background: "#2a2210", border: "1px solid #b45309", borderRadius: 8, padding: "12px 14px", color: "#fde68a", fontSize: 13, marginBottom: 16 }}>
            <div style={{ fontWeight: 700, marginBottom: 4 }}>⏳ Compte en attente de validation</div>
            <div>Votre compte existe mais n'a pas encore été validé par l'administrateur.</div>
            <div style={{ marginTop: 4 }}>Revenez plus tard ou contactez l'administrateur.</div>
          </div>
        )}
        {accountState?.status === "suspended" && (
          <div style={{ background: "#2a0e0e", border: "1px solid #7f1d1d", borderRadius: 8, padding: "12px 14px", color: "#fca5a5", fontSize: 13, marginBottom: 16 }}>
            {accountState.detail}
          </div>
        )}
        {error && <div style={{ background: "#2a0e0e", border: "1px solid #7f1d1d", borderRadius: 8, padding: "10px 14px", color: "#fca5a5", fontSize: 13, marginBottom: 16 }}>{error}</div>}
        <label style={{ display: "block", marginBottom: 4, color: "var(--c-text-muted)", fontSize: 12, fontWeight: 500 }}>{t("login.username")}</label>
        <input value={username} onChange={e => setUsername(e.target.value)} autoFocus required
          style={{ width: "100%", marginBottom: 14, padding: "10px 14px", background: "var(--c-panel)", border: "1px solid var(--c-border)", borderRadius: 8, color: "var(--c-text)", fontSize: 14, outline: "none" }} />
        <label style={{ display: "block", marginBottom: 4, color: "var(--c-text-muted)", fontSize: 12, fontWeight: 500 }}>{t("login.password")}</label>
        <input type="password" value={password} onChange={e => setPassword(e.target.value)} required
          style={{ width: "100%", marginBottom: 24, padding: "10px 14px", background: "var(--c-panel)", border: "1px solid var(--c-border)", borderRadius: 8, color: "var(--c-text)", fontSize: 14, outline: "none" }} />
        <button type="submit" disabled={loading}
          style={{ width: "100%", padding: "11px 0", background: "linear-gradient(135deg,#1a6fcc,#2d8ff0)", border: "none", borderRadius: 8, color: "#fff", fontFamily: "var(--font-title)", fontWeight: 700, fontSize: 15, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", gap: 8 }}>
          {loading ? <Spinner size={16} /> : null}
          {loading ? t("login.connecting") : t("login.submit")}
        </button>
        {registrationOpen && (
          <div style={{ marginTop: 16, textAlign: "center" }}>
            <Link to="/register" style={{ color: "var(--c-accent-lt)", textDecoration: "none", fontSize: 13 }}>
              Créer un compte
            </Link>
          </div>
        )}
      </form>
    </div>
  )
}

// -- Layout authentifie ------------------------------------------------------
function AppLayout({ token, username, role, plan, status, onLogout }) {
  const [activeProjectId, setActiveProjectId]     = useState(null)
  const [sidebarRefresh, setSidebarRefresh]        = useState(0)
  const [hiddenCount, setHiddenCount]              = useState(0)
  const navigate = useNavigate()

  const refreshSidebar = useCallback(() => setSidebarRefresh(n => n + 1), [])
  const handleNewChat = useCallback(() => {
    setActiveProjectId(null)
    setHiddenCount(0)
    navigate("/")
  }, [navigate])

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh", overflow: "hidden" }}>
      <TopBar username={username} role={role} plan={plan} status={status} onLogout={onLogout} onHome={handleNewChat} />
      <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>
        <ProjectSidebar
          token={token}
          activeProjectId={activeProjectId}
          onSelectProject={setActiveProjectId}
          onNewProject={handleNewChat}
          refresh={sidebarRefresh}
        />
        <main style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
          <Routes>
            <Route path="/" element={
              <ChatInterface
                token={token}
                username={username}
                activeProjectId={activeProjectId}
                onProjectCreated={id => { setActiveProjectId(id); refreshSidebar() }}
                onHiddenCountChange={setHiddenCount}
              />
            }/>
            <Route path="/dashboard" element={<UserDashboard token={token} username={username} />} />
            <Route path="/dashboard/full" element={<Dashboard token={token} />} />
            <Route path="/settings" element={<Settings token={token} username={username} />} />
            <Route path="/admin" element={role === "admin" ? <AdminDashboard token={token} /> : <Navigate to="/" replace />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
        <RightPanel hiddenCount={hiddenCount} />
      </div>
    </div>
  )
}

// -- Root --------------------------------------------------------------------
export default function App() {
  const { t } = useTranslation()
  const [token, setToken]           = useState(() => sessionStorage.getItem(TOKEN_KEY) ?? "")
  const [initialized, setInitialized] = useState(null)  // null = checking
  const [registrationOpen, setRegistrationOpen] = useState(true)

  useEffect(() => {
    fetch(`${API_URL}/api/auth/status`)
      .then(r => r.json())
      .then(d => {
        setInitialized(d.initialized ?? true)
        setRegistrationOpen(d.registration_open ?? true)
      })
      .catch(() => { setInitialized(true); setRegistrationOpen(true) })  // on error, assume initialized (safer)
  }, [])

  function handleLogin(t) { setToken(t) }
  async function handleLogout() {
    try {
      const t = sessionStorage.getItem(TOKEN_KEY)
      if (t) {
        // Revocation serveur : blacklist le JTI dans Redis
        // Le token ne sera plus accepte meme s'il n'est pas encore expire.
        await fetch(`${API_URL}/api/auth/logout`, {
          method: "POST",
          headers: { Authorization: `Bearer ${t}` },
        })
      }
    } catch (_) {
      // Echec silencieux : on deconnecte quand meme cote client
    } finally {
      sessionStorage.removeItem(TOKEN_KEY)
      setToken("")
    }
  }

  // Checking status
  if (initialized === null) {
    return (
      <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--c-bg)" }}>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 12 }}>
          <Spinner size={32} />
          <div style={{ fontSize: 13, color: "var(--c-text-muted)" }}>{t("common.loading")}</div>
        </div>
      </div>
    )
  }

  // Not yet initialized — show bootstrap page
  if (!initialized) {
    return <BootstrapPage onBootstrapped={() => setInitialized(true)} />
  }

  // Initialized but no token — show login
  if (!token) {
    return (
      <Routes>
        <Route path="/register" element={<Register />} />
        <Route path="*" element={<LoginForm onLogin={handleLogin} registrationOpen={registrationOpen} />} />
      </Routes>
    )
  }

  const payload  = decodeJwt(token)
  const username = payload.sub ?? "user"
  const role     = payload.role ?? "member"
  const plan     = payload.plan ?? null
  const accountStatus = payload.status ?? "active"

  return <AppLayout token={token} username={username} role={role} plan={plan} status={accountStatus} onLogout={handleLogout} />
}

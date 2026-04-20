import React, { useState, useRef, useEffect } from "react"
import { Link, useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { ShieldSvg } from "../App.jsx"

const LANGS = ["fr", "en", "es"]

function planBadge(plan, role, status) {
  if (role === "admin") return { label: "Admin", color: "#ef4444" }
  if (status === "pending") return { label: "En attente", color: "#f59e0b" }
  if (status === "suspended") return { label: "Suspendu", color: "#6b7280" }
  if (plan === "max") return { label: "Max", color: "#d4af37" }
  if (plan === "pro") return { label: "Pro", color: "#2d8ff0" }
  return { label: "Basic", color: "#94a3b8" }
}

export default function TopBar({ username, role, plan, status, onLogout, onHome }) {
  const { t, i18n } = useTranslation()
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  const navigate = useNavigate()

  useEffect(() => {
    function handler(e) { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [])

  function setLang(lang) {
    i18n.changeLanguage(lang)
    localStorage.setItem("language", lang)
  }

  const initials = username ? username.slice(0, 2).toUpperCase() : "U"
  const currentLang = i18n.language?.slice(0, 2) ?? "fr"
  const accountBadge = planBadge(plan, role, status)

  return (
    <header style={{
      height: 50, minHeight: 50,
      background: "var(--c-surface)",
      borderBottom: "1px solid var(--c-border)",
      display: "flex", alignItems: "center",
      padding: "0 16px", gap: 12, zIndex: 50,
    }}>
      {/* Logo */}
      <div
        onClick={() => onHome?.()}
        style={{ display: "flex", alignItems: "center", gap: 9, flex: "0 0 auto", cursor: "pointer" }}
        title={t("chat.new_project")}
      >
        <ShieldSvg size={26} className="shield-glow" />
        <span className="font-title" style={{ fontSize: 16, fontWeight: 700, color: "var(--c-text)", letterSpacing: "-.02em" }}>
          IaProTech
        </span>
        <span style={{ fontSize: 10, color: "var(--c-text-faint)", fontWeight: 500, marginLeft: 2, marginTop: 2, letterSpacing: ".06em", textTransform: "uppercase" }}>
          Privacy&nbsp;Proxy
        </span>
      </div>

      <div style={{ flex: 1 }} />

      {/* Selecteur de langue */}
      <div style={{ display: "flex", gap: 2 }}>
        {LANGS.map(lang => (
          <button key={lang} onClick={() => setLang(lang)}
            style={{
              padding: "3px 7px", borderRadius: 5, fontSize: 11, fontWeight: 600,
              border: "1px solid " + (currentLang === lang ? "var(--c-accent)" : "var(--c-border)"),
              background: currentLang === lang ? "var(--c-accent-dim)" : "none",
              color: currentLang === lang ? "var(--c-accent-lt)" : "var(--c-text-faint)",
              cursor: "pointer", textTransform: "uppercase", letterSpacing: ".04em",
              transition: "all .12s",
            }}>
            {lang}
          </button>
        ))}
      </div>

      {/* Dashboard link */}
      <Link to="/dashboard" style={{
        fontSize: 12, color: "var(--c-text-muted)", textDecoration: "none",
        padding: "4px 10px", borderRadius: 6,
        border: "1px solid var(--c-border)",
        transition: "all .15s",
      }}
        onMouseEnter={e => { e.currentTarget.style.color = "var(--c-accent-lt)"; e.currentTarget.style.borderColor = "var(--c-accent)" }}
        onMouseLeave={e => { e.currentTarget.style.color = "var(--c-text-muted)"; e.currentTarget.style.borderColor = "var(--c-border)" }}
      >
        {t("topbar.dashboard_link")}
      </Link>

      {/* Avatar dropdown */}
      <div ref={ref} style={{ position: "relative" }}>
        <button onClick={() => setOpen(o => !o)}
          style={{
            width: 32, height: 32, borderRadius: "50%",
            background: "linear-gradient(135deg,#1a6fcc,#2d8ff0)",
            border: "none", cursor: "pointer",
            color: "#fff", fontWeight: 700, fontSize: 12,
            fontFamily: "var(--font-title)",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}>
          {initials}
        </button>
        {open && (
          <div style={{
            position: "absolute", right: 0, top: "calc(100% + 6px)",
            background: "var(--c-panel)", border: "1px solid var(--c-border)",
            borderRadius: 10, padding: "6px 0", minWidth: 160, zIndex: 100,
            boxShadow: "0 8px 24px #00000055",
          }}>
            <div style={{ padding: "8px 14px 6px", borderBottom: "1px solid var(--c-border)", marginBottom: 4 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: "var(--c-text)" }}>{username}</div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}>
                <span style={{ fontSize: 11, color: "var(--c-text-muted)" }}>{role === "admin" ? t("topbar.administrator") : username}</span>
                <span style={{
                  padding: "2px 8px",
                  borderRadius: 999,
                  fontSize: 10,
                  fontWeight: 700,
                  border: `1px solid ${accountBadge.color}44`,
                  background: `${accountBadge.color}22`,
                  color: accountBadge.color,
                }}>
                  {accountBadge.label}
                </span>
              </div>
            </div>
            {role === "admin" && (
              <button onClick={() => { setOpen(false); navigate("/admin") }}
                style={{ ...menuItem, color: "var(--c-accent-lt)" }}>{t("topbar.admin")}</button>
            )}
            <button onClick={() => { setOpen(false); navigate("/dashboard") }}
              style={menuItem}>{t("topbar.dashboard")}</button>
            <button onClick={() => { setOpen(false); onLogout() }}
              style={{ ...menuItem, color: "#f87171" }}>{t("topbar.logout")}</button>
          </div>
        )}
      </div>
    </header>
  )
}

const menuItem = {
  display: "block", width: "100%", textAlign: "left",
  padding: "8px 14px", background: "none", border: "none",
  color: "var(--c-text)", fontSize: 13, cursor: "pointer",
  transition: "background .1s",
}

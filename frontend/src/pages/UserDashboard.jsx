import React, { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { API_URL, Spinner } from "../App.jsx"

function StatCard({ label, value, sub, accent = false }) {
  return (
    <div style={{
      flex: 1, minWidth: 140,
      background: "var(--c-panel)",
      border: accent ? "1px solid var(--c-accent)" : "1px solid var(--c-border)",
      borderRadius: 10, padding: "16px 20px", textAlign: "center",
    }}>
      <div style={{ fontSize: 26, fontWeight: 700, color: accent ? "var(--c-accent-lt)" : "var(--c-text)", fontFamily: "var(--font-title)" }}>{value}</div>
      <div style={{ fontSize: 12, color: "var(--c-text-muted)", fontWeight: 600, marginTop: 4 }}>{label}</div>
      {sub && <div style={{ fontSize: 11, color: "var(--c-text-faint)", marginTop: 2 }}>{sub}</div>}
    </div>
  )
}

function ProgressBar({ pct, color = "var(--c-accent)" }) {
  const p = Math.min(100, Math.max(0, pct))
  const barColor = p > 90 ? "#ef4444" : p > 70 ? "#f59e0b" : color
  return (
    <div style={{ background: "var(--c-border)", borderRadius: 8, height: 8, overflow: "hidden" }}>
      <div style={{ width: `${p}%`, height: "100%", background: barColor, borderRadius: 8, transition: "width .4s" }} />
    </div>
  )
}

export default function UserDashboard({ token, username }) {
  const { t } = useTranslation()
  const [usage, setUsage]   = useState(null)
  const [plan, setPlan]     = useState(null)
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(true)
  const auth = { Authorization: `Bearer ${token}` }

  useEffect(() => {
    Promise.all([
      fetch(`${API_URL}/dashboard/usage/current`, { headers: auth }).then(r => r.json()),
      fetch(`${API_URL}/dashboard/usage/plan`, { headers: auth }).then(r => r.json()),
      fetch(`${API_URL}/dashboard/usage/history`, { headers: auth }).then(r => r.json()),
    ]).then(([u, p, h]) => {
      setUsage(u)
      setPlan(p)
      setHistory(h.days || [])
    }).catch(() => {}).finally(() => setLoading(false))
  }, [token])

  if (loading) return <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--c-text-muted)" }}><Spinner size={32} /></div>

  return (
    <div style={{ padding: 24, maxWidth: 900, margin: "0 auto" }}>
      {/* Header */}
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ fontSize: 20, fontWeight: 700, color: "var(--c-text)", fontFamily: "var(--font-title)", marginBottom: 4 }}>
          {t("dashboard.title")}
        </h1>
        <div style={{ fontSize: 13, color: "var(--c-text-faint)" }}>
          Bonjour <strong style={{ color: "var(--c-text)" }}>{username}</strong> — plan <strong style={{ color: "var(--c-accent-lt)" }}>{plan?.plan_name ?? "—"}</strong>
        </div>
      </div>

      {/* Quota bar */}
      {plan && plan.monthly_tokens > 0 && (
        <div style={{ background: "var(--c-panel)", border: "1px solid var(--c-border)", borderRadius: 10, padding: "16px 20px", marginBottom: 20 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: "var(--c-text)" }}>{t("dashboard.quota_monthly")}</span>
            <span style={{ fontSize: 12, color: "var(--c-text-faint)" }}>
              {plan.used_tokens.toLocaleString()} / {plan.monthly_tokens.toLocaleString()} tokens — {plan.tokens_pct}%
            </span>
          </div>
          <ProgressBar pct={plan.tokens_pct} />
          <div style={{ marginTop: 12, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontSize: 12, color: "var(--c-text-faint)" }}>Requêtes : {plan.used_requests} / {plan.monthly_requests}</span>
            <span style={{ fontSize: 12, color: "var(--c-text-faint)" }}>Coût estimé : ${plan.used_cost_usd.toFixed(4)}</span>
          </div>
        </div>
      )}

      {/* Stat cards */}
      {usage && (
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 24 }}>
          <StatCard label={t("dashboard.tokens_month")} value={usage.total_tokens.toLocaleString()} sub={t("dashboard.tokens_used")} accent />
          <StatCard label={t("dashboard.requests")} value={usage.requests_count.toLocaleString()} sub={t("dashboard.api_calls")} />
          <StatCard label={t("dashboard.cost")} value={`$${usage.total_cost_usd.toFixed(4)}`} sub={t("dashboard.this_month")} />
        </div>
      )}

      {/* History table */}
      {history.length > 0 && (
        <div style={{ background: "var(--c-panel)", border: "1px solid var(--c-border)", borderRadius: 10, padding: "16px 20px" }}>
          <h2 style={{ fontSize: 14, fontWeight: 700, color: "var(--c-text)", marginBottom: 12 }}>{t("dashboard.tokens_per_day")}</h2>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ color: "var(--c-text-faint)", textTransform: "uppercase", letterSpacing: ".05em", fontSize: 10 }}>
                  {["Date", "Tokens", "Requêtes", "Coût"].map(h => (
                    <th key={h} style={{ padding: "6px 10px", textAlign: "left", borderBottom: "1px solid var(--c-border)" }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {history.slice().reverse().slice(0, 14).map((d, i) => (
                  <tr key={i} style={{ borderBottom: "1px solid var(--c-border)" }}>
                    <td style={{ padding: "7px 10px", color: "var(--c-text-muted)" }}>{d.date}</td>
                    <td style={{ padding: "7px 10px", color: "var(--c-text)" }}>{d.tokens.toLocaleString()}</td>
                    <td style={{ padding: "7px 10px", color: "var(--c-text-muted)" }}>{d.requests}</td>
                    <td style={{ padding: "7px 10px", color: "var(--c-text-muted)" }}>${(d.cost_usd ?? 0).toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Provider breakdown */}
      {usage && usage.by_provider && Object.keys(usage.by_provider).length > 0 && (
        <div style={{ marginTop: 16, background: "var(--c-panel)", border: "1px solid var(--c-border)", borderRadius: 10, padding: "16px 20px" }}>
          <h2 style={{ fontSize: 14, fontWeight: 700, color: "var(--c-text)", marginBottom: 12 }}>{t("dashboard.by_provider")}</h2>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ color: "var(--c-text-faint)", textTransform: "uppercase", letterSpacing: ".05em", fontSize: 10 }}>
                {[t("dashboard.provider"), "Tokens", t("dashboard.requests"), t("dashboard.cost"), "%"].map(h => (
                  <th key={h} style={{ padding: "6px 10px", textAlign: "left", borderBottom: "1px solid var(--c-border)" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {Object.entries(usage.by_provider).map(([name, data]) => (
                <tr key={name} style={{ borderBottom: "1px solid var(--c-border)" }}>
                  <td style={{ padding: "7px 10px", color: "var(--c-text)", fontWeight: 500 }}>{name}</td>
                  <td style={{ padding: "7px 10px", color: "var(--c-text-muted)" }}>{(data.tokens ?? 0).toLocaleString()}</td>
                  <td style={{ padding: "7px 10px", color: "var(--c-text-muted)" }}>{data.count ?? 0}</td>
                  <td style={{ padding: "7px 10px", color: "var(--c-text-muted)" }}>${(data.cost_usd ?? 0).toFixed(4)}</td>
                  <td style={{ padding: "7px 10px", color: "var(--c-text-faint)" }}>
                    {usage.total_tokens > 0 ? Math.round((data.tokens ?? 0) / usage.total_tokens * 100) : 0}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

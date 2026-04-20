import React, { useState, useEffect, useCallback } from "react"
import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router-dom"
import { API_URL, Spinner } from "../App.jsx"

const TABS = ["requests", "users", "orgs", "stats", "audit", "logs", "maintenance"]

const card = {
  background: "var(--c-panel)",
  border: "1px solid var(--c-border)",
  borderRadius: 10,
  padding: "16px 20px",
  marginBottom: 12,
}

const badge = (color) => ({
  display: "inline-block",
  padding: "2px 8px",
  borderRadius: 12,
  fontSize: 11,
  fontWeight: 600,
  background: `${color}22`,
  color,
  border: `1px solid ${color}44`,
})

const filterInput = {
  padding: "8px 10px",
  border: "1px solid var(--c-border)",
  borderRadius: 8,
  background: "var(--c-surface)",
  color: "var(--c-text)",
  fontSize: 13,
}

const actionBtn = (color) => ({
  padding: "4px 10px",
  border: `1px solid ${color}44`,
  borderRadius: 6,
  background: `${color}18`,
  color,
  fontSize: 11,
  fontWeight: 600,
  cursor: "pointer",
  transition: "all .12s",
})

function StatCard({ label, value, sub }) {
  return (
    <div style={{ ...card, flex: 1, minWidth: 150, textAlign: "center" }}>
      <div
        style={{
          fontSize: 28,
          fontWeight: 700,
          color: "var(--c-accent-lt)",
          fontFamily: "var(--font-title)",
        }}
      >
        {value}
      </div>
      <div style={{ fontSize: 12, color: "var(--c-text)", fontWeight: 600, marginTop: 4 }}>
        {label}
      </div>
      {sub && <div style={{ fontSize: 11, color: "var(--c-text-faint)", marginTop: 2 }}>{sub}</div>}
    </div>
  )
}

function UsersTab({ token }) {
  const { t } = useTranslation()
  const [users, setUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [actionLoading, setActionLoading] = useState("")
  const [error, setError] = useState("")
  const auth = { Authorization: `Bearer ${token}` }

  const load = useCallback(async () => {
    setLoading(true)
    setError("")
    try {
      const response = await fetch(`${API_URL}/api/admin/users`, { headers: auth })
      if (!response.ok) throw new Error(t("admin.load_failed"))
      const data = await response.json()
      setUsers(data.users || [])
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
    }
  }, [token, t])

  useEffect(() => {
    load()
  }, [load])

  async function changeRole(username, newRole) {
    const response = await fetch(`${API_URL}/api/admin/users/${username}/role`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ role: newRole }),
    })
    if (!response.ok) {
      const errorPayload = await response.json().catch(() => ({}))
      throw new Error(errorPayload.detail || t("admin.load_failed"))
    }
    return response.json()
  }

  async function action(url, method = "POST", body = null) {
    setActionLoading(url)
    setError("")
    try {
      const response = await fetch(`${API_URL}${url}`, {
        method,
        headers: body ? { ...auth, "Content-Type": "application/json" } : auth,
        body: body ? JSON.stringify(body) : undefined,
      })
      if (!response.ok) throw new Error(t("admin.load_failed"))
      await load()
    } catch (actionError) {
      setError(actionError.message)
    } finally {
      setActionLoading("")
    }
  }

  if (loading) return <div style={{ textAlign: "center", padding: 40 }}><Spinner size={28} /></div>

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h2 style={{ fontSize: 16, fontWeight: 700, color: "var(--c-text)" }}>
          {t("admin.users")} ({users.length})
        </h2>
      </div>

      {error && <div style={{ ...card, color: "#fca5a5" }}>{error}</div>}

      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: "var(--c-panel)", color: "var(--c-text-muted)", fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em" }}>
              {[
                t("admin.table_user"),
                t("admin.role"),
                t("admin.table_org"),
                t("admin.status"),
                t("admin.table_tokens_month"),
                t("admin.table_cost_month"),
                t("admin.actions"),
              ].map((header) => (
                <th key={header} style={{ padding: "8px 12px", textAlign: "left", borderBottom: "1px solid var(--c-border)" }}>
                  {header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {users.map((user) => {
              const nextRole = user.role === "admin" ? "member" : "admin"
              return (
                <tr key={user.username} style={{ borderBottom: "1px solid var(--c-border)" }}>
                  <td style={{ padding: "10px 12px", color: "var(--c-text)", fontWeight: 500 }}>
                    {user.username}
                  </td>
                  <td style={{ padding: "10px 12px" }}>
                    <span style={badge(user.role === "admin" ? "#2d8ff0" : "#6b7280")}>
                      {user.role === "admin" ? t("admin.role_admin") : t("admin.role_member")}
                    </span>
                  </td>
                  <td style={{ padding: "10px 12px", color: "var(--c-text-faint)", fontSize: 11 }}>
                    {user.org_id || "-"}
                  </td>
                  <td style={{ padding: "10px 12px" }}>
                    <span style={badge(user.is_active ? "#22c55e" : "#ef4444")}>
                      {user.is_active ? t("admin.active") : t("admin.suspended")}
                    </span>
                  </td>
                  <td style={{ padding: "10px 12px", color: "var(--c-text-muted)" }}>
                    {user.tokens_month.toLocaleString()}
                  </td>
                  <td style={{ padding: "10px 12px", color: "var(--c-text-muted)" }}>
                    ${user.cost_month.toFixed(4)}
                  </td>
                  <td style={{ padding: "10px 12px" }}>
                    <div style={{ display: "flex", gap: 6 }}>
                      {user.is_active ? (
                        <button
                          onClick={() => action(`/api/admin/users/${user.username}/suspend`)}
                          disabled={!!actionLoading}
                          style={actionBtn("#f59e0b")}
                        >
                          {t("admin.suspend")}
                        </button>
                      ) : (
                        <button
                          onClick={() => action(`/api/admin/users/${user.username}/unsuspend`)}
                          disabled={!!actionLoading}
                          style={actionBtn("#22c55e")}
                        >
                          {t("admin.reactivate")}
                        </button>
                      )}
                      <button
                        onClick={async () => {
                          setActionLoading(`/api/admin/users/${user.username}/role`)
                          setError("")
                          try {
                            await changeRole(user.username, nextRole)
                            await load()
                          } catch (actionError) {
                            setError(actionError.message)
                          } finally {
                            setActionLoading("")
                          }
                        }}
                        disabled={!!actionLoading}
                        style={actionBtn("#2d8ff0")}
                        title={t("admin.toggle_role", { role: nextRole === "admin" ? t("admin.role_admin") : t("admin.role_member") })}
                      >
                        {nextRole === "admin" ? t("admin.make_admin") : t("admin.make_member")}
                      </button>
                      <button
                        onClick={() => {
                          if (window.confirm(t("admin.delete_user_confirm", { username: user.username }))) {
                            action(`/api/admin/users/${user.username}`, "DELETE")
                          }
                        }}
                        disabled={!!actionLoading}
                        style={actionBtn("#ef4444")}
                      >
                        {t("admin.delete")}
                      </button>
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function AccessRequestsTab({ token, onPendingCount }) {
  const [pending, setPending] = useState([])
  const [activeUsers, setActiveUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const auth = { Authorization: `Bearer ${token}` }

  const load = useCallback(async () => {
    setLoading(true)
    setError("")
    try {
      const [pendingRes, activeRes] = await Promise.all([
        fetch(`${API_URL}/api/admin/users/pending`, { headers: auth }),
        fetch(`${API_URL}/api/admin/users/all`, { headers: auth }),
      ])
      if (!pendingRes.ok || !activeRes.ok) throw new Error("Chargement impossible")
      const pendingData = await pendingRes.json()
      const activeData = await activeRes.json()
      setPending(pendingData.users || [])
      setActiveUsers((activeData.users || []).filter((user) => user.status === "active"))
      onPendingCount?.((pendingData.users || []).length)
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
    }
  }, [token, onPendingCount])

  useEffect(() => {
    load()
  }, [load])

  async function activate(username, plan) {
    const response = await fetch(`${API_URL}/api/admin/users/${username}/activate`, {
      method: "POST",
      headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ plan }),
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.detail ?? "Activation impossible")
    await load()
  }

  async function reject(username) {
    const response = await fetch(`${API_URL}/api/admin/users/${username}/reject`, {
      method: "POST",
      headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "Rejected by admin" }),
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.detail ?? "Refus impossible")
    await load()
  }

  async function changePlan(username, plan) {
    const response = await fetch(`${API_URL}/api/admin/users/${username}/plan`, {
      method: "PUT",
      headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ plan }),
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.detail ?? "Changement de plan impossible")
    await load()
  }

  if (loading) return <div style={{ textAlign: "center", padding: 40 }}><Spinner size={28} /></div>

  return (
    <div style={{ display: "grid", gap: 18 }}>
      {error && <div style={{ ...card, color: "#fca5a5" }}>{error}</div>}

      <div style={card}>
        <h2 style={{ fontSize: 16, fontWeight: 700, color: "var(--c-text)", marginBottom: 16 }}>Demandes d'accès ({pending.length})</h2>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ background: "var(--c-panel)", color: "var(--c-text-muted)", fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em" }}>
                {["Username", "Email", "Date inscription", "Invité par", "Actions"].map((header) => (
                  <th key={header} style={{ padding: "8px 12px", textAlign: "left", borderBottom: "1px solid var(--c-border)" }}>{header}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {pending.map((user) => (
                <tr key={user.username} style={{ borderBottom: "1px solid var(--c-border)" }}>
                  <td style={{ padding: "10px 12px", color: "var(--c-text)" }}>{user.username}</td>
                  <td style={{ padding: "10px 12px", color: "var(--c-text-muted)" }}>{user.email || "-"}</td>
                  <td style={{ padding: "10px 12px", color: "var(--c-text-muted)" }}>{user.created_at ? new Date(user.created_at).toLocaleString() : "-"}</td>
                  <td style={{ padding: "10px 12px", color: "var(--c-text-muted)" }}>{user.invited_by || "-"}</td>
                  <td style={{ padding: "10px 12px" }}>
                    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      <select onChange={(event) => event.target.value && activate(user.username, event.target.value)} defaultValue="" style={filterInput}>
                        <option value="">Activer...</option>
                        <option value="basic">Basic</option>
                        <option value="pro">Pro</option>
                        <option value="max">Max</option>
                      </select>
                      <button onClick={() => reject(user.username)} style={actionBtn("#ef4444")}>Refuser</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div style={card}>
        <h2 style={{ fontSize: 16, fontWeight: 700, color: "var(--c-text)", marginBottom: 16 }}>Comptes actifs</h2>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ background: "var(--c-panel)", color: "var(--c-text-muted)", fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em" }}>
                {["Username", "Email", "Plan", "Tokens/mois", "Statut", "Actions"].map((header) => (
                  <th key={header} style={{ padding: "8px 12px", textAlign: "left", borderBottom: "1px solid var(--c-border)" }}>{header}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {activeUsers.map((user) => (
                <tr key={user.username} style={{ borderBottom: "1px solid var(--c-border)" }}>
                  <td style={{ padding: "10px 12px", color: "var(--c-text)" }}>{user.username}</td>
                  <td style={{ padding: "10px 12px", color: "var(--c-text-muted)" }}>{user.email || "-"}</td>
                  <td style={{ padding: "10px 12px" }}><span style={badge("#2d8ff0")}>{user.plan || "-"}</span></td>
                  <td style={{ padding: "10px 12px", color: "var(--c-text-muted)" }}>{(user.tokens_month || 0).toLocaleString()}</td>
                  <td style={{ padding: "10px 12px" }}><span style={badge("#22c55e")}>{user.status}</span></td>
                  <td style={{ padding: "10px 12px" }}>
                    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      <select onChange={(event) => event.target.value && changePlan(user.username, event.target.value)} defaultValue="" style={filterInput}>
                        <option value="">Changer plan...</option>
                        <option value="basic">Basic</option>
                        <option value="pro">Pro</option>
                        <option value="max">Max</option>
                      </select>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

function OrgsTab({ token }) {
  const { t } = useTranslation()
  const [orgs, setOrgs] = useState([])
  const [orgSettings, setOrgSettings] = useState(null)
  const [departmentsInput, setDepartmentsInput] = useState("")
  const [savingSettings, setSavingSettings] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const auth = { Authorization: `Bearer ${token}` }

  const load = useCallback(async () => {
    setLoading(true)
    setError("")
    try {
      const [orgsResponse, meResponse] = await Promise.all([
        fetch(`${API_URL}/api/admin/organizations`, { headers: auth }),
        fetch(`${API_URL}/api/organizations/me`, { headers: auth }),
      ])
      if (!orgsResponse.ok || !meResponse.ok) throw new Error(t("admin.load_failed"))
      const data = await orgsResponse.json()
      const me = await meResponse.json()
      setOrgs(data.organizations || [])
      setOrgSettings(me.settings || null)
      setDepartmentsInput((me.settings?.policy?.departments_strict_mode || []).join(", "))
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
    }
  }, [token, t])

  useEffect(() => {
    load()
  }, [load])

  async function deleteOrg(orgId) {
    if (!window.confirm(t("admin.delete_org_confirm", { orgId }))) return
    setError("")
    try {
      const response = await fetch(`${API_URL}/api/admin/organizations/${orgId}`, {
        method: "DELETE",
        headers: auth,
      })
      if (!response.ok) throw new Error(t("admin.load_failed"))
      await load()
    } catch (deleteError) {
      setError(deleteError.message)
    }
  }

  async function saveOrgSettings() {
    setSavingSettings(true)
    setError("")
    try {
      const departments = departmentsInput
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean)
      const response = await fetch(`${API_URL}/api/organizations/settings`, {
        method: "PUT",
        headers: {
          ...auth,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ departments_strict_mode: departments }),
      })
      if (!response.ok) throw new Error(t("admin.load_failed"))
      const updated = await response.json()
      setOrgSettings(updated)
      setDepartmentsInput((updated.policy?.departments_strict_mode || []).join(", "))
    } catch (saveError) {
      setError(saveError.message)
    } finally {
      setSavingSettings(false)
    }
  }

  if (loading) return <div style={{ textAlign: "center", padding: 40 }}><Spinner size={28} /></div>

  return (
    <div>
      <h2 style={{ fontSize: 16, fontWeight: 700, color: "var(--c-text)", marginBottom: 16 }}>
        {t("admin.organizations")} ({orgs.length})
      </h2>
      {error && <div style={{ ...card, color: "#fca5a5" }}>{error}</div>}
      <div style={{ ...card, marginBottom: 16 }}>
        <div style={{ fontSize: 14, fontWeight: 700, color: "var(--c-text)", marginBottom: 6 }}>
          {t("admin.org_settings_title")}
        </div>
        <div style={{ fontSize: 12, color: "var(--c-text-faint)", marginBottom: 12 }}>
          {t("admin.org_settings_help")}
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <input
            value={departmentsInput}
            onChange={(event) => setDepartmentsInput(event.target.value)}
            placeholder={t("admin.strict_departments_placeholder")}
            style={{ ...filterInput, minWidth: 320, flex: 1 }}
          />
          <button onClick={saveOrgSettings} disabled={savingSettings} style={actionBtn("#2d8ff0")}>
            {savingSettings ? t("common.loading") : t("common.save")}
          </button>
        </div>
        {orgSettings?.policy?.departments_strict_mode?.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 12 }}>
            {orgSettings.policy.departments_strict_mode.map((department) => (
              <span key={department} style={badge("#f59e0b")}>{department}</span>
            ))}
          </div>
        )}
      </div>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: "var(--c-panel)", color: "var(--c-text-muted)", fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em" }}>
              {[
                t("admin.table_name"),
                t("admin.table_plan"),
                t("admin.table_members"),
                t("admin.table_tokens_month"),
                t("admin.cost"),
                t("admin.table_created_at"),
                t("admin.actions"),
              ].map((header) => (
                <th key={header} style={{ padding: "8px 12px", textAlign: "left", borderBottom: "1px solid var(--c-border)" }}>
                  {header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {orgs.map((org) => (
              <tr key={org.org_id} style={{ borderBottom: "1px solid var(--c-border)" }}>
                <td style={{ padding: "10px 12px", color: "var(--c-text)", fontWeight: 500 }}>{org.name}</td>
                <td style={{ padding: "10px 12px" }}><span style={badge("#2d8ff0")}>{org.plan}</span></td>
                <td style={{ padding: "10px 12px", color: "var(--c-text-muted)" }}>{org.member_count}</td>
                <td style={{ padding: "10px 12px", color: "var(--c-text-muted)" }}>{org.month_tokens.toLocaleString()}</td>
                <td style={{ padding: "10px 12px", color: "var(--c-text-muted)" }}>${org.month_cost_usd.toFixed(4)}</td>
                <td style={{ padding: "10px 12px", color: "var(--c-text-faint)", fontSize: 11 }}>
                  {org.created_at ? new Date(org.created_at).toLocaleDateString() : "-"}
                </td>
                <td style={{ padding: "10px 12px" }}>
                  <button onClick={() => deleteOrg(org.org_id)} style={actionBtn("#ef4444")}>
                    {t("admin.delete")}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function StatsTab({ token }) {
  const { t } = useTranslation()
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const auth = { Authorization: `Bearer ${token}` }

  useEffect(() => {
    fetch(`${API_URL}/api/admin/stats`, { headers: auth })
      .then(async (response) => {
        if (!response.ok) throw new Error(t("admin.load_failed"))
        return response.json()
      })
      .then(setStats)
      .catch((loadError) => setError(loadError.message))
      .finally(() => setLoading(false))
  }, [token, t])

  if (loading) return <div style={{ textAlign: "center", padding: 40 }}><Spinner size={28} /></div>
  if (!stats) return <div style={{ ...card, color: "#fca5a5" }}>{error || t("errors.load_failed")}</div>

  return (
    <div>
      <h2 style={{ fontSize: 16, fontWeight: 700, color: "var(--c-text)", marginBottom: 16 }}>
        {t("admin.global_stats")}
      </h2>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 24 }}>
        <StatCard label={t("admin.users")} value={stats.total_users} />
        <StatCard label={t("admin.table_projects")} value={stats.total_projects} />
        <StatCard label={t("admin.organizations")} value={stats.total_organizations} />
        <StatCard label={t("admin.table_tokens_month")} value={stats.month_tokens.toLocaleString()} />
        <StatCard label={t("dashboard.requests")} value={stats.month_requests.toLocaleString()} />
        <StatCard label={t("admin.cost")} value={`$${stats.month_cost_usd.toFixed(4)}`} />
      </div>
      <div style={{ ...card, color: "var(--c-text-faint)", fontSize: 12 }}>
        {t("admin.updated_at", { value: new Date(stats.timestamp).toLocaleString() })}
      </div>
    </div>
  )
}

function LogsTab({ token }) {
  const { t } = useTranslation()
  const [logs, setLogs] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const auth = { Authorization: `Bearer ${token}` }

  useEffect(() => {
    fetch(`${API_URL}/api/admin/logs`, { headers: auth })
      .then(async (response) => {
        if (!response.ok) throw new Error(t("admin.load_failed"))
        return response.json()
      })
      .then((data) => setLogs(data.entries || []))
      .catch((loadError) => setError(loadError.message))
      .finally(() => setLoading(false))
  }, [token, t])

  if (loading) return <div style={{ textAlign: "center", padding: 40 }}><Spinner size={28} /></div>
  if (error) return <div style={{ ...card, color: "#fca5a5" }}>{error}</div>

  return (
    <div>
      <h2 style={{ fontSize: 16, fontWeight: 700, color: "var(--c-text)", marginBottom: 16 }}>
        {t("admin.logs_title", { count: logs.length })}
      </h2>
      {logs.length === 0 ? (
        <div style={{ ...card, color: "var(--c-text-faint)", fontSize: 13 }}>{t("admin.logs_empty")}</div>
      ) : (
        <div
          style={{
            fontFamily: "monospace",
            fontSize: 12,
            background: "var(--c-panel)",
            border: "1px solid var(--c-border)",
            borderRadius: 8,
            padding: 12,
            maxHeight: 500,
            overflowY: "auto",
          }}
        >
          {logs.map((entry, index) => (
            <div
              key={index}
              style={{
                marginBottom: 4,
                paddingBottom: 4,
                borderBottom: "1px solid var(--c-border)",
                color: "var(--c-text-muted)",
              }}
            >
              {entry.raw ? entry.raw : (
                <span>
                  <span style={{ color: "var(--c-text-faint)" }}>{entry.timestamp || entry.time || ""}</span>
                  {" "}
                  <span style={{ color: entry.level === "ERROR" ? "#f87171" : entry.level === "WARNING" ? "#fbbf24" : "var(--c-accent-lt)" }}>
                    [{entry.level || entry.levelname || "INFO"}]
                  </span>
                  {" "}
                  <span style={{ color: "var(--c-text)" }}>
                    {entry.message || entry.msg || JSON.stringify(entry)}
                  </span>
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function buildAuditQuery(filters, limit = 100) {
  const params = new URLSearchParams()
  params.set("limit", String(limit))
  if (filters.action) params.set("action", filters.action)
  if (filters.date) params.set("date", filters.date)
  return params.toString()
}

function AuditTrailTab({ token }) {
  const { t } = useTranslation()
  const [events, setEvents] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [filters, setFilters] = useState({ action: "", date: "" })
  const auth = { Authorization: `Bearer ${token}` }

  const load = useCallback(async () => {
    setLoading(true)
    setError("")
    try {
      const query = buildAuditQuery(filters, 100)
      const response = await fetch(`${API_URL}/api/admin/audit?${query}`, { headers: auth })
      if (!response.ok) throw new Error(t("admin.load_failed"))
      const data = await response.json()
      setEvents(data.events || [])
    } catch (loadError) {
      setError(loadError.message)
      setEvents([])
    } finally {
      setLoading(false)
    }
  }, [filters, token, t])

  useEffect(() => {
    load()
  }, [load])

  async function exportCsv() {
    setError("")
    try {
      const query = buildAuditQuery(filters, 1000)
      const response = await fetch(`${API_URL}/api/admin/audit/export?${query}`, { headers: auth })
      if (!response.ok) throw new Error(t("admin.load_failed"))
      const blob = await response.blob()
      const url = window.URL.createObjectURL(blob)
      const link = document.createElement("a")
      link.href = url
      link.download = "audit_trail.csv"
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.URL.revokeObjectURL(url)
    } catch (exportError) {
      setError(exportError.message)
    }
  }

  if (loading) return <div style={{ textAlign: "center", padding: 40 }}><Spinner size={28} /></div>

  return (
    <div>
      <div style={{ ...card, display: "flex", gap: 12, flexWrap: "wrap", alignItems: "end" }}>
        <label style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 160 }}>
          <span style={{ fontSize: 12, color: "var(--c-text-muted)", fontWeight: 600 }}>
            {t("admin.action")}
          </span>
          <select
            value={filters.action}
            onChange={(event) => setFilters((previous) => ({ ...previous, action: event.target.value }))}
            style={filterInput}
          >
            <option value="">{t("common.all")}</option>
            <option value="upload">{t("admin.action_upload")}</option>
            <option value="chat">{t("admin.action_chat")}</option>
            <option value="export">{t("admin.action_export")}</option>
            <option value="delete">{t("admin.action_delete")}</option>
          </select>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 180 }}>
          <span style={{ fontSize: 12, color: "var(--c-text-muted)", fontWeight: 600 }}>
            {t("admin.date_min")}
          </span>
          <input
            type="date"
            value={filters.date}
            onChange={(event) => setFilters((previous) => ({ ...previous, date: event.target.value }))}
            style={filterInput}
          />
        </label>

        <button onClick={load} style={actionBtn("#2d8ff0")}>{t("common.refresh")}</button>
        <button onClick={exportCsv} style={actionBtn("#22c55e")}>{t("admin.export_csv")}</button>
      </div>

      {error && <div style={{ ...card, color: "#fca5a5" }}>{error}</div>}

      <h2 style={{ fontSize: 16, fontWeight: 700, color: "var(--c-text)", marginBottom: 16 }}>
        {t("admin.audit")} ({events.length})
      </h2>

      {events.length === 0 ? (
        <div style={{ ...card, color: "var(--c-text-faint)", fontSize: 13 }}>{t("admin.audit_empty")}</div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ background: "var(--c-panel)", color: "var(--c-text-muted)", fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em" }}>
                {[
                  t("admin.timestamp"),
                  t("admin.action"),
                  t("admin.model"),
                  t("admin.mode"),
                  t("admin.masked_entities"),
                  t("admin.cost"),
                  t("admin.status"),
                ].map((header) => (
                  <th key={header} style={{ padding: "8px 12px", textAlign: "left", borderBottom: "1px solid var(--c-border)" }}>
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {events.map((event) => (
                <tr key={event.event_id} style={{ borderBottom: "1px solid var(--c-border)" }}>
                  <td style={{ padding: "10px 12px", color: "var(--c-text-faint)", fontSize: 12 }}>
                    {event.timestamp ? new Date(event.timestamp).toLocaleString() : "-"}
                  </td>
                  <td style={{ padding: "10px 12px" }}>
                    <span style={badge("#2d8ff0")}>{event.action}</span>
                  </td>
                  <td style={{ padding: "10px 12px", color: "var(--c-text-muted)" }}>{event.model_used || "-"}</td>
                  <td style={{ padding: "10px 12px" }}>
                    <span style={badge(event.protection_mode === "strict" ? "#ef4444" : event.protection_mode === "fast" ? "#60a5fa" : "#22c55e")}>
                      {event.protection_mode || "-"}
                    </span>
                  </td>
                  <td style={{ padding: "10px 12px", color: "var(--c-text-muted)" }}>{event.entities_masked ?? 0}</td>
                  <td style={{ padding: "10px 12px", color: "var(--c-text-muted)" }}>${Number(event.cost_usd ?? 0).toFixed(4)}</td>
                  <td style={{ padding: "10px 12px" }}>
                    <span style={badge(event.success ? "#22c55e" : "#ef4444")}>
                      {event.success
                        ? t("common.status_ok")
                        : `${t("common.status_failed")}${event.error_code ? `:${event.error_code}` : ""}`}
                    </span>
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

function MaintenanceTab({ token }) {
  const { t } = useTranslation()
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState("")
  const auth = { Authorization: `Bearer ${token}` }

  async function purge() {
    setLoading(true)
    setError("")
    try {
      const response = await fetch(`${API_URL}/api/admin/maintenance/purge-expired`, {
        method: "POST",
        headers: auth,
      })
      if (!response.ok) throw new Error(t("admin.load_failed"))
      const data = await response.json()
      setResult(data)
    } catch (purgeError) {
      setError(purgeError.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <h2 style={{ fontSize: 16, fontWeight: 700, color: "var(--c-text)", marginBottom: 16 }}>
        {t("admin.maintenance")}
      </h2>
      <div style={card}>
        <h3 style={{ fontSize: 14, fontWeight: 600, color: "var(--c-text)", marginBottom: 8 }}>
          {t("admin.maintenance_title")}
        </h3>
        <p style={{ fontSize: 13, color: "var(--c-text-muted)", marginBottom: 16 }}>
          {t("admin.maintenance_help")}
        </p>
        <button
          onClick={purge}
          disabled={loading}
          style={{
            padding: "8px 20px",
            background: "linear-gradient(135deg,#1a6fcc,#2d8ff0)",
            border: "none",
            borderRadius: 8,
            color: "#fff",
            fontWeight: 600,
            fontSize: 13,
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            gap: 8,
          }}
        >
          {loading && <Spinner size={14} />}
          {loading ? t("admin.maintenance_running") : t("admin.maintenance_run")}
        </button>
        {error && <div style={{ marginTop: 12, color: "#fca5a5", fontSize: 13 }}>{error}</div>}
        {result && (
          <div
            style={{
              marginTop: 16,
              padding: 12,
              background: "#0a2a1a",
              border: "1px solid #22c55e44",
              borderRadius: 8,
              fontSize: 12,
              fontFamily: "monospace",
              color: "#86efac",
            }}
          >
            <div>{t("admin.vault_keys_found", { count: result.vault_keys_found })}</div>
            <div>{t("admin.excel_keys_found", { count: result.excel_keys_found })}</div>
            <div>{t("admin.purged_keys", { count: result.purged })}</div>
            <div style={{ color: "var(--c-text-faint)", marginTop: 4 }}>{result.timestamp}</div>
          </div>
        )}
      </div>
    </div>
  )
}

export default function AdminDashboard({ token }) {
  const { t } = useTranslation()
  const [tab, setTab] = useState("requests")
  const [pendingCount, setPendingCount] = useState(0)
  const navigate = useNavigate()
  const tabLabels = {
    requests: "Demandes d'accès",
    users: t("admin.users"),
    orgs: t("admin.organizations"),
    stats: t("admin.stats"),
    audit: t("admin.audit"),
    logs: t("admin.logs"),
    maintenance: t("admin.maintenance"),
  }

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <div
        style={{
          borderBottom: "1px solid var(--c-border)",
          padding: "0 24px",
          display: "flex",
          alignItems: "center",
          gap: 0,
          background: "var(--c-surface)",
        }}
      >
        <button
          onClick={() => navigate("/")}
          style={{
            background: "none",
            border: "none",
            color: "var(--c-text-faint)",
            cursor: "pointer",
            fontSize: 20,
            padding: "0 12px 0 0",
            lineHeight: "50px",
          }}
          title={t("common.back")}
        >
          &#8592;
        </button>
        <span
          style={{
            fontSize: 14,
            fontWeight: 700,
            color: "var(--c-text)",
            fontFamily: "var(--font-title)",
            marginRight: 24,
          }}
        >
          {t("admin.title")}
        </span>
        {TABS.map((tabKey) => (
          <button
            key={tabKey}
            onClick={() => setTab(tabKey)}
            style={{
              padding: "0 16px",
              height: 49,
              background: "none",
              border: "none",
              borderBottom: tab === tabKey ? "2px solid var(--c-accent)" : "2px solid transparent",
              color: tab === tabKey ? "var(--c-accent-lt)" : "var(--c-text-muted)",
              fontSize: 13,
              fontWeight: tab === tabKey ? 600 : 400,
              cursor: "pointer",
              transition: "all .12s",
            }}
          >
            <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
              <span>{tabLabels[tabKey]}</span>
              {tabKey === "requests" && pendingCount > 0 && (
                <span style={badge("#ef4444")}>{pendingCount}</span>
              )}
            </span>
          </button>
        ))}
      </div>

      <div style={{ flex: 1, overflowY: "auto", padding: 24 }}>
        {tab === "requests" && <AccessRequestsTab token={token} onPendingCount={setPendingCount} />}
        {tab === "users" && <UsersTab token={token} />}
        {tab === "orgs" && <OrgsTab token={token} />}
        {tab === "stats" && <StatsTab token={token} />}
        {tab === "audit" && <AuditTrailTab token={token} />}
        {tab === "logs" && <LogsTab token={token} />}
        {tab === "maintenance" && <MaintenanceTab token={token} />}
      </div>
    </div>
  )
}

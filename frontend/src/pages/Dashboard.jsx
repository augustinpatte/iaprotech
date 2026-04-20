import React, { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router-dom"
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import { API_URL, ShieldSvg, Spinner } from "../App.jsx"

function formatCurrency(value) {
  return `$${Number(value ?? 0).toFixed(4)}`
}

function formatPercent(value) {
  return `${Number(value ?? 0).toFixed(1)}%`
}

function displayModelName(modelId, t) {
  if (modelId === "local-processing") return t("dashboard.local_processing")
  return modelId
}

function ProgressCard({ label, value, sub, percent, tone = "var(--c-accent)" }) {
  const safePercent = Math.max(0, Math.min(100, Number(percent ?? 0)))
  return (
    <div style={{
      background: "var(--c-card)",
      border: "1px solid var(--c-border)",
      borderRadius: 12,
      padding: "20px 24px",
    }}>
      <div style={{ fontSize: 12, color: "var(--c-text-muted)", marginBottom: 8, textTransform: "uppercase", letterSpacing: ".06em", fontWeight: 500 }}>
        {label}
      </div>
      <div className="font-title" style={{ fontSize: 28, fontWeight: 700, color: "var(--c-text)", lineHeight: 1.1 }}>
        {value}
      </div>
      {sub && <div style={{ fontSize: 12, color: "var(--c-text-faint)", marginTop: 6 }}>{sub}</div>}
      {percent !== undefined && (
        <>
          <div style={{ marginTop: 14, height: 8, background: "var(--c-panel)", borderRadius: 999, overflow: "hidden" }}>
            <div style={{
              width: `${safePercent}%`,
              height: "100%",
              background: safePercent > 85 ? "#ef4444" : safePercent > 65 ? "#f59e0b" : tone,
              borderRadius: 999,
              transition: "width .35s ease",
            }} />
          </div>
          <div style={{ marginTop: 6, fontSize: 11, color: "var(--c-text-faint)" }}>{formatPercent(safePercent)}</div>
        </>
      )}
    </div>
  )
}

function DashboardTooltip({ active, payload, label, t }) {
  if (!active || !payload?.length) return null
  const tokens = payload.find((entry) => entry.dataKey === "tokens")
  const cost = payload.find((entry) => entry.dataKey === "cost_usd")
  return (
    <div style={{ background: "var(--c-panel)", border: "1px solid var(--c-border)", borderRadius: 8, padding: "8px 14px", fontSize: 12 }}>
      <div style={{ color: "var(--c-text-muted)", marginBottom: 6 }}>{label}</div>
      <div style={{ color: "#2d8ff0", fontWeight: 600 }}>{t("dashboard.table_tokens")}: {(tokens?.value ?? 0).toLocaleString()}</div>
      <div style={{ color: "#22c55e", fontWeight: 600 }}>{t("dashboard.table_cost")}: {formatCurrency(cost?.value ?? 0)}</div>
    </div>
  )
}

const tableCell = {
  padding: "12px 16px",
  fontSize: 13,
  color: "var(--c-text-muted)",
  verticalAlign: "middle",
  borderTop: "1px solid var(--c-border)",
}

export default function Dashboard({ token }) {
  const { t } = useTranslation()
  const [current, setCurrent] = useState(null)
  const [history, setHistory] = useState([])
  const [plan, setPlan] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")

  const headers = { Authorization: `Bearer ${token}` }

  useEffect(() => {
    async function load() {
      setLoading(true)
      setError("")
      try {
        const [rc, rh, rp] = await Promise.allSettled([
          fetch(`${API_URL}/dashboard/usage/current`, { headers }),
          fetch(`${API_URL}/dashboard/usage/history`, { headers }),
          fetch(`${API_URL}/dashboard/usage/plan`, { headers }),
        ])

        if (rc.status === "fulfilled" && rc.value.ok) {
          setCurrent(await rc.value.json())
        }
        if (rh.status === "fulfilled" && rh.value.ok) {
          const historyPayload = await rh.value.json()
          setHistory(historyPayload.days || [])
        }
        if (rp.status === "fulfilled" && rp.value.ok) {
          setPlan(await rp.value.json())
        }
        if (
          (rc.status !== "fulfilled" || !rc.value.ok)
          && (rh.status !== "fulfilled" || !rh.value.ok)
          && (rp.status !== "fulfilled" || !rp.value.ok)
        ) {
          throw new Error(t("dashboard.load_failed"))
        }
      } catch (err) {
        setError(err.message)
      } finally {
        setLoading(false)
      }
    }

    load()
  }, [token, t])

  const chartData = history.map((day) => ({
    date: day.date ? day.date.slice(5) : "",
    tokens: day.tokens ?? 0,
    cost_usd: day.cost_usd ?? 0,
  }))
  const modelRows = Object.entries(current?.breakdown_by_model ?? {})
  const pricingRows = Object.entries(current?.pricing_catalog ?? {})

  return (
    <div style={{ flex: 1, overflowY: "auto", background: "var(--c-bg)", padding: "24px 32px" }} className="scrollbar-none">
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 24, fontSize: 13, color: "var(--c-text-muted)" }}>
        <Link to="/" style={{ color: "var(--c-accent-lt)", textDecoration: "none" }}>{t("dashboard.chat_breadcrumb")}</Link>
        <span>/</span>
        <span style={{ color: "var(--c-text)" }}>{t("dashboard.page_title")}</span>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 28 }}>
        <ShieldSvg size={28} />
        <h1 className="font-title" style={{ fontSize: 22, fontWeight: 700, color: "var(--c-text)" }}>
          {t("dashboard.page_heading")}
        </h1>
        {plan && (
          <span style={{ fontSize: 12, padding: "3px 10px", background: "var(--c-accent-dim)", border: "1px solid var(--c-accent)", borderRadius: 20, color: "var(--c-accent-lt)", fontWeight: 600 }}>
            {plan.plan_name}
          </span>
        )}
      </div>

        {loading && <div style={{ textAlign: "center", padding: 48 }}><Spinner size={28} /></div>}
      {error && <div style={{ color: "#f87171", marginBottom: 16 }}>{error}</div>}

      {!loading && current && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4,minmax(0,1fr))", gap: 16, marginBottom: 28 }}>
            <ProgressCard
              label={t("dashboard.cards.tokens_month")}
              value={(current.total_tokens ?? 0).toLocaleString()}
              sub={t("dashboard.cards.tokens_remaining", { count: Math.max(0, (current.plan_limit_tokens ?? 0) - (current.total_tokens ?? 0)).toLocaleString() })}
              percent={current.percent_tokens_used}
            />
            <ProgressCard
              label={t("dashboard.cards.cost_month")}
              value={formatCurrency(current.total_cost_usd)}
              sub={t("dashboard.cards.cost_budget", { value: formatCurrency(current.plan_limit_cost_usd) })}
              percent={current.percent_cost_used}
              tone="#22c55e"
            />
            <ProgressCard
              label={t("dashboard.cards.requests_month")}
              value={(current.requests_count ?? 0).toLocaleString()}
              sub={t("dashboard.cards.projected_monthly_cost", { value: formatCurrency(current.projected_monthly_cost) })}
            />
            <ProgressCard
              label={t("dashboard.cards.avg_cost_request")}
              value={formatCurrency(current.cost_per_request_avg)}
              sub={t("dashboard.cards.avg_cost_help")}
            />
          </div>

          <div style={{ background: "var(--c-card)", border: "1px solid var(--c-border)", borderRadius: 12, padding: "16px 24px", marginBottom: 28 }}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: 24, flexWrap: "wrap", fontSize: 13 }}>
              <div style={{ color: "var(--c-text-muted)" }}>
                {t("dashboard.summary.tokens_used")}: <strong style={{ color: "var(--c-text)" }}>{(current.total_tokens ?? 0).toLocaleString()}</strong> / {(current.plan_limit_tokens ?? 0).toLocaleString()}
              </div>
              <div style={{ color: "var(--c-text-muted)" }}>
                {t("dashboard.summary.cost_used")}: <strong style={{ color: "var(--c-text)" }}>{formatCurrency(current.total_cost_usd)}</strong> / {formatCurrency(current.plan_limit_cost_usd)}
              </div>
              <div style={{ color: "var(--c-text-muted)" }}>
                {t("dashboard.summary.providers_active")}: <strong style={{ color: "var(--c-text)" }}>{Object.keys(current.breakdown_by_provider ?? {}).length}</strong>
              </div>
            </div>
            <div style={{ marginTop: 12, fontSize: 12, color: "var(--c-text-faint)" }}>
              {t("dashboard.estimated_note")}
            </div>
          </div>

          {chartData.length > 0 && (
            <div style={{ background: "var(--c-card)", border: "1px solid var(--c-border)", borderRadius: 12, padding: "20px 24px", marginBottom: 28 }}>
              <div className="font-title" style={{ fontSize: 15, fontWeight: 600, color: "var(--c-text)", marginBottom: 18 }}>
                {t("dashboard.chart_title")}
              </div>
              <ResponsiveContainer width="100%" height={280}>
                <ComposedChart data={chartData} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e3a5640" vertical={false} />
                  <XAxis dataKey="date" tick={{ fill: "#6a90b8", fontSize: 11 }} axisLine={false} tickLine={false} />
                  <YAxis yAxisId="tokens" tick={{ fill: "#6a90b8", fontSize: 11 }} axisLine={false} tickLine={false} width={54} />
                  <YAxis yAxisId="cost" orientation="right" tick={{ fill: "#86efac", fontSize: 11 }} axisLine={false} tickLine={false} width={54} />
                  <Tooltip content={<DashboardTooltip t={t} />} />
                  <Legend />
                  <Bar yAxisId="tokens" dataKey="tokens" name={t("dashboard.table_tokens")} fill="#2d8ff0" radius={[4, 4, 0, 0]} />
                  <Line yAxisId="cost" type="monotone" dataKey="cost_usd" name={t("dashboard.cost_usd")} stroke="#22c55e" strokeWidth={2} dot={false} />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          )}

          <div style={{ background: "var(--c-card)", border: "1px solid var(--c-border)", borderRadius: 12, overflow: "hidden", marginBottom: 28 }}>
            <div style={{ padding: "16px 24px", borderBottom: "1px solid var(--c-border)" }}>
              <span className="font-title" style={{ fontSize: 15, fontWeight: 600, color: "var(--c-text)" }}>
                {t("dashboard.breakdown_title")}
              </span>
            </div>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr style={{ background: "var(--c-panel)" }}>
                    {[
                      t("dashboard.table_model"),
                      t("dashboard.table_provider"),
                      t("dashboard.table_tokens"),
                      t("dashboard.table_cost"),
                      t("dashboard.table_requests"),
                      t("dashboard.table_price_input"),
                      t("dashboard.table_price_output"),
                    ].map((header) => (
                      <th key={header} style={{ padding: "10px 16px", textAlign: "left", fontSize: 11, fontWeight: 600, color: "var(--c-text-muted)", textTransform: "uppercase", letterSpacing: ".06em" }}>
                        {header}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {modelRows.map(([modelId, stats]) => (
                    <tr key={modelId}>
                      <td style={{ ...tableCell, color: "var(--c-text)", fontWeight: 600 }}>{displayModelName(modelId, t)}</td>
                      <td style={tableCell}>{stats.provider || "-"}</td>
                      <td style={tableCell}>{(stats.tokens ?? 0).toLocaleString()}</td>
                      <td style={tableCell}>{formatCurrency(stats.cost_usd)}</td>
                      <td style={tableCell}>{(stats.requests ?? 0).toLocaleString()}</td>
                      <td style={tableCell}>{formatCurrency(stats.price_input_per_1k)}</td>
                      <td style={tableCell}>{formatCurrency(stats.price_output_per_1k)}</td>
                    </tr>
                  ))}
                  {modelRows.length === 0 && (
                    <tr>
                      <td style={tableCell} colSpan={7}>{t("dashboard.empty_month")}</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div style={{ background: "var(--c-card)", border: "1px solid var(--c-border)", borderRadius: 12, padding: "20px 24px" }}>
            <div className="font-title" style={{ fontSize: 15, fontWeight: 600, color: "var(--c-text)", marginBottom: 16 }}>
              {t("dashboard.cost_equation")}
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12 }}>
              {pricingRows.map(([modelId, pricing]) => (
                <div key={modelId} style={{ border: "1px solid var(--c-border)", borderRadius: 10, padding: "14px 16px", background: "var(--c-panel)" }}>
                  <div style={{ fontSize: 13, fontWeight: 700, color: "var(--c-text)", marginBottom: 8 }}>{displayModelName(modelId, t)}</div>
                  <div style={{ fontSize: 12, color: "var(--c-text-muted)", marginBottom: 4 }}>
                    {t("dashboard.equation_input", { value: formatCurrency(pricing.price_input_per_1k) })}
                  </div>
                  <div style={{ fontSize: 12, color: "var(--c-text-muted)", marginBottom: 4 }}>
                    {t("dashboard.equation_output", { value: formatCurrency(pricing.price_output_per_1k) })}
                  </div>
                  <div style={{ fontSize: 12, color: "var(--c-accent-lt)" }}>
                    {pricing.is_observed
                      ? t("dashboard.equation_observed", { value: formatCurrency(pricing.average_message_cost_usd) })
                      : t("dashboard.equation_estimated", { value: formatCurrency(pricing.average_message_cost_usd) })}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  )
}

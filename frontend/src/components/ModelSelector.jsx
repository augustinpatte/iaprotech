import React, { useState, useRef, useEffect } from "react"
import { useTranslation } from "react-i18next"

const COLOR_BY_MODEL = {
  auto: "#94a3b8",
  "claude-sonnet-4-5": "#38bdf8",
  "claude-opus-4-5": "#0ea5e9",
  "gpt-4o": "#4ade80",
  "gemini-1.5-pro": "#facc15",
  "mistral-large-latest": "#f87171",
}

function modelOptions(t) {
  return [
    { id: null, label: t("models.auto") },
    { id: "claude-sonnet-4-5", label: t("models.anthropic") },
    { id: "gpt-4o", label: t("models.openai") },
    { id: "gemini-1.5-pro", label: t("models.google") },
    { id: "mistral-large-latest", label: t("models.mistral") },
  ]
}

function ColorDot({ color }) {
  return (
    <span
      style={{
        width: 8,
        height: 8,
        borderRadius: "50%",
        background: color,
        display: "inline-block",
        flexShrink: 0,
      }}
    />
  )
}

export default function ModelSelector({ value, onChange }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  const models = modelOptions(t)
  const current = models.find((model) => model.id === value) ?? models[0]

  useEffect(() => {
    function handler(event) {
      if (ref.current && !ref.current.contains(event.target)) setOpen(false)
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [])

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        onClick={() => setOpen((previous) => !previous)}
        style={{
          background: "none",
          border: "none",
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          gap: 6,
          color: "var(--c-text-muted)",
          fontSize: 12,
          padding: "4px 6px",
          borderRadius: 6,
          transition: "color .12s",
        }}
        onMouseEnter={(event) => { event.currentTarget.style.color = "var(--c-text)" }}
        onMouseLeave={(event) => { event.currentTarget.style.color = "var(--c-text-muted)" }}
      >
        <ColorDot color={COLOR_BY_MODEL[current.id ?? "auto"] ?? COLOR_BY_MODEL.auto} />
        <span>{current.label}</span>
        <span style={{ fontSize: 9, opacity: 0.6 }}>&#9650;</span>
      </button>

      {open && (
        <div
          style={{
            position: "absolute",
            bottom: "calc(100% + 6px)",
            left: 0,
            background: "var(--c-panel)",
            border: "1px solid var(--c-border)",
            borderRadius: 10,
            padding: "4px 0",
            minWidth: 180,
            zIndex: 100,
            boxShadow: "0 -8px 24px #00000055",
          }}
        >
          {models.map((model) => (
            <button
              key={String(model.id)}
              onClick={() => { onChange(model.id); setOpen(false) }}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                width: "100%",
                padding: "8px 14px",
                background: model.id === value ? "var(--c-accent-dim)" : "none",
                border: "none",
                cursor: "pointer",
                color: model.id === value ? "var(--c-accent-lt)" : "var(--c-text)",
                fontSize: 13,
                textAlign: "left",
                transition: "background .1s",
              }}
              onMouseEnter={(event) => {
                if (model.id !== value) event.currentTarget.style.background = "var(--c-card)"
              }}
              onMouseLeave={(event) => {
                if (model.id !== value) event.currentTarget.style.background = "none"
              }}
            >
              <ColorDot color={COLOR_BY_MODEL[model.id ?? "auto"] ?? COLOR_BY_MODEL.auto} />
              <span>{model.label}</span>
              {model.id === value && <span style={{ marginLeft: "auto", fontSize: 11 }}>&#10003;</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

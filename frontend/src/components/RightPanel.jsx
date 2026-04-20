import React, { useState } from "react"
import { useNavigate } from "react-router-dom"

function IconBtn({ icon, label, onClick, badge }) {
  const [hover, setHover] = useState(false)
  return (
    <div
      style={{ position: "relative" }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <button
        onClick={onClick}
        style={{
          width: 36,
          height: 36,
          borderRadius: 8,
          background: hover ? "var(--c-card)" : "transparent",
          border: "none",
          cursor: onClick ? "pointer" : "default",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: 17,
          transition: "background .15s",
          position: "relative",
        }}
      >
        {icon}
        {badge > 0 && (
          <span
            style={{
              position: "absolute",
              top: 4,
              right: 4,
              width: 14,
              height: 14,
              borderRadius: "50%",
              background: "var(--c-accent)",
              color: "#fff",
              fontSize: 9,
              fontWeight: 700,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              lineHeight: 1,
            }}
          >
            {badge > 9 ? "9+" : badge}
          </span>
        )}
      </button>
      {hover && (
        <div
          style={{
            position: "absolute",
            right: "calc(100% + 8px)",
            top: "50%",
            transform: "translateY(-50%)",
            background: "var(--c-card)",
            border: "1px solid var(--c-border)",
            borderRadius: 6,
            padding: "4px 10px",
            whiteSpace: "nowrap",
            fontSize: 12,
            color: "var(--c-text)",
            zIndex: 200,
            pointerEvents: "none",
            boxShadow: "0 4px 12px #00000044",
          }}
        >
          {label}
        </div>
      )}
    </div>
  )
}

export default function RightPanel({ hiddenCount = 0 }) {
  const navigate = useNavigate()

  return (
    <aside
      style={{
        width: 52,
        minWidth: 52,
        background: "var(--c-surface)",
        borderLeft: "1px solid var(--c-border)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        paddingTop: 12,
        gap: 4,
      }}
    >
      <IconBtn icon="🔒" label="Entités masquées" badge={hiddenCount} />
      <IconBtn icon="📊" label="Dashboard" onClick={() => navigate("/dashboard")} />
      <IconBtn icon="⚙️" label="Paramètres" onClick={() => navigate("/settings")} />
    </aside>
  )
}

import React from "react"
import { ShieldSvg } from "../App.jsx"

const ATTACHMENT_PREFIX_RE = /^\[Document joint: ([^\]]+)\]\s*(?:\n\n)?/i

function EntityBadge({ text }) {
  return (
    <span style={{
      display: "inline-block",
      background: "var(--c-accent-dim)",
      border: "1px solid var(--c-accent)",
      color: "var(--c-accent-lt)",
      borderRadius: 4,
      padding: "1px 6px",
      fontSize: 11,
      fontFamily: "monospace",
      fontWeight: 600,
      lineHeight: 1.5,
      verticalAlign: "middle",
      margin: "0 2px",
    }}>{text}</span>
  )
}

function AttachmentChip({ name }) {
  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 8,
        padding: "6px 10px",
        borderRadius: 10,
        border: "1px solid var(--c-border)",
        background: "rgba(255,255,255,0.06)",
        marginBottom: 10,
        maxWidth: "100%",
      }}
    >
      <span style={{ fontSize: 15, lineHeight: 1 }}>{"\uD83D\uDCCE"}</span>
      <span
        style={{
          fontSize: 12,
          fontWeight: 600,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {name}
      </span>
    </div>
  )
}

function parseAttachment(content, attachments) {
  const parsedAttachments = Array.isArray(attachments) ? [...attachments] : []
  const text = String(content ?? "")
  const match = text.match(ATTACHMENT_PREFIX_RE)
  if (!match) return { content: text, attachments: parsedAttachments }

  parsedAttachments.unshift({ name: match[1] })
  return {
    content: text.slice(match[0].length),
    attachments: parsedAttachments,
  }
}

function renderInline(text) {
  if (!text) return null

  const parts = text.split(/(\[[A-Z_]+_\d+\]|\*\*[^*\n]+\*\*)/g)
  return parts.map((part, index) => {
    if (!part) return null
    if (/^\[[A-Z_]+_\d+\]$/.test(part)) {
      return <EntityBadge key={index} text={part} />
    }
    if (/^\*\*[^*\n]+\*\*$/.test(part)) {
      return <strong key={index}>{part.slice(2, -2)}</strong>
    }
    return <span key={index}>{part}</span>
  })
}

function renderRichText(content) {
  const text = String(content ?? "")
    .replace(/\r\n/g, "\n")
    .replace(/([^\n])\s+(#{1,3}\s)/g, "$1\n$2")
    .replace(/([^\n])\s+(-\s+\*\*)/g, "$1\n$2")
    .replace(/([^\n])\s+(-\s+[A-ZÀ-ÿ0-9\[])/g, "$1\n$2")
    .trim()
  if (!text) return null

  const lines = text.split("\n")
  return lines.map((rawLine, index) => {
    const line = rawLine.trimEnd()
    if (!line.trim()) {
      return <div key={`spacer-${index}`} style={{ height: 8 }} />
    }
    if (line.startsWith("### ")) {
      return (
        <div key={`h3-${index}`} style={{ fontWeight: 700, fontSize: 14, marginTop: index === 0 ? 0 : 8 }}>
          {renderInline(line.slice(4))}
        </div>
      )
    }
    if (line.startsWith("## ")) {
      return (
        <div key={`h2-${index}`} style={{ fontWeight: 800, fontSize: 15, marginTop: index === 0 ? 0 : 8 }}>
          {renderInline(line.slice(3))}
        </div>
      )
    }
    if (line.startsWith("# ")) {
      return (
        <div key={`h1-${index}`} style={{ fontWeight: 800, fontSize: 16, marginTop: index === 0 ? 0 : 10 }}>
          {renderInline(line.slice(2))}
        </div>
      )
    }
    if (line.startsWith("- ") || line.startsWith("* ")) {
      return (
        <div key={`li-${index}`} style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
          <span style={{ lineHeight: 1.7 }}>{"\u2022"}</span>
          <div style={{ flex: 1 }}>{renderInline(line.slice(2))}</div>
        </div>
      )
    }
    return (
      <div key={`p-${index}`} style={{ whiteSpace: "pre-wrap" }}>
        {renderInline(line)}
      </div>
    )
  })
}

export default function MessageBubble({ role, content, attachments = [] }) {
  const isUser = role === "user"
  const parsed = parseAttachment(content, attachments)

  if (isUser) {
    return (
      <div className="animate-fade-in" style={{ display: "flex", justifyContent: "flex-end", marginBottom: 16, gap: 10 }}>
        <div style={{
          maxWidth: "70%",
          background: "linear-gradient(135deg,#1a6fcc,#2d8ff0)",
          borderRadius: "14px 14px 4px 14px",
          padding: "11px 15px",
          color: "#fff",
          fontSize: 14,
          lineHeight: 1.65,
          boxShadow: "0 2px 12px #1a6fcc33",
        }}>
          {parsed.attachments.map((attachment, index) => (
            <AttachmentChip key={`${attachment.name}-${index}`} name={attachment.name} />
          ))}
          {renderRichText(parsed.content)}
        </div>
        <div style={{
          width: 30, height: 30, borderRadius: "50%", flexShrink: 0,
          background: "linear-gradient(135deg,#1a6fcc,#2d8ff0)",
          display: "flex", alignItems: "center", justifyContent: "center",
          color: "#fff", fontSize: 11, fontWeight: 700,
          fontFamily: "var(--font-title)", marginTop: 2,
        }}>AP</div>
      </div>
    )
  }

  return (
    <div className="animate-fade-in" style={{ display: "flex", justifyContent: "flex-start", marginBottom: 16, gap: 10 }}>
      <div style={{
        width: 30, height: 30, borderRadius: "50%", flexShrink: 0,
        background: "var(--c-panel)", border: "1px solid var(--c-border)",
        display: "flex", alignItems: "center", justifyContent: "center",
        marginTop: 2,
      }}>
        <ShieldSvg size={16} />
      </div>
      <div style={{
        maxWidth: "72%",
        background: "var(--c-card)", border: "1px solid var(--c-border)",
        borderRadius: "4px 14px 14px 14px",
        padding: "11px 15px",
        color: "var(--c-text)", fontSize: 14, lineHeight: 1.7,
      }}>
        {parsed.attachments.map((attachment, index) => (
          <AttachmentChip key={`${attachment.name}-${index}`} name={attachment.name} />
        ))}
        {renderRichText(parsed.content)}
      </div>
    </div>
  )
}

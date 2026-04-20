import React, { useState, useRef, useEffect, useCallback } from "react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router-dom"
import { API_URL, ShieldSvg, Spinner } from "../App.jsx"
import MessageBubble from "./MessageBubble.jsx"
import ModelSelector from "./ModelSelector.jsx"

const ACCEPT = ".pdf,.docx,.doc,.odt,.xlsx,.xls,.ods,.csv,.tsv,.pptx,.ppt,.odp,.txt,.rtf,.md,.jpg,.jpeg,.png,.gif,.webp,.bmp,.tiff,.tif,.eml,.msg,.vtt,.srt,.html,.htm,.json,.xml,.yaml,.yml"
const PROTECTION_STORAGE_PREFIX = "privacy-proxy:protection-mode:"
const MODEL_STORAGE_PREFIX = "privacy-proxy:model:"
const VALID_MODEL_IDS = new Set([
  "claude-sonnet-4-5",
  "claude-opus-4-5",
  "gpt-4o",
  "gemini-1.5-pro",
  "mistral-large-latest",
])
const MAX_DOCUMENT_CONTEXT_CHARS = 8000
const PIPELINE_STEPS = ["preparing", "policy", "history", "redacting", "provider", "restoring", "saving", "done"]
const OCR_IMAGE_EXTS = new Set(["jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif"])

function buildDocumentSystemContext(uploadResult, filename) {
  const redactedText = String(uploadResult?.redacted_text ?? "").trim()
  if (!redactedText) return null

  const excerpt = redactedText.length > MAX_DOCUMENT_CONTEXT_CHARS
    ? `${redactedText.slice(0, MAX_DOCUMENT_CONTEXT_CHARS)}\n\n[Document tronque pour tenir dans le contexte]`
    : redactedText

  return [
    `Document joint pseudonymise: ${uploadResult?.filename ?? filename ?? "document"}`,
    "Utilise ce document comme contexte principal pour repondre a la question de l'utilisateur.",
    "Si l'information n'est pas dans le document, dis-le clairement.",
    "",
    excerpt,
  ].join("\n")
}

function buildAttachmentMeta(uploadResult, uploadFile) {
  const name = uploadResult?.filename ?? uploadFile?.name
  if (!name) return []
  return [{ name, type: uploadResult?.file_type ?? "" }]
}

const PROTECTION_OPTIONS = [
  { value: "fast", color: "#38bdf8" },
  { value: "privacy", color: "#4ade80" },
  { value: "strict", color: "#f87171" },
]

function EmptyState() {
  const { t } = useTranslation()

  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 20,
        color: "var(--c-text-muted)",
        userSelect: "none",
      }}
    >
      <div className="animate-float">
        <ShieldSvg size={76} className="shield-glow" />
      </div>
      <div style={{ textAlign: "center" }}>
        <div
          className="font-title"
          style={{ fontSize: 18, fontWeight: 600, color: "var(--c-text)", marginBottom: 6 }}
        >
          {t("chat.ready")}
        </div>
        <div style={{ fontSize: 13, color: "var(--c-text-muted)", maxWidth: 320 }}>
          {t("chat.ready_subtitle")}
        </div>
      </div>
    </div>
  )
}

function SessionExpireBadge({ expiresAt }) {
  const { t } = useTranslation()
  if (!expiresAt) return null

  const msLeft = new Date(expiresAt) - Date.now()
  if (msLeft <= 0) return null

  const hoursLeft = Math.ceil(msLeft / 3_600_000)
  const label =
    hoursLeft < 2
      ? t("chat.session_expires_minutes", { count: Math.ceil(msLeft / 60_000) })
      : t("chat.session_expires_hours", { count: hoursLeft })

  return (
    <span
      title={t("chat.session_expires_tooltip")}
      style={{
        fontSize: 11,
        color: "var(--c-text-muted)",
        padding: "2px 7px",
        borderRadius: 4,
        border: "1px solid var(--c-border)",
        whiteSpace: "nowrap",
      }}
    >
      {label}
    </span>
  )
}

export default function ChatInterface({
  token,
  username,
  activeProjectId,
  onProjectCreated,
  onHiddenCountChange,
}) {
  const { t } = useTranslation()
  const protectionOptions = [
    { ...PROTECTION_OPTIONS[0], label: t("protection.fast"), tooltip: t("protection.fast_tooltip") },
    { ...PROTECTION_OPTIONS[1], label: t("protection.privacy"), tooltip: t("protection.privacy_tooltip") },
    { ...PROTECTION_OPTIONS[2], label: t("protection.strict"), tooltip: t("protection.strict_tooltip") },
  ]

  const [messages, setMessages] = useState([])
  const [input, setInput] = useState("")
  const [streaming, setStreaming] = useState(false)
  const [hiddenCount, setHiddenCount] = useState(0)
  const [selectedModel, setModel] = useState(null)
  const [protectionMode, setProtectionMode] = useState("privacy")
  const [projectName, setProjectName] = useState(null)
  const [uploadFile, setUploadFile] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState("")
  const [sessionExpiresAt, setSessionExpiresAt] = useState(null)
  const [pipelineStep, setPipelineStep] = useState("")
  const [pipelineModel, setPipelineModel] = useState("")
  const [unsafeAttachmentPrompt, setUnsafeAttachmentPrompt] = useState(null)

  const bottomRef = useRef(null)
  const textareaRef = useRef(null)
  const fileRef = useRef(null)
  const abortRef = useRef(null)
  const skipNextReloadRef = useRef(false)

  const headers = { Authorization: `Bearer ${token}` }

  const stepLabels = {
    preparing: t("chat.steps.preparing"),
    policy: t("chat.steps.policy"),
    history: t("chat.steps.history"),
    redacting: t("chat.steps.redacting"),
    provider: pipelineModel
      ? t("chat.steps.provider_with_model", { model: pipelineModel })
      : t("chat.steps.provider"),
    restoring: t("chat.steps.restoring"),
    saving: t("chat.steps.saving"),
    done: t("chat.steps.done"),
  }

  function getFileExt(file) {
    return file?.name?.split(".")?.pop()?.toLowerCase?.() ?? ""
  }

  function isOcrTextExtractionFailure(message, file) {
    const ext = getFileExt(file)
    if (!OCR_IMAGE_EXTS.has(ext)) return false
    const text = String(message ?? "").toLowerCase()
    return (
      text.includes("aucun texte extractible par ocr")
      || text.includes("aucun texte extractible")
      || text.includes("aucun texte")
      || text.includes("ocr")
      || text.includes("image ne contient")
    )
  }

  useEffect(() => {
    const key = `${PROTECTION_STORAGE_PREFIX}${username ?? "anonymous"}`
    const saved = localStorage.getItem(key)
    if (saved && protectionOptions.some((option) => option.value === saved)) {
      setProtectionMode(saved)
      return
    }
    setProtectionMode("privacy")
  }, [username, t])

  useEffect(() => {
    const key = `${PROTECTION_STORAGE_PREFIX}${username ?? "anonymous"}`
    localStorage.setItem(key, protectionMode)
  }, [protectionMode, username])

  useEffect(() => {
    const key = `${MODEL_STORAGE_PREFIX}${username ?? "anonymous"}`
    const saved = localStorage.getItem(key)
    if (saved && VALID_MODEL_IDS.has(saved)) {
      setModel(saved)
      return
    }
    localStorage.removeItem(key)
    setModel(null)
  }, [username])

  useEffect(() => {
    const key = `${MODEL_STORAGE_PREFIX}${username ?? "anonymous"}`
    if (selectedModel && VALID_MODEL_IDS.has(selectedModel)) {
      localStorage.setItem(key, selectedModel)
    } else {
      localStorage.removeItem(key)
    }
  }, [selectedModel, username])

  useEffect(() => {
    setHiddenCount(0)
    setProjectName(null)
    setSessionExpiresAt(null)

    if (!activeProjectId) {
      setMessages([])
      return
    }

    const skip = skipNextReloadRef.current
    skipNextReloadRef.current = false
    if (!skip) setMessages([])

    fetch(`${API_URL}/api/projects/${activeProjectId}`, { headers })
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        if (!data) return
        setProjectName(data.name ?? null)
        if (!skip) {
          const projectMessages = (data.messages ?? []).map((message) => ({
            role: message.role,
            content: message.content_restored ?? message.content_redacted ?? "",
          }))
          setMessages(projectMessages)
        }
      })
  }, [activeProjectId, token])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [messages, streaming])

  useEffect(() => {
    onHiddenCountChange?.(hiddenCount)
  }, [hiddenCount, onHiddenCountChange])

  const waitForUploadJob = useCallback(async (jobId) => {
    while (true) {
      await new Promise((resolve) => window.setTimeout(resolve, 2000))
      const response = await fetch(`${API_URL}/api/upload/status/${jobId}`, { headers })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(payload.detail ?? `HTTP ${response.status}`)
      }
      if (payload.status === "done" && payload.result) {
        return payload.result
      }
      if (payload.status === "error") {
        throw new Error(payload.error ?? t("chat.upload_failed"))
      }
    }
  }, [headers, t])

  const send = useCallback(async (options = {}) => {
    const { allowUnsafeAttachment = false } = options
    const text = input.trim()
    if ((!text && !uploadFile) || streaming) return

    setInput("")
    setError("")
    setPipelineStep("")
    setPipelineModel("")
    setUnsafeAttachmentPrompt(null)

    let uploadResult = null
    let bypassedAttachment = null
    let keepUploadFile = false

    if (uploadFile && !(allowUnsafeAttachment && OCR_IMAGE_EXTS.has(getFileExt(uploadFile)))) {
      setUploading(true)
      try {
        const formData = new FormData()
        formData.append("file", uploadFile)
        if (activeProjectId) formData.append("project_id", activeProjectId)
        formData.append("protection_mode", protectionMode)

        const response = await fetch(`${API_URL}/api/upload`, {
          method: "POST",
          headers,
          body: formData,
        })
        const payload = await response.json()
        if (!response.ok) throw new Error(payload.detail ?? t("chat.upload_failed"))

        uploadResult =
          payload.job_id && payload.status === "processing"
            ? await waitForUploadJob(payload.job_id)
            : payload

        if (uploadResult.project_id && !activeProjectId) {
          skipNextReloadRef.current = true
          onProjectCreated?.(uploadResult.project_id)
        }
        setHiddenCount((count) => count + (uploadResult.redacted_entities ?? 0))

        if (!text) {
          setMessages((previous) => [
            ...previous,
            {
              role: "user",
              content: "",
              attachments: buildAttachmentMeta(uploadResult, uploadFile),
            },
            {
              role: "assistant",
              content: t("chat.file_added", { filename: uploadResult.filename ?? uploadFile.name }),
            },
          ])
        }
      } catch (uploadError) {
        if (isOcrTextExtractionFailure(uploadError.message, uploadFile)) {
          keepUploadFile = true
          setUnsafeAttachmentPrompt({
            name: uploadFile.name,
            ext: getFileExt(uploadFile),
          })
          setError("")
        } else {
          setError(uploadError.message)
        }
      } finally {
        setUploading(false)
        if (!keepUploadFile) {
          setUploadFile(null)
        }
      }

      if (keepUploadFile) return
      if (!text) return
    }

    if (uploadFile && allowUnsafeAttachment && OCR_IMAGE_EXTS.has(getFileExt(uploadFile))) {
      bypassedAttachment = {
        filename: uploadFile.name,
        file_type: getFileExt(uploadFile),
        unsafe_attachment: true,
      }
      if (!text) {
        setMessages((previous) => [
          ...previous,
          {
            role: "user",
            content: "",
            attachments: buildAttachmentMeta(bypassedAttachment, uploadFile),
          },
          {
            role: "assistant",
            content: t("chat.unsafe_attachment_added", { filename: uploadFile.name }),
          },
        ])
        setUploadFile(null)
        return
      }
      setUploadFile(null)
    }

    const attachmentPayload = uploadResult || bypassedAttachment
    const targetProjectId = activeProjectId || attachmentPayload?.project_id || null
    const userMessage = {
      role: "user",
      content: text,
      attachments: attachmentPayload ? buildAttachmentMeta(attachmentPayload, uploadFile) : [],
    }
    const outboundMessages = [...messages, userMessage]
    setMessages((previous) => [...previous, userMessage])
    setStreaming(true)

    const body = {
      messages: outboundMessages.map((message) => ({
        role: message.role,
        content: message.content,
      })),
      max_tokens: 2048,
      project_id: targetProjectId,
      protection_mode: protectionMode,
      ...(attachmentPayload ? { attachment_name: attachmentPayload.filename ?? uploadFile?.name } : {}),
      ...(attachmentPayload?.file_type ? { attachment_types: [attachmentPayload.file_type] } : {}),
      ...(uploadResult ? { system: buildDocumentSystemContext(uploadResult, uploadFile?.name) } : {}),
      ...(selectedModel && VALID_MODEL_IDS.has(selectedModel) ? { preferred_model: selectedModel } : {}),
    }

    let newProjectId = null

    try {
      const controller = new AbortController()
      abortRef.current = controller

      const response = await fetch(`${API_URL}/api/chat/stream`, {
        method: "POST",
        headers: { ...headers, "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      })

      if (!response.ok) {
        const payload = await response.json().catch(() => ({}))
        const detail = payload.detail
        const message = Array.isArray(detail)
          ? detail.map((entry) => entry.msg ?? JSON.stringify(entry)).join(" | ")
          : typeof detail === "string"
            ? detail
            : `HTTP ${response.status}`
        throw new Error(message)
      }

      setMessages((previous) => [...previous, { role: "assistant", content: "" }])

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ""

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const chunks = buffer.split("\n\n")
        buffer = chunks.pop() || ""

        for (const chunk of chunks) {
          if (!chunk.startsWith("data: ")) continue

          try {
            const parsed = JSON.parse(chunk.slice(6))
            if (parsed.type === "start") {
              if (parsed.project_id && !targetProjectId) newProjectId = parsed.project_id
              if (parsed.redacted_entities) {
                setHiddenCount((count) => count + parsed.redacted_entities)
              }
              if (parsed.session_expires_at) setSessionExpiresAt(parsed.session_expires_at)
            }

            if (parsed.type === "status" && typeof parsed.step === "string") {
              setPipelineStep(parsed.step)
              if (parsed.model) setPipelineModel(parsed.model)
            }

            if (parsed.type === "delta" && typeof parsed.content === "string") {
              setMessages((previous) => {
                const updated = [...previous]
                const last = updated[updated.length - 1]
                if (last && last.role === "assistant") {
                  updated[updated.length - 1] = {
                    ...last,
                    content: last.content + parsed.content,
                  }
                }
                return updated
              })
            }

            if (parsed.type === "done") {
              setPipelineStep("done")
              window.setTimeout(() => {
                setPipelineStep("")
                setPipelineModel("")
              }, 1200)
            }

            if (parsed.type === "error") {
              setError(parsed.content || t("chat.unknown_error"))
              setPipelineStep("")
              setPipelineModel("")
              setMessages((previous) => {
                const updated = [...previous]
                const last = updated[updated.length - 1]
                if (last && last.role === "assistant" && !last.content) {
                  updated.pop()
                }
                return updated
              })
            }
          } catch {
            // ignore invalid SSE chunks
          }
        }
      }

      if (newProjectId) {
        skipNextReloadRef.current = true
        onProjectCreated?.(newProjectId)
      }
    } catch (streamError) {
      if (streamError.name !== "AbortError") {
        setError(streamError.message)
        setPipelineStep("")
        setPipelineModel("")
        setMessages((previous) => previous.slice(0, -1))
      }
    } finally {
      setStreaming(false)
      abortRef.current = null
    }
  }, [
    input,
    uploadFile,
    streaming,
    activeProjectId,
    selectedModel,
    protectionMode,
    messages,
    headers,
    onProjectCreated,
    waitForUploadJob,
    t,
  ])

  function handleKey(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      send()
    }
  }

  function handleFileChange(event) {
    const file = event.target.files?.[0]
    if (file) setUploadFile(file)
    event.target.value = ""
  }

  const currentModelLabel = selectedModel
    ? {
      "claude-opus-4-5": t("models.anthropic"),
      "claude-sonnet-4-5": t("models.anthropic"),
      "claude-haiku-4-5": t("models.anthropic"),
      "gpt-4o": t("models.openai"),
      "gpt-4o-mini": t("models.openai"),
      "gemini-1.5-pro": t("models.google"),
      "gemini-1.5-flash": t("models.google"),
      "mistral-large-latest": t("models.mistral"),
      "mistral-small-latest": t("models.mistral"),
    }[selectedModel] ?? selectedModel
    : t("models.auto")

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", background: "var(--c-bg)" }}>
      <div
        style={{
          height: 50,
          minHeight: 50,
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "0 20px",
          borderBottom: "1px solid var(--c-border)",
          background: "var(--c-surface)",
        }}
      >
        <div
          className="font-title"
          style={{
            fontSize: 15,
            fontWeight: 600,
            color: "var(--c-text)",
            flex: 1,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {projectName ?? (activeProjectId ? t("chat.project") : t("chat.new_conversation"))}
        </div>

        <Link
          to="/dashboard"
          style={{ fontSize: 12, color: "var(--c-text-muted)", textDecoration: "none" }}
          onMouseEnter={(event) => { event.currentTarget.style.color = "var(--c-accent-lt)" }}
          onMouseLeave={(event) => { event.currentTarget.style.color = "var(--c-text-muted)" }}
        >
          {t("chat.credits_month")}
        </Link>

        <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
          <span
            className="dot-pulse"
            style={{
              width: 7,
              height: 7,
              borderRadius: "50%",
              background: "#22c55e",
              display: "inline-block",
            }}
          />
          <span style={{ fontSize: 12, color: "var(--c-text-muted)" }}>
            {t("chat.entities_masked", { count: hiddenCount })}
          </span>
        </div>

        <SessionExpireBadge expiresAt={sessionExpiresAt} />
      </div>

      <div style={{ flex: 1, overflowY: "auto", padding: "20px 24px" }} className="scrollbar-none">
        {messages.length === 0
          ? <EmptyState />
          : messages.map((message, index) => (
            <MessageBubble key={index} role={message.role} content={message.content} attachments={message.attachments} />
          ))}

        {streaming && pipelineStep && (
          <div style={{ marginBottom: 14, display: "flex", justifyContent: "center" }}>
            <div style={{
              width: "min(100%, 560px)",
              background: "var(--c-panel)",
              border: "1px solid var(--c-border)",
              borderRadius: 12,
              padding: "12px 14px",
            }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "var(--c-text)", marginBottom: 8 }}>
                {t("chat.processing_title")}
              </div>
              <div style={{ display: "grid", gap: 6 }}>
                {PIPELINE_STEPS.filter((step) => step !== "done" || pipelineStep === "done").map((step) => {
                  const currentIndex = PIPELINE_STEPS.indexOf(pipelineStep)
                  const stepIndex = PIPELINE_STEPS.indexOf(step)
                  const completed = currentIndex > stepIndex
                  const active = currentIndex === stepIndex
                  return (
                    <div key={step} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
                      <span style={{
                        width: 10,
                        height: 10,
                        borderRadius: "50%",
                        background: completed ? "#22c55e" : active ? "#2d8ff0" : "var(--c-border)",
                        boxShadow: active ? "0 0 0 4px rgba(45,143,240,0.12)" : "none",
                        transition: "all .15s ease",
                        flexShrink: 0,
                      }} />
                      <span style={{ color: active ? "var(--c-text)" : completed ? "#c8f5d1" : "var(--c-text-faint)" }}>
                        {stepLabels[step]}
                      </span>
                    </div>
                  )
                })}
              </div>
            </div>
          </div>
        )}

        {streaming
          && messages.length > 0
          && messages[messages.length - 1].role === "assistant"
          && messages[messages.length - 1].content === "" && (
            <div style={{ display: "flex", gap: 10, marginBottom: 16 }}>
              <div
                style={{
                  width: 30,
                  height: 30,
                  borderRadius: "50%",
                  background: "var(--c-panel)",
                  border: "1px solid var(--c-border)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <ShieldSvg size={16} />
              </div>
              <div
                style={{
                  background: "var(--c-card)",
                  border: "1px solid var(--c-border)",
                  borderRadius: "4px 14px 14px 14px",
                  padding: "12px 16px",
                }}
              >
                <Spinner size={16} />
              </div>
            </div>
          )}

        {error && (
          <div style={{ color: "#f87171", fontSize: 13, marginBottom: 12, textAlign: "center" }}>
            {error}
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      <div
        style={{
          borderTop: "1px solid var(--c-border)",
          background: "var(--c-surface)",
          padding: "12px 16px",
        }}
      >
        {unsafeAttachmentPrompt && (
          <div
            style={{
              marginBottom: 10,
              padding: "10px 12px",
              borderRadius: 10,
              border: "1px solid #b45309",
              background: "rgba(180,83,9,0.14)",
              color: "#fcd34d",
              fontSize: 12,
            }}
          >
            <div style={{ fontWeight: 700, marginBottom: 6 }}>
              {t("chat.unsafe_attachment_title")}
            </div>
            <div style={{ color: "#fde68a", lineHeight: 1.5 }}>
              {t("chat.unsafe_attachment_help", { filename: unsafeAttachmentPrompt.name })}
            </div>
            <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
              <button
                type="button"
                onClick={() => send({ allowUnsafeAttachment: true })}
                style={{
                  border: "none",
                  borderRadius: 8,
                  background: "#b45309",
                  color: "#fff",
                  padding: "8px 12px",
                  fontSize: 12,
                  fontWeight: 700,
                  cursor: "pointer",
                }}
              >
                {t("chat.unsafe_attachment_confirm")}
              </button>
              <button
                type="button"
                onClick={() => {
                  setUnsafeAttachmentPrompt(null)
                  setUploadFile(null)
                }}
                style={{
                  border: "1px solid var(--c-border)",
                  borderRadius: 8,
                  background: "var(--c-panel)",
                  color: "var(--c-text)",
                  padding: "8px 12px",
                  fontSize: 12,
                  cursor: "pointer",
                }}
              >
                {t("common.cancel")}
              </button>
            </div>
          </div>
        )}

        {uploadFile && (
          <div
            style={{
              marginBottom: 8,
              padding: "6px 12px",
              background: "var(--c-accent-dim)",
              border: "1px solid var(--c-accent)",
              borderRadius: 6,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              fontSize: 12,
              color: "var(--c-accent-lt)",
            }}
          >
            <span>{uploadFile.name}</span>
            <button
              onClick={() => setUploadFile(null)}
              style={{
                background: "none",
                border: "none",
                color: "var(--c-accent-lt)",
                cursor: "pointer",
                fontSize: 14,
              }}
              title={t("common.close")}
            >
              &#10005;
            </button>
          </div>
        )}

        <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
          <div style={{ display: "flex", gap: 4, flexShrink: 0, paddingBottom: 2 }}>
            <button onClick={() => fileRef.current?.click()} title={t("chat.attach_file")} style={smallBtn}>
              &#128206;
            </button>
            <input
              ref={fileRef}
              type="file"
              accept={ACCEPT}
              style={{ display: "none" }}
              onChange={handleFileChange}
            />
          </div>

          <textarea
            ref={textareaRef}
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={handleKey}
            placeholder={t("chat.placeholder")}
            rows={1}
            style={{
              flex: 1,
              resize: "none",
              overflow: "hidden",
              background: "var(--c-panel)",
              border: "1px solid var(--c-border)",
              borderRadius: 10,
              padding: "10px 14px",
              color: "var(--c-text)",
              fontSize: 14,
              lineHeight: 1.5,
              outline: "none",
              fontFamily: "var(--font-body)",
              minHeight: 42,
              maxHeight: 160,
              transition: "border-color .15s",
            }}
            onInput={(event) => {
              event.target.style.height = "auto"
              event.target.style.height = `${Math.min(event.target.scrollHeight, 160)}px`
            }}
            onFocus={(event) => { event.currentTarget.style.borderColor = "var(--c-accent)" }}
            onBlur={(event) => { event.currentTarget.style.borderColor = "var(--c-border)" }}
          />

          <div style={{ display: "flex", alignItems: "flex-end", gap: 0, flexShrink: 0, paddingBottom: 2 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginRight: 8, marginBottom: 4 }}>
              <div style={{ fontSize: 11, color: "var(--c-text-muted)", whiteSpace: "nowrap" }}>
                {currentModelLabel}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                {protectionOptions.map((option) => {
                  const active = protectionMode === option.value
                  return (
                    <button
                      key={option.value}
                      type="button"
                      title={option.tooltip}
                      onClick={() => setProtectionMode(option.value)}
                      style={{
                        height: 26,
                        padding: "0 9px",
                        borderRadius: 20,
                        border: active ? `1px solid ${option.color}55` : "1px solid var(--c-border)",
                        background: active ? `${option.color}18` : "var(--c-panel)",
                        color: active ? option.color : "var(--c-text-muted)",
                        fontSize: 11,
                        cursor: "pointer",
                        whiteSpace: "nowrap",
                        display: "flex",
                        alignItems: "center",
                        gap: 6,
                        transition: "all .12s",
                        fontFamily: "var(--font-body)",
                      }}
                    >
                      <span
                        style={{
                          width: 8,
                          height: 8,
                          borderRadius: "50%",
                          background: option.color,
                          display: "inline-block",
                        }}
                      />
                      <span>{option.label}</span>
                    </button>
                  )
                })}
              </div>
            </div>

            <ModelSelector value={selectedModel} onChange={setModel} />

            <button
              onClick={send}
              disabled={streaming || uploading || (!input.trim() && !uploadFile)}
              title={t("chat.send")}
              style={{
                height: 38,
                padding: "0 16px",
                borderRadius: "8px 0 0 8px",
                background:
                  (streaming || uploading || (!input.trim() && !uploadFile))
                    ? "var(--c-accent-dim)"
                    : "linear-gradient(135deg,#1a6fcc,#2d8ff0)",
                border: "none",
                color: "#fff",
                cursor: "pointer",
                fontSize: 13,
                display: "flex",
                alignItems: "center",
                gap: 8,
                transition: "all .15s",
                fontWeight: 600,
              }}
            >
              {streaming ? <Spinner size={15} /> : null}
              <span>{t("chat.send")}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

const smallBtn = {
  width: 34,
  height: 34,
  borderRadius: 7,
  background: "var(--c-panel)",
  border: "1px solid var(--c-border)",
  color: "var(--c-text-muted)",
  fontSize: 15,
  cursor: "pointer",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  transition: "all .12s",
}

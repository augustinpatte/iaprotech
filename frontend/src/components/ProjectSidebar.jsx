import React, { useState, useEffect, useCallback, useRef } from "react"
import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router-dom"
import { API_URL } from "../App.jsx"

function groupProjects(projects, t) {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const week = new Date(today)
  week.setDate(today.getDate() - 7)
  const groups = {
    [t("sidebar.today")]: [],
    [t("sidebar.this_week")]: [],
    [t("sidebar.earlier")]: [],
  }

  for (const project of projects) {
    const date = new Date(project.last_activity ?? project.created_at)
    if (date >= today) groups[t("sidebar.today")].push(project)
    else if (date >= week) groups[t("sidebar.this_week")].push(project)
    else groups[t("sidebar.earlier")].push(project)
  }

  return groups
}

const iconBtn = {
  background: "none",
  border: "none",
  cursor: "pointer",
  color: "var(--c-text-muted)",
  fontSize: 12,
  padding: "2px 4px",
  borderRadius: 4,
  lineHeight: 1,
  transition: "color .1s",
}

function ProjectItem({ project, active, onSelect, onRename, onDelete, t }) {
  const [hover, setHover] = useState(false)
  const [editing, setEditing] = useState(false)
  const untitled = t("sidebar.untitled")
  const [name, setName] = useState(project.name ?? untitled)
  const inputRef = useRef(null)

  useEffect(() => {
    if (editing) inputRef.current?.focus()
  }, [editing])

  function commitRename() {
    setEditing(false)
    if (name.trim() && name !== (project.name ?? untitled)) onRename(project.project_id, name.trim())
    else setName(project.name ?? untitled)
  }

  return (
    <div
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onClick={() => !editing && onSelect(project.project_id)}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        padding: "7px 10px",
        borderRadius: 8,
        cursor: "pointer",
        background: active ? "var(--c-accent-dim)" : hover ? "var(--c-panel)" : "transparent",
        border: active ? "1px solid var(--c-accent)" : "1px solid transparent",
        transition: "all .12s",
        marginBottom: 2,
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: active ? "var(--c-accent-lt)" : "var(--c-text-faint)",
          flexShrink: 0,
          display: "inline-block",
        }}
      />

      {editing ? (
        <input
          ref={inputRef}
          value={name}
          onChange={(event) => setName(event.target.value)}
          onBlur={commitRename}
          onKeyDown={(event) => {
            if (event.key === "Enter") commitRename()
            if (event.key === "Escape") {
              setEditing(false)
              setName(project.name ?? untitled)
            }
          }}
          onClick={(event) => event.stopPropagation()}
          style={{
            flex: 1,
            background: "transparent",
            border: "none",
            outline: "none",
            color: "var(--c-text)",
            fontSize: 13,
          }}
        />
      ) : (
        <span
          style={{
            flex: 1,
            fontSize: 13,
            color: active ? "var(--c-text)" : "var(--c-text-muted)",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            fontWeight: active ? 500 : 400,
          }}
        >
          {project.name ?? untitled}
        </span>
      )}

      {(hover || active) && !editing && (
        <div style={{ display: "flex", gap: 2, flexShrink: 0 }}>
          <button
            onClick={(event) => { event.stopPropagation(); setEditing(true) }}
            style={iconBtn}
            title={t("sidebar.rename")}
          >
            &#9998;
          </button>
          <button
            onClick={(event) => { event.stopPropagation(); onDelete(project.project_id) }}
            style={{ ...iconBtn, color: "#f87171" }}
            title={t("sidebar.delete")}
          >
            &#10005;
          </button>
        </div>
      )}
    </div>
  )
}

function exportButtonStyle(activeProjectId) {
  return {
    width: "100%",
    padding: "7px 0",
    marginBottom: 6,
    background: activeProjectId ? "var(--c-accent-dim)" : "transparent",
    border: `1px solid ${activeProjectId ? "var(--c-accent)" : "var(--c-border)"}`,
    borderRadius: 7,
    color: activeProjectId ? "var(--c-accent-lt)" : "var(--c-text-faint)",
    fontSize: 12,
    cursor: activeProjectId ? "pointer" : "default",
    transition: "all .12s",
  }
}

export default function ProjectSidebar({
  token,
  activeProjectId,
  onSelectProject,
  onNewProject,
  refresh,
}) {
  const { t } = useTranslation()
  const [projects, setProjects] = useState([])
  const [loading, setLoading] = useState(false)
  const [importing, setImporting] = useState(false)
  const [downloading, setDownloading] = useState("")
  const [error, setError] = useState("")
  const fileRef = useRef(null)
  const navigate = useNavigate()

  const headers = { Authorization: `Bearer ${token}` }

  const load = useCallback(async () => {
    setLoading(true)
    setError("")
    try {
      const response = await fetch(`${API_URL}/api/projects`, { headers })
      if (!response.ok) throw new Error(t("sidebar.load_failed"))
      const data = await response.json()
      setProjects(data.projects || [])
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
    }
  }, [token, t])

  useEffect(() => {
    load()
  }, [load, refresh])

  async function handleRename(id, newName) {
    setError("")
    try {
      const response = await fetch(`${API_URL}/api/projects/${id}`, {
        method: "PATCH",
        headers: { ...headers, "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName }),
      })
      if (!response.ok) throw new Error(t("sidebar.rename_failed"))
      await load()
    } catch (renameError) {
      setError(renameError.message)
    }
  }

  async function handleDelete(id) {
    if (!window.confirm(t("sidebar.delete_confirm"))) return
    setError("")
    try {
      const response = await fetch(`${API_URL}/api/projects/${id}`, {
        method: "DELETE",
        headers,
      })
      if (!response.ok) throw new Error(t("sidebar.delete_failed"))
      if (activeProjectId === id) onSelectProject(null)
      await load()
    } catch (deleteError) {
      setError(deleteError.message)
    }
  }

  async function handleDownload(kind = "pdf") {
    if (!activeProjectId) return
    const endpoint = kind === "pdf"
      ? `${API_URL}/api/projects/${activeProjectId}/export`
      : `${API_URL}/api/projects/${activeProjectId}/export/${kind}`
    const fallback = kind === "pdf" ? "project.pdf" : `project.${kind}`

    setDownloading(kind)
    setError("")
    try {
      const response = await fetch(endpoint, { headers })
      if (!response.ok) throw new Error(t("sidebar.export_failed"))

      const blob = await response.blob()
      const disposition = response.headers.get("content-disposition") ?? ""
      const filename = disposition.match(/filename="([^"]+)"/)?.[1] ?? fallback
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement("a")
      anchor.href = url
      anchor.download = filename
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (downloadError) {
      setError(downloadError.message)
    } finally {
      setDownloading("")
    }
  }

  function handleNewProjectClick() {
    onNewProject?.()
    onSelectProject(null)
    navigate("/")
  }

  async function handleImport(event) {
    const file = event.target.files?.[0]
    if (!file) return

    setImporting(true)
    setError("")
    try {
      const formData = new FormData()
      formData.append("file", file)
      const response = await fetch(`${API_URL}/api/projects/import`, {
        method: "POST",
        headers,
        body: formData,
      })
      if (!response.ok) throw new Error(t("sidebar.import_failed"))
      const data = await response.json()
      onSelectProject(data.project_id)
      await load()
    } catch (importError) {
      setError(importError.message)
    } finally {
      setImporting(false)
      event.target.value = ""
    }
  }

  const groups = groupProjects(projects, t)

  return (
    <aside
      style={{
        width: 255,
        minWidth: 255,
        background: "var(--c-surface)",
        borderRight: "1px solid var(--c-border)",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
      }}
    >
      <div style={{ padding: "12px 12px 8px" }}>
        <button
          onClick={handleNewProjectClick}
          style={{
            width: "100%",
            padding: "9px 0",
            background: "linear-gradient(135deg,#1a6fcc,#2d8ff0)",
            border: "none",
            borderRadius: 8,
            color: "#fff",
            fontFamily: "var(--font-title)",
            fontWeight: 700,
            fontSize: 13,
            cursor: "pointer",
            marginBottom: 6,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 6,
          }}
        >
          <span style={{ fontSize: 16, lineHeight: 1 }}>+</span>
          {t("chat.new_project")}
        </button>

        <button
          onClick={() => fileRef.current?.click()}
          disabled={importing}
          style={{
            width: "100%",
            padding: "8px 0",
            background: "transparent",
            border: "1px solid var(--c-border)",
            borderRadius: 8,
            color: "var(--c-text-muted)",
            fontSize: 13,
            cursor: "pointer",
            transition: "all .12s",
          }}
          onMouseEnter={(event) => {
            event.currentTarget.style.borderColor = "var(--c-accent)"
            event.currentTarget.style.color = "var(--c-accent-lt)"
          }}
          onMouseLeave={(event) => {
            event.currentTarget.style.borderColor = "var(--c-border)"
            event.currentTarget.style.color = "var(--c-text-muted)"
          }}
        >
          {importing ? t("chat.importing") : t("chat.import_project")}
        </button>
        <input ref={fileRef} type="file" accept=".pdf,.docx,.txt,.json" style={{ display: "none" }} onChange={handleImport} />
      </div>

      <div style={{ flex: 1, overflowY: "auto", padding: "0 8px" }} className="scrollbar-none">
        {loading && (
          <div style={{ textAlign: "center", color: "var(--c-text-faint)", fontSize: 12, padding: 16 }}>
            {t("sidebar.loading")}
          </div>
        )}

        {error && (
          <div
            style={{
              margin: "8px 6px 10px",
              padding: "10px 12px",
              borderRadius: 8,
              background: "#3b0f12",
              border: "1px solid #7f1d1d",
              color: "#fca5a5",
              fontSize: 12,
            }}
          >
            {error}
          </div>
        )}

        {Object.entries(groups).map(([label, list]) => (
          list.length === 0 ? null : (
            <div key={label}>
              <div
                style={{
                  fontSize: 10,
                  fontWeight: 600,
                  color: "var(--c-text-faint)",
                  letterSpacing: ".08em",
                  textTransform: "uppercase",
                  padding: "10px 10px 4px",
                }}
              >
                {label}
              </div>
              {list.map((project) => (
                <ProjectItem
                  key={project.project_id}
                  project={project}
                  active={project.project_id === activeProjectId}
                  onSelect={onSelectProject}
                  onRename={handleRename}
                  onDelete={handleDelete}
                  t={t}
                />
              ))}
            </div>
          )
        ))}

        {!loading && projects.length === 0 && (
          <div style={{ textAlign: "center", color: "var(--c-text-faint)", fontSize: 12, padding: "24px 8px" }}>
            {t("sidebar.no_projects")}
          </div>
        )}
      </div>

      <div style={{ borderTop: "1px solid var(--c-border)", padding: "10px 12px 12px" }}>
        <div
          style={{
            fontSize: 10,
            fontWeight: 600,
            color: "var(--c-text-faint)",
            letterSpacing: ".08em",
            textTransform: "uppercase",
            marginBottom: 8,
          }}
        >
          {t("sidebar.export_section")}
        </div>

        <button onClick={() => handleDownload("pdf")} disabled={!activeProjectId || !!downloading} style={exportButtonStyle(activeProjectId)}>
          {downloading === "pdf" ? t("export.exporting") : t("export.download_pdf")}
        </button>
        <button onClick={() => handleDownload("docx")} disabled={!activeProjectId || !!downloading} style={exportButtonStyle(activeProjectId)}>
          {downloading === "docx" ? t("export.exporting") : t("export.download_docx")}
        </button>
        <button onClick={() => handleDownload("txt")} disabled={!activeProjectId || !!downloading} style={exportButtonStyle(activeProjectId)}>
          {downloading === "txt" ? t("export.exporting") : t("export.download_txt")}
        </button>
        <button onClick={() => handleDownload("json")} disabled={!activeProjectId || !!downloading} style={exportButtonStyle(activeProjectId)}>
          {downloading === "json" ? t("export.exporting") : t("export.export_json")}
        </button>

        <button
          onClick={() => fileRef.current?.click()}
          style={{
            width: "100%",
            padding: "7px 0",
            background: "transparent",
            border: "1px solid var(--c-border)",
            borderRadius: 7,
            color: "var(--c-text-muted)",
            fontSize: 12,
            cursor: "pointer",
          }}
        >
          {t("export.resume")}
        </button>
      </div>
    </aside>
  )
}

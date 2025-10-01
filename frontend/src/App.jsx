import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'

const API_BASE = ''

const STATUS_FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'running', label: 'Running' },
  { value: 'success', label: 'Successful' },
  { value: 'failed', label: 'Failed' },
  { value: 'pending', label: 'Pending' },
]

const RANGE_LABELS = {
  '24h': '24 hours',
  '7d': '7 days',
  '30d': '30 days',
  all: 'all time',
}


let readStorageWarningLogged = false
let writeStorageWarningLogged = false

function readPageSizeFromStorage() {
  if (typeof window === 'undefined' || typeof window.localStorage === 'undefined') return 20
  try {
    const saved = window.localStorage.getItem('pageSize')
    const parsed = saved ? parseInt(saved, 10) : 20
    return Number.isFinite(parsed) ? parsed : 20
  } catch (error) {
    if (!readStorageWarningLogged) {
      console.warn('Unable to read page size from localStorage; falling back to default.', error)
      readStorageWarningLogged = true
    }
    return 20
  }
}

function writePageSizeToStorage(value) {
  if (typeof window === 'undefined' || typeof window.localStorage === 'undefined') return
  try {
    window.localStorage.setItem('pageSize', String(value))
  } catch (error) {
    if (!writeStorageWarningLogged) {
      console.warn('Unable to persist page size to localStorage.', error)
      writeStorageWarningLogged = true
    }
  }
}


function formatLocal(iso) {
  if (!iso) return '—'
  const parsed = new Date(iso)
  return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString()
}

function parseScope(scope) {
  if (!scope) return []
  if (Array.isArray(scope)) return scope.filter(Boolean)
  let value = String(scope).trim()
  if (value.startsWith('servers:')) value = value.slice('servers:'.length)
  if (value.startsWith('group:')) value = value.slice('group:'.length)
  return value
    .split(',')
    .map(part => part.trim())
    .filter(Boolean)
}

function canonicalStatus(status) {
  const s = (status || '').toString().toLowerCase()
  if (s.includes('running') || s.includes('progress') || s.includes('in_progress')) return 'running'
  if (s.includes('success') || s.includes('complete') || s.includes('ok')) return 'success'
  if (s.includes('fail') || s.includes('error')) return 'failed'
  if (s.includes('pending') || s.includes('waiting') || s.includes('queued')) return 'pending'
  return 'pending'
}

function statusClass(status) {
  return `status ${canonicalStatus(status)}`
}

function normalizeProgress(value) {
  if (value == null || value === '') return 0
  let cleaned = value
  if (typeof cleaned === 'string') {
    cleaned = cleaned.trim()
    if (cleaned.endsWith('%')) {
      cleaned = cleaned.slice(0, -1)
    }
  }
  const numeric = Number(cleaned)
  if (!Number.isFinite(numeric)) return 0
  const scaled = numeric >= 0 && numeric <= 1 ? numeric * 100 : numeric
  return Math.min(100, Math.max(0, Math.round(scaled)))
}

function formatStatusLabel(status) {
  const key = canonicalStatus(status)
  switch (key) {
    case 'running':
      return 'Running'
    case 'success':
      return 'Success'
    case 'failed':
      return 'Failed'
    case 'pending':
      return 'Pending'
    default: {
      if (!status) return 'Pending'
      const str = String(status)
      return str.charAt(0).toUpperCase() + str.slice(1)
    }
  }
}

function computeProgressPercent(job) {
  if (!job) return 0
  const statusKey = canonicalStatus(job.status)
  let value = normalizeProgress(job.progress)
  if (statusKey === 'running') {
    value = Math.min(value, 99)
  } else if (statusKey === 'success' && value < 100) {
    value = 100
  }
  return value
}

function formatDuration(startIso, endIso) {
  if (!startIso) return '—'
  const start = new Date(startIso)
  if (Number.isNaN(start.getTime())) return '—'
  const end = endIso ? new Date(endIso) : new Date()
  const diffSeconds = Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000))
  const hours = Math.floor(diffSeconds / 3600)
  const minutes = Math.floor((diffSeconds % 3600) / 60)
  const seconds = diffSeconds % 60
  const parts = []
  if (hours) parts.push(`${hours}h`)
  if (minutes) parts.push(`${minutes}m`)
  parts.push(`${seconds}s`)
  return parts.join(' ')
}

export default function App() {
  const [jobs, setJobs] = useState({})
  const [range, setRange] = useState('24h')
  const [searchTerm, setSearchTerm] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [pageSize, setPageSize] = useState(() => readPageSizeFromStorage())
  const [page, setPage] = useState(0)
  const [selectedJob, setSelectedJob] = useState(null)
  const [logs, setLogs] = useState([])
  const [autoScroll, setAutoScroll] = useState(true)
  const [isLoading, setIsLoading] = useState(false)
  const [isLogsLoading, setIsLogsLoading] = useState(false)
  const [error, setError] = useState(null)
  const [logsError, setLogsError] = useState(null)
  const [scopeFilter, setScopeFilter] = useState('')

  const wsRef = useRef(null)
  const reconnectTimerRef = useRef(null)
  const logsContainerRef = useRef(null)
  const selectedJobRef = useRef(null)

  const connectWebSocket = useCallback(() => {
    if (wsRef.current) {
      try {
        wsRef.current.close()
      } catch {
        /* noop */
      }
    }
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${protocol}://${window.location.host}/ws`)
    ws.onopen = () => ws.send('hello')
    ws.onmessage = event => {
      try {
        const msg = JSON.parse(event.data)
        if (msg.type === 'job_start' || msg.type === 'job_progress' || msg.type === 'job_complete') {
          const job = msg.job
          if (job && job.id != null) {
            setJobs(prev => {
              const next = { ...prev }
              next[job.id] = job
              return next
            })
          }
        } else if (msg.type === 'job_log') {
          const logEntry = msg.log
          if (logEntry && selectedJobRef.current && logEntry.job_id === selectedJobRef.current) {
            setLogs(prev => [...prev, logEntry])
          }
        }
      } catch (error) {
        console.error('Failed to parse websocket message', error)
      }
    }
    ws.onerror = () => {
      ws.close()
    }
    ws.onclose = () => {
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current)
      reconnectTimerRef.current = setTimeout(() => {
        if (wsRef.current === ws || wsRef.current === null) {
          connectWebSocket()
        }
      }, 1500)
    }
    wsRef.current = ws
  }, [])

  useEffect(() => { loadJobs(range) }, [range])

  useEffect(() => {
    connectWebSocket()
    return () => {
      if (wsRef.current) wsRef.current.close()
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current)
    }
  }, [connectWebSocket])

  useEffect(() => {
    selectedJobRef.current = selectedJob
  }, [selectedJob])

  useEffect(() => {
    setScopeFilter('')
  }, [selectedJob])

  useEffect(() => {
    if (!autoScroll) return
    const container = logsContainerRef.current
    if (container) {
      container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' })
    }
  }, [logs, autoScroll])

  useEffect(() => {
    writePageSizeToStorage(pageSize)
    setPage(0)
  }, [pageSize])

  useEffect(() => {
    setPage(0)
  }, [searchTerm, statusFilter])

  async function loadJobs(r) {
    setIsLoading(true)
    try {
      const res = await fetch(`${API_BASE}/api/jobs?range=${encodeURIComponent(r)}`)
      if (!res.ok) {
        throw new Error(`Request failed with status ${res.status}`)
      }
      const data = await res.json()
      const map = {}
      data.jobs.forEach(job => {
        if (job && job.id != null) {
          map[job.id] = job
        }
      })
      setJobs(map)
      setError(null)
      setPage(0)
      if (selectedJobRef.current && !map[selectedJobRef.current]) {
        setSelectedJob(null)
        setLogs([])
      }
    } catch (err) {
      console.error('Failed to load jobs', err)
      const message = err instanceof Error ? err.message : 'Unknown error'
      setError(`Unable to load jobs: ${message}`)
    } finally {
      setIsLoading(false)
    }
  }

  async function openJob(jobId) {
    setSelectedJob(jobId)
    selectedJobRef.current = jobId
    setAutoScroll(true)
    setLogs([])
    setLogsError(null)
    if (jobId == null) return
    setIsLogsLoading(true)
    try {
      const res = await fetch(`${API_BASE}/api/jobs/${jobId}/logs?limit=0`)
      if (!res.ok) {
        throw new Error(`Request failed with status ${res.status}`)
      }
      const data = await res.json()
      setLogs(data.logs || [])
      setLogsError(null)
    } catch (err) {
      console.error('Failed to load logs', err)
      const message = err instanceof Error ? err.message : 'Unknown error'
      setLogsError(`Unable to load logs: ${message}`)
    } finally {
      setIsLogsLoading(false)
    }
  }

  const filteredJobs = useMemo(() => {
    const term = searchTerm.trim().toLowerCase()
    return Object.values(jobs).filter(job => {
      const statusKey = canonicalStatus(job.status)
      if (statusFilter !== 'all' && statusKey !== statusFilter) return false
      if (!term) return true
      const haystack = [
        job.job_name,
        job.triggered_by,
        job.scope,
        job.id,
      ]
        .map(item => (item == null ? '' : String(item).toLowerCase()))
        .join(' ')
      return haystack.includes(term)
    })
  }, [jobs, searchTerm, statusFilter])

  const sortedJobs = useMemo(() => {
    return [...filteredJobs].sort((a, b) => {
      const aTime = new Date(a.start_time || 0).getTime()
      const bTime = new Date(b.start_time || 0).getTime()
      return bTime - aTime
    })
  }, [filteredJobs])

  const metrics = useMemo(() => {
    const summary = { total: 0, running: 0, success: 0, failed: 0, pending: 0, successRate: 0 }
    const values = Object.values(jobs)
    if (!values.length) return summary
    values.forEach(job => {
      const key = canonicalStatus(job.status)
      switch (key) {
        case 'running':
          summary.running += 1
          break
        case 'success':
          summary.success += 1
          break
        case 'failed':
          summary.failed += 1
          break
        case 'pending':
        default:
          summary.pending += 1
          break
      }
    })
    summary.total = values.length
    summary.successRate = summary.total ? Math.round((summary.success / summary.total) * 100) : 0
    return summary
  }, [jobs])

  const effectivePageSize = pageSize > 0 ? pageSize : sortedJobs.length || 1
  const pageCount = Math.max(1, Math.ceil(sortedJobs.length / effectivePageSize))
  const startIdx = pageSize > 0 ? page * pageSize : 0
  const endIdx = pageSize > 0 ? Math.min(sortedJobs.length, startIdx + pageSize) : sortedJobs.length
  const pagedJobs = pageSize > 0 ? sortedJobs.slice(startIdx, endIdx) : sortedJobs

  const jobDetails = useMemo(() => {
    if (selectedJob == null) return null
    return Object.values(jobs).find(job => job?.id === selectedJob) ?? null
  }, [jobs, selectedJob])
  const jobScopeItems = useMemo(() => {
    return jobDetails ? parseScope(jobDetails.scope) : []
  }, [jobDetails])
  const jobProgress = computeProgressPercent(jobDetails)
  const detailStatusKey = jobDetails ? canonicalStatus(jobDetails.status) : null
  const detailStatusLabel = formatStatusLabel(jobDetails?.status)
  const scopeFilterTerm = scopeFilter.trim()
  const filteredScopeItems = useMemo(() => {
    if (!scopeFilterTerm) return jobScopeItems
    const query = scopeFilterTerm.toLowerCase()
    return jobScopeItems.filter(item => item.toLowerCase().includes(query))
  }, [jobScopeItems, scopeFilterTerm])
  const rangeLabel = useMemo(() => {
    switch (range) {
      case '24h':
        return RANGE_LABELS['24h']
      case '7d':
        return RANGE_LABELS['7d']
      case '30d':
        return RANGE_LABELS['30d']
      case 'all':
        return RANGE_LABELS.all
      default:
        return 'selected range'
    }
  }, [range])

  const handleStatusFilterSelect = useCallback((nextFilter) => {
    setStatusFilter(nextFilter)
  }, [])

  const metricCardProps = useCallback((nextFilter) => ({
    role: 'button',
    tabIndex: 0,
    onClick: () => handleStatusFilterSelect(nextFilter),
    onKeyDown: event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault()
        handleStatusFilterSelect(nextFilter)
      }
    },
  }), [handleStatusFilterSelect])

  const canCopyScope = typeof navigator !== 'undefined' && typeof navigator.clipboard !== 'undefined'

  const copyScopeList = useCallback(() => {
    if (!canCopyScope) return
    const targets = (scopeFilterTerm ? filteredScopeItems : jobScopeItems)
    if (!targets.length) return
    navigator.clipboard.writeText(targets.join('\n')).catch(err => {
      console.warn('Unable to copy scope targets.', err)
    })
  }, [canCopyScope, filteredScopeItems, jobScopeItems, scopeFilterTerm])

  function downloadLogs() {
    if (selectedJob == null || !logs.length) return
    const text = logs
      .map(entry => {
        const ts = formatLocal(entry.ts || entry.timestamp)
        const level = (entry.level || 'info').toUpperCase()
        return `[${ts}] [${level}] ${entry.message || ''}`
      })
      .join('\n')
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `job-${selectedJob}-logs.txt`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  async function copyLogs() {
    if (!logs.length || !navigator.clipboard) return
    try {
      const text = logs
        .map(entry => {
          const ts = formatLocal(entry.ts || entry.timestamp)
          const level = (entry.level || 'info').toUpperCase()
          return `[${ts}] [${level}] ${entry.message || ''}`
        })
        .join('\n')
      await navigator.clipboard.writeText(text)
    } catch (err) {
      console.warn('Clipboard copy failed', err)
    }
  }

  return (
    <div className="page">
      <header className="hero card">
        <div>
          <h1>Ansible Job Dashboard</h1>
          <p className="subtitle">Live playbook telemetry with streaming progress and logs.</p>
        </div>
        <div className="hero-actions">
          <button className="btn outline" onClick={() => loadJobs(range)} disabled={isLoading}>
            {isLoading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </header>

      <section className="metrics-grid">
        <article
          className={`metric-card primary clickable ${statusFilter === 'all' ? 'active' : ''}`}
          {...metricCardProps('all')}
          title="Click to show all jobs"
        >
          <span className="metric-label">Total jobs</span>
          <span className="metric-value">{metrics.total}</span>
          <div className="metric-progress">
            <div className="progress-track mini">
              <span style={{ width: `${metrics.successRate}%` }} />
            </div>
            <span className="metric-caption muted">{metrics.successRate}% success rate</span>
          </div>
        </article>
        <article
          className={`metric-card running clickable ${statusFilter === 'running' ? 'active' : ''}`}
          {...metricCardProps('running')}
          title="Click to show running jobs"
        >
          <span className="metric-label">Running</span>
          <span className="metric-value">{metrics.running}</span>
          <span className="metric-caption muted">Currently executing</span>
        </article>
        <article
          className={`metric-card pending clickable ${statusFilter === 'pending' ? 'active' : ''}`}
          {...metricCardProps('pending')}
          title="Click to show pending jobs"
        >
          <span className="metric-label">Pending</span>
          <span className="metric-value">{metrics.pending}</span>
          <span className="metric-caption muted">Waiting to start</span>
        </article>
        <article
          className={`metric-card success clickable ${statusFilter === 'success' ? 'active' : ''}`}
          {...metricCardProps('success')}
          title="Click to show successful jobs"
        >
          <span className="metric-label">Successful</span>
          <span className="metric-value">{metrics.success}</span>
          <span className="metric-caption muted">Last {rangeLabel}</span>
        </article>
        <article
          className={`metric-card failed clickable ${statusFilter === 'failed' ? 'active' : ''}`}
          {...metricCardProps('failed')}
          title="Click to show failed jobs"
        >
          <span className="metric-label">Failed</span>
          <span className="metric-value">{metrics.failed}</span>
          <span className="metric-caption muted">Includes unreachable hosts</span>
        </article>
      </section>

      <div className="controls-bar card">
        <div className="search">
          <input
            type="search"
            placeholder="Search by job name, scope, or user…"
            value={searchTerm}
            onChange={e => setSearchTerm(e.target.value)}
          />
        </div>
        <div className="filters">
          {STATUS_FILTERS.map(filter => (
            <button
              key={filter.value}
              className={`chip ${statusFilter === filter.value ? 'active' : ''}`}
              onClick={() => setStatusFilter(filter.value)}
            >
              {filter.label}
              {filter.value !== 'all' && metrics[filter.value] != null && (
                <span className="chip-count">{metrics[filter.value]}</span>
              )}
            </button>
          ))}
        </div>
        <div className="controls-right">
          <div className="select-group">
            <span className="muted">Range</span>
            <select value={range} onChange={e => setRange(e.target.value)}>
              <option value="24h">Last 24 hours</option>
              <option value="7d">Last 7 days</option>
              <option value="30d">Last 30 days</option>
              <option value="all">All</option>
            </select>
          </div>
          <div className="select-group">
            <span className="muted">Page size</span>
            <select value={String(pageSize)} onChange={e => setPageSize(parseInt(e.target.value, 10))}>
              <option value="10">10</option>
              <option value="20">20</option>
              <option value="50">50</option>
              <option value="100">100</option>
              <option value="0">All</option>
            </select>
          </div>
        </div>
      </div>

      {error && <div className="alert danger">{error}</div>}

      <div className="layout">
        <div className="card table-card">
          <div className="card-header">
            <div>
              <h2>Recent jobs</h2>
              <p className="muted small">Showing {sortedJobs.length ? startIdx + 1 : 0} – {endIdx} of {sortedJobs.length}</p>
            </div>
            {isLoading && <span className="badge muted">Loading…</span>}
          </div>
          <div className="table-wrapper">
            <table className="table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Job</th>
                  <th>Scope</th>
                  <th>By</th>
                  <th>Status</th>
                  <th>Progress</th>
                  <th>Started</th>
                  <th>Ended</th>
                </tr>
              </thead>
              <tbody>
                {pagedJobs.length === 0 ? (
                  <tr>
                    <td colSpan="8">
                      <div className="empty-state">
                        {isLoading ? 'Loading jobs…' : 'No jobs match your filters yet.'}
                      </div>
                    </td>
                  </tr>
                ) : (
                  pagedJobs.map(job => {
                    const scopeItems = parseScope(job.scope)
                    const scopeLabel = scopeItems[0] || ''
                    const scopeTooltip = scopeItems.join('\n')
                    const statusKey = canonicalStatus(job.status)
                    const statusLabel = formatStatusLabel(job.status)
                    const progressValue = computeProgressPercent(job)
                    const progressPercent = Number.isFinite(progressValue) ? progressValue : 0
                    const progressClass = `progress-track ${statusKey ? `is-${statusKey}` : ''}`
                    return (
                      <tr
                        key={job.id}
                        className={`clickable ${selectedJob === job.id ? 'selected' : ''}`}
                        onClick={() => openJob(job.id)}
                      >
                        <td>{job.id}</td>
                        <td>{job.job_name}</td>
                        <td title={scopeTooltip}>{scopeLabel}{scopeItems.length > 1 ? ' …' : ''}</td>
                        <td>{job.triggered_by || '—'}</td>
                        <td>
                          <span className={statusClass(job.status)}>{statusLabel}</span>
                        </td>
                        <td>
                          <div className="progress-cell">
                            <div className={progressClass}>
                              <span style={{ width: `${progressPercent}%` }} />
                            </div>
                            <span className="progress-value">{progressPercent}%</span>
                          </div>
                        </td>
                        <td>{formatLocal(job.start_time)}</td>
                        <td>{formatLocal(job.end_time)}</td>
                      </tr>
                    )
                  })
                )}
              </tbody>
            </table>
          </div>
          <div className="pagination">
            <button className="btn outline" onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0}>
              Newer
            </button>
            <span className="muted small">Page {page + 1} / {pageCount}</span>
            <button
              className="btn outline"
              onClick={() => setPage(p => Math.min(pageCount - 1, p + 1))}
              disabled={page >= pageCount - 1}
            >
              Older
            </button>
          </div>
        </div>

        <div className="card detail-card">
          {jobDetails ? (
            <>
              <div className="detail-header">
                <div className="detail-title">
                  <h2>Job #{jobDetails.id}</h2>
                  <p className="muted small">
                    {jobDetails.job_name} • Started {formatLocal(jobDetails.start_time)} by {jobDetails.triggered_by || 'unknown'}
                  </p>
                </div>
                <div className="detail-header-meta">
                  <div className="detail-summary">
                    <div className="detail-summary-item">
                      <span className="muted small">Duration</span>
                      <span>{formatDuration(jobDetails.start_time, jobDetails.end_time)}</span>
                    </div>
                    <div className="detail-summary-item">
                      <span className="muted small">Progress</span>
                      <div className="summary-progress">
                        <div className={`progress-track micro ${detailStatusKey ? `is-${detailStatusKey}` : ''}`}>
                          <span style={{ width: `${jobProgress}%` }} />
                        </div>
                        <span className="summary-progress-value">{jobProgress}%</span>
                      </div>
                    </div>
                  </div>
                  <span className={statusClass(jobDetails.status)}>{detailStatusLabel}</span>
                </div>
              </div>

              <div className="detail-scope">
                <div className="detail-scope-header">
                  <div>
                    <h3>Scope</h3>
                    <p className="muted small">
                      Showing {filteredScopeItems.length} of {jobScopeItems.length} target{jobScopeItems.length === 1 ? '' : 's'}
                    </p>
                  </div>
                  <div className="detail-scope-controls">
                    <input
                      type="search"
                      className="scope-filter"
                      placeholder={jobScopeItems.length > 20 ? 'Filter targets…' : 'Filter scope…'}
                      value={scopeFilter}
                      onChange={e => setScopeFilter(e.target.value)}
                    />
                    <button
                      className="btn outline"
                      onClick={copyScopeList}
                      disabled={!jobScopeItems.length || !canCopyScope}
                    >
                      Copy list
                    </button>
                  </div>
                </div>
                <div className="scope-list">
                  {filteredScopeItems.length === 0 ? (
                    <div className="scope-empty muted">
                      {scopeFilterTerm ? `No targets match “${scopeFilterTerm}”.` : 'No targets defined.'}
                    </div>
                  ) : (
                    filteredScopeItems.map((item, index) => {
                      const lowerItem = item.toLowerCase()
                      let highlighted = item
                      if (scopeFilterTerm) {
                        const query = scopeFilterTerm.toLowerCase()
                        const matchIndex = lowerItem.indexOf(query)
                        if (matchIndex !== -1) {
                          const before = item.slice(0, matchIndex)
                          const match = item.slice(matchIndex, matchIndex + scopeFilterTerm.length)
                          const after = item.slice(matchIndex + scopeFilterTerm.length)
                          highlighted = (
                            <>
                              {before}
                              <mark>{match}</mark>
                              {after}
                            </>
                          )
                        }
                      }
                      return (
                        <div className="scope-item" key={`${item}-${index}`}>
                          <span className="scope-index">{index + 1}</span>
                          <span className="scope-name">{highlighted}</span>
                        </div>
                      )
                    })
                  )}
                </div>
              </div>

              <div className="logs-toolbar">
                <div className="toolbar-buttons">
                  <button className="btn outline" onClick={() => openJob(jobDetails.id)} disabled={isLogsLoading}>
                    {isLogsLoading ? 'Refreshing…' : 'Reload logs'}
                  </button>
                  <button className="btn outline" onClick={downloadLogs} disabled={!logs.length}>
                    Download
                  </button>
                  <button className="btn outline" onClick={copyLogs} disabled={!logs.length || !navigator.clipboard}>
                    Copy
                  </button>
                </div>
                <button
                  className={`btn toggle ${autoScroll ? 'active' : ''}`}
                  onClick={() => setAutoScroll(v => !v)}
                >
                  Auto-scroll {autoScroll ? 'on' : 'off'}
                </button>
              </div>

              {logsError && <div className="alert subtle">{logsError}</div>}

              <div className="logs-container" ref={logsContainerRef}>
                {logs.length === 0 && !logsError && (
                  <div className="empty-state">No logs yet. They will appear here as the play runs.</div>
                )}
                {logs.map((entry, index) => {
                  const levelKey = (entry.level || 'info').toLowerCase()
                  return (
                    <pre key={index} className={`log-line level-${levelKey}`}>
                      <span className="log-timestamp">{formatLocal(entry.ts || entry.timestamp)}</span>
                      <span className="log-level">{(entry.level || 'info').toUpperCase()}</span>
                      <span className="log-message">{entry.message}</span>
                    </pre>
                  )
                })}
              </div>
            </>
          ) : (
            <div className="empty-detail">
              <h2>Select a job</h2>
              <p className="muted">Choose a job from the table to see its real-time log stream and metadata.</p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

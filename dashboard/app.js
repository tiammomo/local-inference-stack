const $ = (id) => document.getElementById(id)

const state = {
  hours: 24,
  status: null,
  history: [],
  baseline: null,
  routingTab: 'logical',
  live: null,
  socket: null,
  reconnectTimer: null,
  reconnectDelay: 1000,
  loading: false,
}

const labels = {
  completed: '正常完成',
  failed_before_response: '响应前失败',
  timeout_before_response: '响应前超时',
  upstream_error: '上游错误',
  upstream_timeout: '上游超时',
  delivery_error: '下游交付错误',
  downstream_cancelled: '客户端取消',
  tool_protocol: 'Tool Use 协议',
  context_or_token_limit: '上下文 / Token 限制',
  timeout: '超时',
  rate_or_quota_limit: '限流 / 配额',
  authentication: '鉴权',
  capacity: '容量',
  upstream_transport: '上游传输',
  other: '其他',
  qwen_unhealthy: 'Qwen 服务不可用',
  failure_rate: '调用失败率超过阈值',
  tool_failure_rate: 'Tool Use 失败率超过阈值',
  p95_latency_ms: 'P95 延迟超过阈值',
  timeouts: '窗口内存在超时',
  qwen_deferred_requests: 'llama.cpp 存在排队请求',
  ledger_unreconciled_increase: '账本 unreconciled 增加',
  ledger_expired_leases: '账本存在过期租约',
  report_truncated: '报告记录被截断',
}

const toolOutcomeLabels = {
  not_requested: '未请求工具',
  completed: '完成',
  completed_unobserved: '完成（未观测语义）',
  tool_called: '模型发起工具',
  answered_without_tool: '未调用工具直接回答',
  final_answer: '工具结果续轮完成',
  continuation_completed: '工具结果续轮完成',
  continuation_tool_called: '续轮再次调用工具',
  client_cancelled: '客户端取消',
  timeout: '超时',
  protocol_error: '协议错误',
  upstream_or_delivery_error: '上游 / 交付错误',
  unknown_legacy: '历史未知',
  legacy_unknown: '历史未知',
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;')
}

function number(value, digits = 0) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return '—'
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits }).format(numeric)
}

function compact(value) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return '—'
  return new Intl.NumberFormat('zh-CN', {
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(numeric)
}

function percent(value, empty = '—') {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return empty
  return `${(numeric * 100).toFixed(numeric >= 0.995 ? 0 : 1)}%`
}

function bytes(value) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return '—'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let result = numeric
  let unit = 0
  while (result >= 1024 && unit < units.length - 1) {
    result /= 1024
    unit += 1
  }
  return `${result.toFixed(unit >= 3 ? 1 : 0)} ${units[unit]}`
}

function duration(value) {
  if (value === null || value === undefined || value === '') return '—'
  const milliseconds = Number(value)
  if (!Number.isFinite(milliseconds)) return '—'
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`
  if (milliseconds < 60_000) return `${(milliseconds / 1000).toFixed(milliseconds < 10_000 ? 1 : 0)} s`
  return `${(milliseconds / 60_000).toFixed(1)} min`
}

function uptime(seconds) {
  const value = Number(seconds)
  if (!Number.isFinite(value)) return '—'
  const days = Math.floor(value / 86400)
  const hours = Math.floor((value % 86400) / 3600)
  const minutes = Math.floor((value % 3600) / 60)
  if (days) return `${days}d ${hours}h`
  if (hours) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

function clamp(value, minimum = 0, maximum = 100) {
  return Math.min(maximum, Math.max(minimum, Number(value) || 0))
}

function setText(id, value) {
  const element = $(id)
  if (element) element.textContent = value
}

function socketReady() {
  return state.socket?.readyState === WebSocket.OPEN
}

function sendSocket(payload) {
  if (!socketReady()) return false
  state.socket.send(JSON.stringify(payload))
  return true
}

function mergeLive() {
  if (!state.status || !state.live) return
  state.status.health = { ...state.status.health, ...state.live.health }
  state.status.process = { ...state.status.process, ...state.live.process }
}

function appendStatusHistory(report) {
  const traffic = report?.traffic || {}
  const qwen = report?.process?.qwen?.metrics || {}
  const gpu = report?.process?.gpu || {}
  const point = {
    timestamp: report?.generatedAtEpochMs,
    hours: report?.window?.hours,
    requests: traffic.requests || 0,
    successRate: traffic.serviceAvailabilityRate ?? traffic.successRate,
    availabilityRate: traffic.serviceAvailabilityRate,
    p95LatencyMs: traffic.latencyMs?.p95 || 0,
    ttftP95Ms: Number(traffic.firstByteLatencyMs?.samples) > 0
      ? traffic.firstByteLatencyMs.p95
      : null,
    toolUseSuccessRate: report?.toolUse?.protocolPassRate ?? report?.toolUse?.requestSuccessRate,
    cacheHitRate: traffic.tokens?.cacheHitRate,
    gpuMemoryUsedMiB: gpu.memoryUsedMiB,
    generationTokensPerSecond: qwen.generatedTokensPerSecond,
    alertCount: report?.alerts?.length || 0,
  }
  if (!Number.isFinite(Number(point.timestamp))) return
  state.history = state.history
    .filter((item) => item.timestamp !== point.timestamp && Number(item.hours) === state.hours)
    .concat(point)
    .sort((left, right) => left.timestamp - right.timestamp)
    .slice(-120)
}

function finishLoading() {
  state.loading = false
  $('refresh-button').disabled = false
  $('refresh-button').classList.remove('loading')
}

function handleSocketMessage(event) {
  let message
  try {
    message = JSON.parse(event.data)
  } catch {
    return
  }
  if (message.type === 'hello') {
    state.baseline = message.baseline
    const liveCadence = message.cadenceSeconds?.live || 2
    const statusCadence = message.cadenceSeconds?.status || 5
    setText('refresh-countdown', `WebSocket · 实时 ${liveCadence}s / 聚合 ${statusCadence}s`)
    if (state.status) render()
  } else if (message.type === 'live') {
    state.live = message.data
    mergeLive()
    if (state.status && state.baseline) {
      renderModel()
      renderGpu()
      renderEngine()
      renderServices()
      renderHost()
    }
  } else if (message.type === 'status') {
    if (Number(message.data?.window?.hours) !== state.hours) return
    state.status = message.data
    mergeLive()
    appendStatusHistory(message.data)
    render()
    finishLoading()
  } else if (message.type === 'history') {
    state.history = message.points || []
    if (state.status) renderHistory()
  } else if (message.type === 'error') {
    finishLoading()
    setConnection(false, message.message)
  }
}

function connectSocket() {
  if (state.reconnectTimer) {
    clearTimeout(state.reconnectTimer)
    state.reconnectTimer = null
  }
  if (state.socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(state.socket.readyState)) return
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const socket = new WebSocket(`${protocol}//${location.host}/ws`)
  state.socket = socket
  setText('connection-label', 'WebSocket 连接中')
  socket.addEventListener('open', () => {
    if (state.socket !== socket) return
    state.reconnectDelay = 1000
    setConnection(true)
    sendSocket({ type: 'subscribe', hours: state.hours })
  })
  socket.addEventListener('message', handleSocketMessage)
  socket.addEventListener('close', () => {
    if (state.socket !== socket) return
    state.socket = null
    finishLoading()
    setConnection(false, 'WebSocket 已断开，正在自动重连')
    const delay = state.reconnectDelay
    state.reconnectDelay = Math.min(state.reconnectDelay * 2, 10000)
    state.reconnectTimer = setTimeout(connectSocket, delay)
  })
  socket.addEventListener('error', () => socket.close())
}

function refresh() {
  if (state.loading) return
  state.loading = true
  $('refresh-button').disabled = true
  $('refresh-button').classList.add('loading')
  if (!sendSocket({ type: 'refresh', hours: state.hours })) {
    finishLoading()
    connectSocket()
  }
}

function setConnection(online, detail = '') {
  const element = $('connection-state')
  element.classList.toggle('online', online)
  element.classList.toggle('offline', !online)
  setText('connection-label', online ? 'WebSocket 实时流' : '实时流已断开')
  if (!online) {
    const banner = $('alert-banner')
    banner.classList.remove('hidden')
    banner.textContent = `运行数据暂时不可用：${detail || '请检查状态服务'}`
  } else if (!state.status?.alerts?.length) {
    $('alert-banner').classList.add('hidden')
  }
}

function render() {
  if (!state.status || !state.baseline) return
  renderModel()
  renderGpu()
  renderEngine()
  renderMetrics()
  renderHistory()
  renderServices()
  renderLatency()
  renderRouting()
  renderToolUse()
  renderHost()
  renderSignals()
}

function renderModel() {
  const { baseline, status } = state
  const qwen = status.process.qwen
  const runtime = qwen.runtime || {}
  const healthy = status.health.qwenHealthy && status.health.modelportReady
  setText('model-name', baseline.displayName)
  setText('model-alias', runtime.modelAlias || baseline.deploymentName)
  setText('model-type', `${baseline.quantization} · ${runtime.modelType || 'GGUF'}`)
  setText('context-value', `${number((runtime.contextSize || baseline.contextTokens) / 1024, 0)}K`)
  setText('input-budget', `≈${number(baseline.recommendedInputTokens / 1000, 0)}K`)
  setText('output-budget', `${number(baseline.maxOutputTokens / 1024, 0)}K`)
  $('context-fill').style.width = `${clamp(baseline.recommendedInputTokens / baseline.contextTokens * 100)}%`
  const health = $('model-health')
  health.textContent = healthy ? '运行正常' : '需要检查'
  health.className = `health-badge ${healthy ? '' : 'error'}`
  setText('runtime-build', `llama.cpp ${runtime.buildInfo || '—'}`)
  const updatedAt = state.live?.generatedAtEpochMs || status.generatedAtEpochMs
  setText('last-updated', `实时 ${new Date(updatedAt).toLocaleTimeString('zh-CN', { hour12: false })}`)

  const specs = [
    ['KV Cache', baseline.kvCache],
    ['思考模式', baseline.reasoningDefault ? '默认开启' : '默认关闭'],
    ['运行档位', baseline.profile],
    ['Tool Use', 'Strict response'],
  ]
  $('model-specs').innerHTML = specs.map(([label, value], index) =>
    `<div><span>${escapeHtml(label)}</span><strong class="${index === 1 ? 'accent' : ''}">${escapeHtml(value)}</strong></div>`
  ).join('')
}

function renderGpu() {
  const gpu = state.status.process.gpu
  if (!gpu) return
  const memoryPercent = gpu.memoryTotalMiB ? gpu.memoryUsedMiB / gpu.memoryTotalMiB * 100 : 0
  setText('gpu-name', gpu.name || 'NVIDIA GPU')
  setText('gpu-memory-percent', `${Math.round(memoryPercent)}%`)
  setText('gpu-memory', `${(gpu.memoryUsedMiB / 1024).toFixed(1)} / ${(gpu.memoryTotalMiB / 1024).toFixed(1)} GiB`)
  setText('gpu-util', `${number(gpu.utilizationPercent)}%`)
  setText('gpu-temp', `${number(gpu.temperatureC)} °C`)
  setText('gpu-power', `${number(gpu.powerWatts, 1)} W`)
  $('gpu-gauge').style.setProperty('--value', clamp(memoryPercent))
}

function renderEngine() {
  const qwen = state.status.process.qwen
  const metrics = qwen.metrics || {}
  const busy = (qwen.slots || []).some((slot) => slot.processing)
  setText('slot-state', busy ? '推理中' : '空闲')
  $('slot-state').classList.toggle('busy', busy)
  setText('prompt-speed', number(metrics.promptTokensPerSecond, 1))
  setText('generation-speed', number(metrics.generatedTokensPerSecond, 1))
  setText('processing-count', number(metrics.requestsProcessing))
  setText('deferred-count', number(metrics.requestsDeferred))
  setText('prompt-total', compact(metrics.promptTokensTotal))
  setText('generated-total', compact(metrics.generatedTokensTotal))
  $('prompt-speed-bar').style.width = `${clamp(metrics.promptTokensPerSecond / 600 * 100)}%`
  $('generation-speed-bar').style.width = `${clamp(metrics.generatedTokensPerSecond / 130 * 100)}%`
}

function renderMetrics() {
  const traffic = state.status.traffic
  const tool = state.status.toolUse
  const tokens = traffic.tokens || {}
  setText('success-rate', percent(traffic.serviceAvailabilityRate ?? traffic.successRate))
  setText('success-detail', `${number(traffic.serviceSuccesses ?? traffic.successes)} 成功 / ${number(traffic.serviceRequests ?? traffic.requests)} 服务请求 · 取消 ${number(traffic.clientCancellations || 0)}`)
  setText('p95-latency', duration(traffic.latencyMs.p95))
  setText('latency-detail', `P50 ${duration(traffic.latencyMs.p50)} · P99 ${duration(traffic.latencyMs.p99)}`)
  const streamTtft = traffic.firstByteLatencyMs || {}
  const hasTtft = Number(streamTtft.samples) > 0
  setText('ttft-p95', duration(hasTtft ? streamTtft.p95 : undefined))
  setText('ttft-detail', hasTtft
    ? `P50 ${duration(streamTtft.p50)} · P99 ${duration(streamTtft.p99)} · ${number(streamTtft.samples)} 流式样本`
    : '等待流式首语义样本')
  setText('cache-rate', percent(tokens.cacheHitRate))
  setText('cache-detail', `读取 ${compact(tokens.cacheRead)} Token`)
  setText('tool-rate', percent(tool.protocolPassRate))
  setText('tool-detail', `${number(tool.modelToolCalls)} 次合法调用 · ${number(tool.continuationCompletions)} 次续轮完成`)
  setText('token-total', compact((tokens.input || 0) + (tokens.output || 0) + (tokens.cacheRead || 0) + (tokens.cacheWrite || 0)))
  setText('token-detail', `输入 ${compact(tokens.input)} · 输出 ${compact(tokens.output)}`)
  setText('window-label', windowName(state.hours))
}

function renderHistory() {
  const allPoints = state.history.filter((point) =>
    Number.isFinite(Number(point.timestamp)) && point.successRate !== null
  )
  const element = $('history-chart')
  if (allPoints.length < 2) {
    element.innerHTML = '<div class="empty-state">等待至少两个历史快照</div>'
    setText('history-range', `${allPoints.length} 个聚合快照`)
    return
  }
  const maximumPoints = 60
  const points = allPoints.length <= maximumPoints
    ? allPoints
    : Array.from({ length: maximumPoints }, (_, index) =>
      allPoints[Math.round(index * (allPoints.length - 1) / (maximumPoints - 1))]
    )
  const measuredWidth = Math.round(element.clientWidth || 720)
  const compactChart = measuredWidth < 600
  const width = Math.max(300, measuredWidth)
  const height = 230
  const left = compactChart ? 42 : 58
  const right = compactChart ? 68 : 94
  const successTop = 16
  const successBottom = 128
  const latencyTop = 158
  const latencyBottom = 194
  const axisY = 222
  const plotWidth = width - left - right
  const x = (index) => left + (points.length === 1 ? 0 : index / (points.length - 1) * plotWidth)
  const successValues = points.map((point) => Number(point.successRate))
  const successMinimum = Math.min(...successValues)
  const successFloor = Math.max(0, Math.min(0.95, Math.floor((successMinimum - 0.01) * 20) / 20))
  const successY = (value) => {
    const normalized = (Number(value) - successFloor) / Math.max(0.001, 1 - successFloor)
    return successBottom - clamp(normalized, 0, 1) * (successBottom - successTop)
  }
  const latencyValues = points.map((point) => Number(point.p95LatencyMs) || 0)
  const minLatency = Math.min(...latencyValues)
  const maxLatency = Math.max(...latencyValues, 1)
  const latencyY = (value) => {
    if (maxLatency === minLatency) return (latencyTop + latencyBottom) / 2
    const normalized = ((Number(value) || 0) - minLatency) / (maxLatency - minLatency)
    return latencyBottom - normalized * (latencyBottom - latencyTop)
  }
  const smoothPath = (coordinates) => {
    if (coordinates.length < 2) return ''
    let path = `M ${coordinates[0][0].toFixed(1)} ${coordinates[0][1].toFixed(1)}`
    for (let index = 1; index < coordinates.length; index += 1) {
      const previous = coordinates[index - 1]
      const current = coordinates[index]
      const middle = (previous[0] + current[0]) / 2
      path += ` C ${middle.toFixed(1)} ${previous[1].toFixed(1)}, ${middle.toFixed(1)} ${current[1].toFixed(1)}, ${current[0].toFixed(1)} ${current[1].toFixed(1)}`
    }
    return path
  }
  const successCoordinates = points.map((point, index) => [x(index), successY(point.successRate)])
  const latencyCoordinates = points.map((point, index) => [x(index), latencyY(point.p95LatencyMs)])
  const successPath = smoothPath(successCoordinates)
  const latencyPath = smoothPath(latencyCoordinates)
  const areaPath = `${successPath} L ${successCoordinates.at(-1)[0].toFixed(1)} ${successBottom} L ${successCoordinates[0][0].toFixed(1)} ${successBottom} Z`
  const first = new Date(points[0].timestamp)
  const last = new Date(points.at(-1).timestamp)
  const currentSuccess = points.at(-1).successRate
  const currentLatency = points.at(-1).p95LatencyMs
  const realtimeRange = last.getTime() - first.getTime() < 86_400_000
  const sameDay = first.toDateString() === last.toDateString()
  const formatAxis = (value) => {
    if (realtimeRange && sameDay) {
      return value.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
    }
    if (realtimeRange) {
      return `${value.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })} ${value.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' })}`
    }
    return value.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
  }
  const grid = [0, 0.5, 1].map((fraction) => {
    const y = successTop + fraction * (successBottom - successTop)
    return `<line class="chart-grid" x1="${left}" y1="${y}" x2="${left + plotWidth}" y2="${y}" />`
  }).join('')
  element.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" aria-hidden="true">
      <defs>
        <linearGradient id="chart-area-gradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#54e3e3" stop-opacity=".20"/><stop offset="1" stop-color="#54e3e3" stop-opacity="0"/></linearGradient>
      </defs>
      ${grid}
      <path class="chart-area" d="${areaPath}" />
      <path class="chart-line success" d="${successPath}" />
      <circle class="chart-current success" cx="${successCoordinates.at(-1)[0]}" cy="${successCoordinates.at(-1)[1]}" r="4" />
      <line class="chart-separator" x1="${left}" y1="144" x2="${left + plotWidth}" y2="144" />
      <rect class="chart-latency-lane" x="${left}" y="${latencyTop - 6}" width="${plotWidth}" height="${latencyBottom - latencyTop + 12}" rx="5" />
      <path class="chart-line latency" d="${latencyPath}" />
      <circle class="chart-current latency" cx="${latencyCoordinates.at(-1)[0]}" cy="${latencyCoordinates.at(-1)[1]}" r="3.5" />
      <text class="chart-label axis" x="6" y="${successTop + 4}">100%</text>
      <text class="chart-label axis" x="6" y="${successBottom}">${Math.round(successFloor * 100)}%</text>
      <text class="chart-label lane" x="6" y="${(latencyTop + latencyBottom) / 2 + 4}">P95</text>
      <text class="chart-value success" x="${width - right + 14}" y="${successCoordinates.at(-1)[1] + 4}">${percent(currentSuccess)}</text>
      <text class="chart-value latency" x="${width - right + 14}" y="${(latencyTop + latencyBottom) / 2 + 4}">${duration(currentLatency)}</text>
      <text class="chart-label" x="${left}" y="${axisY}">${formatAxis(first)}</text>
      <text class="chart-label" text-anchor="end" x="${left + plotWidth}" y="${axisY}">${formatAxis(last)}</text>
    </svg>`
  setText('history-range', `${allPoints.length} 个快照 · 当前 P95 ${duration(currentLatency)} · 峰值 ${duration(maxLatency)}`)
}

function renderServices() {
  const status = state.status
  const containers = status.process.containers || []
  const fallback = [
    { name: 'qwen35-9b-q5km', status: status.health.qwenHealthy ? 'running' : 'down', healthy: status.health.qwenHealthy ? 'healthy' : 'unhealthy' },
    { name: 'modelport-modelport-1', status: status.health.modelportReady ? 'running' : 'down', healthy: status.health.modelportReady ? 'healthy' : 'unhealthy' },
    { name: 'modelport-dashboard-1', status: 'unknown', healthy: 'unknown' },
  ]
  const rows = containers.length ? containers : fallback
  const healthyCount = rows.filter((row) => row.status === 'running' && (!row.healthy || row.healthy === 'healthy')).length
  setText('service-summary', `${healthyCount}/${rows.length} 正常`)
  $('service-list').innerHTML = rows.map((row) => {
    const okay = row.status === 'running' && (!row.healthy || row.healthy === 'healthy')
    const label = row.name.includes('qwen') ? 'Qwen Runtime' : row.name.includes('dashboard') ? 'ModelPort UI' : 'ModelPort Gateway'
    const meta = row.imageId ? row.imageId.slice(7, 19) : row.status
    return `<div class="service-row">
      <div class="service-main"><i class="service-indicator ${okay ? '' : 'down'}"></i><div><span class="service-name">${escapeHtml(label)}</span><span class="service-meta">${escapeHtml(meta || '—')}</span></div></div>
      <span class="service-status">${okay ? 'healthy' : escapeHtml(row.healthy || row.status || 'unknown')}</span>
    </div>`
  }).join('')
}

function renderLatency() {
  const latency = state.status.traffic.latencyMs
  const firstByte = state.status.traffic.firstByteLatencyMs || {}
  const hasTtft = Number(firstByte.samples) > 0
  const groups = [
    ['流式首语义 TTFT', [
      ['P50', hasTtft ? firstByte.p50 : undefined],
      ['P95', hasTtft ? firstByte.p95 : undefined],
      ['P99', hasTtft ? firstByte.p99 : undefined],
    ]],
    ['完整生命周期 E2E', [
      ['P50', latency.p50],
      ['P95', latency.p95],
      ['P99', latency.p99],
    ]],
  ]
  $('latency-bars').innerHTML = groups.map(([title, rows], groupIndex) => {
    const maximum = Math.max(...rows.map(([, value]) => Number(value) || 0), 1)
    return `<section class="latency-group ${groupIndex ? 'e2e' : 'ttft'}">
      <h3>${escapeHtml(title)}</h3>
      ${rows.map(([label, value]) => `
        <div class="latency-row">
          <span>${label}</span>
          <div class="latency-track"><i style="width:${clamp(Number(value) / maximum * 100)}%"></i></div>
          <strong>${escapeHtml(duration(value))}</strong>
        </div>`).join('')}
    </section>`
  }).join('')
}

function renderRouting() {
  const sources = {
    logical: [state.status.traffic.byLogicalModel || [], 'model'],
    input: [state.status.traffic.byInputBucket || [], 'inputBucket'],
    providers: [state.status.traffic.byProvider || [], 'provider'],
  }
  const [unfiltered, key] = sources[state.routingTab] || sources.logical
  const source = unfiltered.filter((item) => {
    if (state.routingTab === 'providers') return item[key] === 'local_qwen'
    if (state.routingTab === 'input') return true
    return String(item[key] || '').toLowerCase().includes('qwen3.5')
  })
  const maximum = Math.max(...source.map((item) => Number(item.requests) || 0), 1)
  setText('routing-total', `${number(source.length)} 项`)
  $('routing-list').innerHTML = source.slice(0, 6).map((item) => `
    <div class="ranking-item">
      <div class="ranking-main"><span class="ranking-name" title="${escapeHtml(item[key])}">${escapeHtml(item[key])}</span><span class="ranking-value">${number(item.requests)} · ${percent(item.serviceAvailabilityRate ?? item.successRate)} · P95 ${duration(item.latencyMs?.p95)}</span></div>
      <div class="ranking-track"><i style="width:${clamp(item.requests / maximum * 100)}%"></i></div>
    </div>`).join('') || '<div class="empty-state">当前窗口没有调用</div>'
}

function renderToolUse() {
  const tool = state.status.toolUse
  const score = tool.protocolPassRate === null ? '—' : percent(tool.protocolPassRate)
  setText('tool-score', score)
  setText('tool-requests', number(tool.observedRequests))
  setText('tool-successes', number(tool.schemaValidatedCalls))
  setText('tool-failures', number(tool.protocolErrors))
  const outcomes = Object.entries(tool.byOutcome || {})
    .sort((left, right) => Number(right[1]) - Number(left[1]))
  $('tool-outcomes').innerHTML = outcomes.length
    ? outcomes.map(([name, value]) => `<span>${escapeHtml(toolOutcomeLabels[name] || name)} <b>${number(value)}</b></span>`).join('')
    : '<span>暂无分类数据</span>'
}

function renderHost() {
  const host = state.status.process.host
  if (!host) return
  const usedPercent = host.memoryTotalBytes ? host.memoryUsedBytes / host.memoryTotalBytes * 100 : 0
  setText('host-memory', `${bytes(host.memoryUsedBytes)} / ${bytes(host.memoryTotalBytes)}`)
  setText('host-available', bytes(host.memoryAvailableBytes))
  setText('swap-used', `${bytes(host.swapUsedBytes)} / ${bytes(host.swapTotalBytes)}`)
  setText('load-one', number(host.loadAverage?.[0], 2))
  setText('load-fifteen', number(host.loadAverage?.[2], 2))
  $('host-memory-fill').style.width = `${clamp(usedPercent)}%`
}

function renderSignals() {
  const alerts = state.status.alerts || []
  const banner = $('alert-banner')
  if (alerts.length) {
    banner.classList.remove('hidden')
    banner.textContent = `检测到 ${alerts.length} 个运行信号：${alerts.map((alert) => labels[alert.code] || alert.code).join('、')}`
  } else {
    banner.classList.add('hidden')
  }
  setText('alert-count', alerts.length ? `${alerts.length} 项告警` : '无活动告警')
  $('alert-list').innerHTML = alerts.length
    ? alerts.map((alert) => `<div class="alert-entry warning"><span>${escapeHtml(labels[alert.code] || alert.code)}</span><strong>${escapeHtml(alertValue(alert.value))}</strong></div>`).join('')
    : '<div class="alert-entry clear"><span>所有阈值均在基线内</span><strong>OK</strong></div>'

  const terminals = Object.entries(state.status.issues.byTerminalReason || {})
    .sort((a, b) => Number(b[1]) - Number(a[1]))
  const maximum = Math.max(...terminals.map(([, value]) => Number(value) || 0), 1)
  $('terminal-list').innerHTML = terminals.slice(0, 6).map(([name, value]) => `
    <div class="terminal-entry">
      <span>${escapeHtml(labels[name] || name)}</span>
      <div class="terminal-bar"><i style="width:${clamp(Number(value) / maximum * 100)}%"></i></div>
      <strong>${number(value)}</strong>
    </div>`).join('') || '<div class="alert-entry clear"><span>当前窗口没有请求终态</span><strong>—</strong></div>'
}

function alertValue(value) {
  if (value && typeof value === 'object') {
    if ('increase' in value) return `+${value.increase}`
    return JSON.stringify(value)
  }
  if (typeof value === 'number' && value > 0 && value < 1) return percent(value)
  return String(value ?? '—')
}

function windowName(hours) {
  if (hours === 1) return '最近 1 小时'
  if (hours === 6) return '最近 6 小时'
  if (hours === 24) return '最近 24 小时'
  return '最近 7 天'
}

document.querySelectorAll('[data-hours]').forEach((button) => {
  button.addEventListener('click', () => {
    const hours = Number(button.dataset.hours)
    if (hours === state.hours) return
    state.hours = hours
    document.querySelectorAll('[data-hours]').forEach((item) => item.classList.toggle('active', item === button))
    state.loading = true
    $('refresh-button').disabled = true
    $('refresh-button').classList.add('loading')
    if (!sendSocket({ type: 'subscribe', hours })) {
      finishLoading()
      connectSocket()
    }
  })
})

document.querySelectorAll('[data-routing-tab]').forEach((button) => {
  button.addEventListener('click', () => {
    state.routingTab = button.dataset.routingTab
    document.querySelectorAll('[data-routing-tab]').forEach((item) => item.classList.toggle('active', item === button))
    if (state.status) renderRouting()
  })
})

$('refresh-button').addEventListener('click', refresh)

window.addEventListener('online', connectSocket)
window.addEventListener('beforeunload', () => state.socket?.close())
connectSocket()

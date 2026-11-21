import { createElement as h, Fragment, useEffect, useMemo, useState } from 'react'
import {
  Badge,
  Button,
  ErrorState,
  Input,
  ListRow,
  Loader,
  PALETTE_AREA,
  STATUSBAR_AREAS,
  SegmentedControl,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Separator,
  StatusDot,
  Tip,
  ToggleRow,
  atom,
  cn,
  host,
  icons,
  isSubmitEnter,
  pluginSettingsHref,
  queryClient,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'

const PLUGIN_ID = 'true-moa'
const SETTINGS_PAGE_ID = 'settings'
const QUERY_ROOT = ['plugin', PLUGIN_ID, 'state']
const STORAGE_STATUSBAR_KEY = 'showStatusbarChip'
const $showStatusbarChip = atom(true)

const COPY = {
  en: {
    pageSubtitle:
      'Keeps Mixture of Agents answers reliable. Failed advisor models are retried automatically, broken responses never pollute cache, and the turn stops safely instead of letting the main model guess alone.',
    badgeArmed: 'Armed',
    badgePaused: 'Paused',
    badgeNative: 'native hooks',
    badgeSeams: count => `${count}/7 seams`,
    runDoctorBtn: 'Run Health Check',
    reverifyBtn: 'Re-verify Gate',
    saveBtn: 'Save',
    savedToast: 'True MoA settings saved.',
    doctorToast: 'Health check refreshed.',
    reverifyToast: 'True MoA gate re-verified.',
    errorTitle: 'Could not load True MoA settings',
    retryLoadBtn: 'Try Again',
    sectionCore: 'Core Protection',
    sectionCoreDesc: 'Control how strictly advisor models must respond before the main model writes the final answer.',
    enabledLabel: 'Strict MoA Protection',
    enabledDesc:
      'Require advisor models to respond before answering. When off, Hermes returns to its default behavior where the main model answers alone if advisors fail.',
    blockSelfLabel: 'Block Main-Model Self-Answering',
    blockSelfDesc:
      'Stop the main aggregator model from quietly answering in place of a failed or rate-limited advisor.',
    quorumTitle: 'Minimum Healthy Advisors (Quorum)',
    quorumDesc:
      'Choose whether every enabled advisor must succeed (recommended), or allow multi-advisor presets to continue once at least N advisors respond after all retries finish.',
    quorumAll: 'All enabled advisors (Strict)',
    quorumMin: n => (n === 1 ? 'At least 1 healthy advisor' : `At least ${n} healthy advisors`),
    sectionRetry: 'Retry Schedule',
    sectionRetryDesc: 'Choose how many times True MoA retries a failed advisor and how long it waits between attempts.',
    retryModeTitle: 'Retry Mode',
    retryModeDesc: 'Pick one clear retry strategy. Only the settings for your selected mode are used.',
    modeManual: 'Manual Schedule',
    modeSync: 'Match Hermes Retries',
    modeCustom: 'Fixed Count',
    manualTitle: 'Wait Seconds Between Retries',
    manualDesc:
      'Comma-separated wait times in seconds before each retry (for example: 15, 30, 60, 60, 60 gives 6 total attempts).',
    manualPlaceholder: '15, 30, 60, 60, 60',
    manualInvalid: 'Enter one or more non-negative wait times in seconds (for example: 15, 30, 60).',
    syncInfoTitle: 'Synced with Hermes API Retries',
    syncInfoDesc:
      'Automatically matches the retry count in Settings > Advanced > Agent limits > API Retries and builds a progressive wait schedule.',
    customTitle: 'Total Attempts per Advisor',
    customDesc: 'Enter a fixed total attempt count from 1 to 25. True MoA generates a smooth progressive wait schedule automatically.',
    customInvalid: 'Enter a whole number between 1 and 25.',
    activeScheduleLabel: 'Active schedule:',
    activeScheduleSummary: (attempts, waits, totalSleep) =>
      waits.length > 0
        ? `${attempts} total attempts · waits ${waits.map(s => `${s}s`).join(' → ')} (max ${totalSleep}s) → safe stop`
        : '1 attempt · no retry wait → safe stop',
    budgetTitle: 'Max Retry Wait Cap (seconds)',
    budgetDesc:
      'Cap total time spent waiting between retries. Set 0 to run the full schedule above. Active advisor requests already running are never cut off.',
    budgetInvalid: 'Enter 0 or a positive number of seconds.',
    sectionBackup: 'Backup Advisor Failover',
    sectionBackupDesc: 'Optionally switch failed slots to a backup model before giving up, while keeping strict protection.',
    backupToggleLabel: 'Backup Advisor Failover',
    backupToggleDesc:
      'Switch failed advisors to a backup model after repeated errors (or immediately on invalid API key or missing model errors).',
    backupModelsTitle: 'Backup Advisor Model(s)',
    backupModelsDesc:
      'Enter one backup model as provider:model, or a comma-separated failover list (for example: openrouter:qwen-plus, gemini:gemini-2.5-flash). Must differ from your main aggregator model.',
    backupModelsPlaceholder: 'provider:model, provider2:model2',
    backupTriggerTitle: 'Switch After Failed Attempts',
    backupTriggerDesc: 'Consecutive primary advisor failures before switching that slot to your backup model.',
    backupTriggerOption: n => (n === 1 ? 'After 1 failed attempt' : `After ${n} failed attempts`),
    sectionHealth: 'Health Check & Recovery Activity',
    sectionHealthDesc: 'Zero-token offline check of your MoA preset, API keys, storage hygiene, and 90-day recovery counters.',
    verdictHealthy: 'Healthy',
    verdictWarnings: 'Healthy (Warnings)',
    verdictIssues: 'Issues Found',
    checkLabels: {
      gate: 'Runtime Gate',
      pyproject_pm: 'Workspace Hygiene',
      plugin_data: 'Isolated Data Storage',
      moa_preset: 'MoA Preset & Keys',
      fallback: 'Backup Advisor',
      ladder: 'Retry Schedule',
      quorum: 'Advisor Quorum'
    },
    statRetries: 'Advisor Retries',
    statSwitches: 'Backup Switches',
    statQuorum: 'Quorum Saves',
    statOutages: 'Safe Stops',
    statBlocked: 'Blocked Self-Answers',
    statusbarToggleLabel: 'Show Status-Bar Indicator',
    statusbarToggleDesc: 'Display a compact MoA status chip in the bottom-right status bar for quick access.',
    chipArmed: 'MoA Strict',
    chipPaused: 'MoA Off',
    chipTipArmed: 'True MoA strict protection is active. Click to open settings and health check.',
    chipTipPaused: 'True MoA is paused (stock fail-open mode). Click to configure.',
    paletteOpen: 'True MoA: Open Settings & Health Check',
    paletteToggle: 'True MoA: Toggle Strict Protection On / Off'
  },
  vi: {
    pageSubtitle:
      'Giữ câu trả lời Mixture of Agents luôn đáng tin cậy. Cố vấn gặp lỗi sẽ được tự động thử lại, phản hồi lỗi không bao giờ lưu vào bộ nhớ đệm, và lượt chat dừng an toàn thay vì để mô hình chính tự đoán một mình.',
    badgeArmed: 'Đang bật',
    badgePaused: 'Tạm tắt',
    badgeNative: 'hook chính thức',
    badgeSeams: count => `${count}/7 điểm chặn`,
    runDoctorBtn: 'Kiểm tra sức khỏe',
    reverifyBtn: 'Xác minh lại cổng',
    saveBtn: 'Lưu',
    savedToast: 'Đã lưu cài đặt True MoA.',
    doctorToast: 'Đã cập nhật chẩn đoán sức khỏe.',
    reverifyToast: 'Đã xác minh lại cổng bảo vệ True MoA.',
    errorTitle: 'Không tải được cài đặt True MoA',
    retryLoadBtn: 'Thử lại',
    sectionCore: 'Bảo vệ cốt lõi',
    sectionCoreDesc: 'Kiểm soát mức độ bắt buộc các mô hình cố vấn phải phản hồi trước khi mô hình chính tổng hợp câu trả lời.',
    enabledLabel: 'Bảo vệ MoA nghiêm ngặt',
    enabledDesc:
      'Bắt buộc các mô hình cố vấn phản hồi thành công trước khi trả lời. Khi tắt, Hermes quay về mặc định (mô hình chính tự trả lời một mình nếu cố vấn lỗi).',
    blockSelfLabel: 'Chặn mô hình chính tự đóng giả cố vấn',
    blockSelfDesc:
      'Ngăn mô hình tổng hợp chính âm thầm tự trả lời thay thế khi một mô hình cố vấn gặp lỗi hoặc bị giới hạn tốc độ.',
    quorumTitle: 'Số cố vấn phản hồi tối thiểu (Quorum)',
    quorumDesc:
      'Chọn bắt buộc tất cả cố vấn đều thành công (khuyên dùng), hoặc cho phép tiếp tục khi có ít nhất N cố vấn phản hồi khỏe sau khi đã thử lại hết lượt.',
    quorumAll: 'Tất cả cố vấn đang bật (Nghiêm ngặt)',
    quorumMin: n => `Tối thiểu ${n} cố vấn phản hồi khỏe`,
    sectionRetry: 'Lịch thử lại khi cố vấn lỗi',
    sectionRetryDesc: 'Chọn số lần True MoA tự động thử lại cố vấn gặp lỗi và thời gian nghỉ giữa mỗi lần gọi.',
    retryModeTitle: 'Chế độ thử lại',
    retryModeDesc: 'Chọn một chế độ rõ ràng. Hệ thống chỉ hiển thị và áp dụng đúng thông số của chế độ bạn chọn.',
    modeManual: 'Thủ công',
    modeSync: 'Đồng bộ Hermes',
    modeCustom: 'Số lần cố định',
    manualTitle: 'Số giây chờ giữa mỗi lần thử lại',
    manualDesc:
      'Nhập danh sách số giây nghỉ cách nhau bằng dấu phẩy (ví dụ: 15, 30, 60, 60, 60 tương ứng tổng cộng 6 lần gọi).',
    manualPlaceholder: '15, 30, 60, 60, 60',
    manualInvalid: 'Vui lòng nhập các số giây hợp lệ cách nhau bằng dấu phẩy (ví dụ: 15, 30, 60).',
    syncInfoTitle: 'Đang đồng bộ theo API Retries của Hermes',
    syncInfoDesc:
      'Tự động lấy số lần thử trong Settings > Advanced > Agent limits > API Retries và sinh lịch chờ tăng dần.',
    customTitle: 'Tổng số lần gọi tối đa mỗi cố vấn',
    customDesc: 'Nhập tổng số lần thử từ 1 đến 25. True MoA sẽ tự động tạo lịch nghỉ tăng dần hợp lý.',
    customInvalid: 'Vui lòng nhập số nguyên từ 1 đến 25.',
    activeScheduleLabel: 'Lịch đang áp dụng:',
    activeScheduleSummary: (attempts, waits, totalSleep) =>
      waits.length > 0
        ? `Tổng ${attempts} lần gọi · nghỉ ${waits.map(s => `${s}s`).join(' → ')} (tối đa ${totalSleep}s) → dừng an toàn`
        : '1 lần gọi · không chờ thử lại → dừng an toàn',
    budgetTitle: 'Giới hạn tổng thời gian chờ nghỉ (giây)',
    budgetDesc:
      'Trần tổng số giây chờ giữa các lần thử lại. Đặt 0 để dùng trọn lịch ở trên. Yêu cầu đang xử lý dở không bao giờ bị cắt ngang.',
    budgetInvalid: 'Vui lòng nhập 0 hoặc số giây dương.',
    sectionBackup: 'Cố vấn dự phòng khi lỗi liên tiếp',
    sectionBackupDesc: 'Tùy chọn chuyển vị trí cố vấn đang lỗi sang mô hình dự phòng trước khi dừng lượt, vẫn giữ nguyên bảo vệ nghiêm ngặt.',
    backupToggleLabel: 'Tự động chuyển sang cố vấn dự phòng',
    backupToggleDesc:
      'Chuyển cố vấn gặp lỗi liên tiếp sang mô hình dự phòng (hoặc chuyển ngay lập tức nếu sai API key / không tìm thấy model).',
    backupModelsTitle: 'Mô hình cố vấn dự phòng',
    backupModelsDesc:
      'Nhập một mô hình dạng provider:model, hoặc chuỗi dự phòng nối tiếp cách nhau bằng dấu phẩy (ví dụ: openrouter:qwen-plus, gemini:gemini-2.5-flash). Không được trùng mô hình tổng hợp chính.',
    backupModelsPlaceholder: 'provider:model, provider2:model2',
    backupTriggerTitle: 'Chuyển mạch sau số lần lỗi',
    backupTriggerDesc: 'Số lần cố vấn chính gặp lỗi liên tiếp trước khi chuyển vị trí đó sang mô hình dự phòng.',
    backupTriggerOption: n => `Sau ${n} lần thử lỗi`,
    sectionHealth: 'Chẩn đoán sức khỏe & Hoạt động khôi phục',
    sectionHealthDesc: 'Kiểm tra tĩnh không tốn token cho preset MoA, khóa API, vùng lưu trữ dữ liệu và bộ đếm khôi phục 90 ngày.',
    verdictHealthy: 'Khỏe mạnh',
    verdictWarnings: 'Khỏe mạnh (Có lưu ý)',
    verdictIssues: 'Phát hiện vấn đề',
    checkLabels: {
      gate: 'Cổng bảo vệ Runtime',
      pyproject_pm: 'Vệ sinh môi trường PM',
      plugin_data: 'Lưu trữ dữ liệu tách biệt',
      moa_preset: 'Preset MoA & Khóa API',
      fallback: 'Cố vấn dự phòng',
      ladder: 'Lịch thử lại',
      quorum: 'Ngưỡng cố vấn (Quorum)'
    },
    statRetries: 'Lần thử lại',
    statSwitches: 'Chuyển dự phòng',
    statQuorum: 'Lượt Quorum',
    statOutages: 'Dừng an toàn',
    statBlocked: 'Chặn tự trả lời',
    statusbarToggleLabel: 'Hiển thị nhãn trạng thái ở thanh dưới cùng',
    statusbarToggleDesc: 'Hiện nút trạng thái MoA nhỏ gọn ở góc phải thanh trạng thái để mở nhanh cài đặt.',
    chipArmed: 'MoA Bật',
    chipPaused: 'MoA Tắt',
    chipTipArmed: 'Bảo vệ nghiêm ngặt True MoA đang bật. Nhấn để mở cài đặt và kiểm tra sức khỏe.',
    chipTipPaused: 'True MoA đang tạm tắt (chế độ mặc định). Nhấn để cấu hình.',
    paletteOpen: 'True MoA: Mở cài đặt & Kiểm tra sức khỏe',
    paletteToggle: 'True MoA: Bật / Tắt bảo vệ nghiêm ngặt'
  }
}

function normalizeLang(raw) {
  return String(raw || '').toLowerCase().startsWith('vi') ? 'vi' : 'en'
}

function SectionCard({ title, description, badge, children }) {
  return h(
    'section',
    { className: 'rounded-xl border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary)/30 p-4' },
    h(
      'div',
      { className: 'mb-3 flex flex-wrap items-start justify-between gap-2' },
      h(
        'div',
        { className: 'grid gap-0.5' },
        h('h3', { className: 'text-sm font-semibold text-foreground' }, title),
        description && h('p', { className: 'text-xs leading-relaxed text-muted-foreground' }, description)
      ),
      badge
    ),
    h('div', { className: 'grid divide-y divide-(--ui-stroke-secondary)' }, children)
  )
}

function TrueMoASettingsPage({ ctx }) {
  const qc = useQueryClient()
  const activeProfile = useValue(host.state.profile)
  const showChip = useValue($showStatusbarChip)
  const queryKey = useMemo(() => [...QUERY_ROOT, activeProfile || 'default'], [activeProfile])

  const stateQuery = useQuery({
    queryKey,
    queryFn: () => ctx.rest('/state')
  })

  const snapshot = stateQuery.data
  const settings = snapshot?.settings || {}
  const serverLang = normalizeLang(settings.language)
  const [optimisticLang, setOptimisticLang] = useState(null)
  const lang = optimisticLang || serverLang
  const t = COPY[lang] || COPY.en

  const [ladderDraft, setLadderDraft] = useState('')
  const [customDraft, setCustomDraft] = useState('5')
  const [budgetDraft, setBudgetDraft] = useState('0')
  const [backupModelDraft, setBackupModelDraft] = useState('')

  useEffect(() => {
    setOptimisticLang(null)
  }, [serverLang])

  const serializedLadder = Array.isArray(settings.ladder) ? settings.ladder.join(', ') : '15, 30, 60, 60, 60'
  useEffect(() => {
    setLadderDraft(serializedLadder)
  }, [serializedLadder])

  useEffect(() => {
    setCustomDraft(String(settings.custom_retries > 0 ? settings.custom_retries : 5))
  }, [settings.custom_retries])

  useEffect(() => {
    setBudgetDraft(String(settings.max_wait_budget_s ?? 0))
  }, [settings.max_wait_budget_s])

  useEffect(() => {
    setBackupModelDraft(String(settings.secondary_reference_model || ''))
  }, [settings.secondary_reference_model])

  const patchMutation = useMutation({
    mutationFn: ({ path = '/settings', body = {}, toastText = '' }) =>
      ctx.rest(path, { method: 'POST', body }).then(data => ({ data, toastText })),
    onSuccess: ({ data, toastText }) => {
      qc.setQueryData(queryKey, data)
      qc.invalidateQueries({ queryKey: QUERY_ROOT })
      if (toastText) host.notify(toastText)
    },
    onError: err => {
      setOptimisticLang(null)
      host.notifyError(err instanceof Error ? err.message : String(err))
    }
  })

  const busy = patchMutation.isPending

  if (stateQuery.isLoading && !snapshot) {
    return h(
      'div',
      { className: 'flex min-h-56 items-center justify-center p-8' },
      h(Loader, null)
    )
  }

  if (stateQuery.isError && !snapshot) {
    const errMsg = stateQuery.error instanceof Error ? stateQuery.error.message : String(stateQuery.error)
    return h(
      'div',
      { className: 'p-6' },
      h(
        ErrorState,
        { title: t.errorTitle, description: errMsg },
        h(
          'div',
          { className: 'flex justify-center' },
          h(Button, { size: 'sm', onClick: () => void stateQuery.refetch() }, t.retryLoadBtn)
        )
      )
    )
  }

  const gateStatus = snapshot?.status || {}
  const schedule = snapshot?.schedule || { source: 'manual', attempts: 6, effective_ladder: [15, 30, 60, 60, 60], total_sleep_s: 225 }
  const doctor = snapshot?.doctor || { ok: true, has_warnings: false, checks: [] }
  const counters = snapshot?.telemetry?.counters || {}

  const isArmed = Boolean(settings.enabled)
  const modeKey =
    Number(settings.custom_retries) > 0 ? 'custom' : settings.sync_hermes_retries ? 'sync' : 'manual'

  const commitPatch = (body, toastText = t.savedToast) => {
    patchMutation.mutate({ path: '/settings', body, toastText })
  }

  const handleLanguageSwitch = nextLang => {
    const normalized = normalizeLang(nextLang)
    if (normalized === lang) return
    setOptimisticLang(normalized)
    const nextCopy = COPY[normalized] || COPY.en
    patchMutation.mutate({
      path: '/settings',
      body: { language: normalized },
      toastText: nextCopy.savedToast
    })
  }

  const handleRetryModeChange = nextMode => {
    if (nextMode === modeKey) return
    if (nextMode === 'manual') {
      commitPatch({ sync_hermes_retries: false, custom_retries: 0 })
    } else if (nextMode === 'sync') {
      commitPatch({ sync_hermes_retries: true, custom_retries: 0 })
    } else {
      const parsed = Number.parseInt(customDraft, 10)
      const safeCount = Number.isFinite(parsed) && parsed >= 1 && parsed <= 25 ? parsed : 5
      commitPatch({ sync_hermes_retries: false, custom_retries: safeCount })
    }
  }

  const saveManualLadder = () => {
    const parts = ladderDraft
      .split(',')
      .map(s => s.trim())
      .filter(Boolean)
    if (parts.length === 0) {
      host.notifyError(t.manualInvalid)
      return
    }
    const nums = parts.map(item => Number(item))
    if (nums.some(n => !Number.isFinite(n) || n < 0)) {
      host.notifyError(t.manualInvalid)
      return
    }
    commitPatch({ ladder: nums.slice(0, 24), sync_hermes_retries: false, custom_retries: 0 })
  }

  const saveCustomCount = () => {
    const parsed = Number.parseInt(customDraft.trim(), 10)
    if (!Number.isFinite(parsed) || parsed < 1 || parsed > 25) {
      host.notifyError(t.customInvalid)
      return
    }
    commitPatch({ custom_retries: parsed, sync_hermes_retries: false })
  }

  const saveBudget = () => {
    const parsed = Number(budgetDraft.trim())
    if (!Number.isFinite(parsed) || parsed < 0) {
      host.notifyError(t.budgetInvalid)
      return
    }
    commitPatch({ max_wait_budget_s: parsed })
  }

  const saveBackupModels = () => {
    commitPatch({ secondary_reference_model: backupModelDraft.trim() })
  }

  const seamBadgeText =
    gateStatus.mode === 'native_hooks'
      ? `v${gateStatus.plugin_version || '1.6.0'} · ${t.badgeNative}`
      : `v${gateStatus.plugin_version || '1.6.0'} · ${t.badgeSeams((gateStatus.wrapped || []).length)}`

  const doctorBadge = !doctor.ok
    ? h(Badge, { variant: 'destructive' }, t.verdictIssues)
    : doctor.has_warnings
      ? h(Badge, { variant: 'warn' }, t.verdictWarnings)
      : h(Badge, { variant: 'success' }, t.verdictHealthy)

  const statItems = [
    { key: 'retries', label: t.statRetries, val: counters.reference_retry ?? 0 },
    { key: 'switches', label: t.statSwitches, val: counters.reference_fallback_switched ?? 0 },
    { key: 'quorum', label: t.statQuorum, val: counters.quorum_accepted ?? 0 },
    { key: 'outages', label: t.statOutages, val: counters.reference_outage ?? 0 },
    { key: 'blocked', label: t.statBlocked, val: counters.aggregator_fallback_blocked ?? 0 }
  ]

  return h(
    'div',
    { className: 'grid max-w-3xl gap-5 pb-8' },
    h(
      'div',
      { className: 'grid gap-3 rounded-xl border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary)/40 p-4' },
      h(
        'div',
        { className: 'flex flex-wrap items-center justify-between gap-3' },
        h(
          'div',
          { className: 'flex flex-wrap items-center gap-2' },
          h('h2', { className: 'text-base font-semibold text-foreground' }, 'True MoA'),
          h(Badge, { variant: isArmed ? 'success' : 'muted' }, isArmed ? t.badgeArmed : t.badgePaused),
          h(Badge, { variant: 'outline' }, seamBadgeText)
        ),
        h(
          'div',
          { className: 'flex flex-wrap items-center gap-2' },
          h(SegmentedControl, {
            sizing: 'content',
            value: lang,
            disabled: busy,
            onChange: handleLanguageSwitch,
            options: [
              { id: 'en', label: 'English' },
              { id: 'vi', label: 'Tiếng Việt' }
            ]
          }),
          h(
            Button,
            {
              size: 'sm',
              variant: 'outline',
              disabled: busy,
              onClick: () => patchMutation.mutate({ path: '/doctor', body: {}, toastText: t.doctorToast })
            },
            t.runDoctorBtn
          )
        )
      ),
      h('p', { className: 'text-xs leading-relaxed text-muted-foreground' }, t.pageSubtitle)
    ),

    h(
      SectionCard,
      { title: t.sectionCore, description: t.sectionCoreDesc },
      h(ToggleRow, {
        label: t.enabledLabel,
        description: t.enabledDesc,
        checked: isArmed,
        disabled: busy,
        onChange: on => commitPatch({ enabled: on })
      }),
      h(ToggleRow, {
        label: t.blockSelfLabel,
        description: t.blockSelfDesc,
        checked: Boolean(settings.block_aggregator_fallback),
        disabled: busy,
        onChange: on => commitPatch({ block_aggregator_fallback: on })
      }),
      h(ListRow, {
        title: t.quorumTitle,
        description: t.quorumDesc,
        action: h(
          Select,
          {
            value: String(settings.min_healthy_references ?? 0),
            onValueChange: val => commitPatch({ min_healthy_references: Number(val) })
          },
          h(SelectTrigger, { className: 'w-60' }, h(SelectValue, null)),
          h(
            SelectContent,
            null,
            h(SelectItem, { value: '0' }, t.quorumAll),
            ...[1, 2, 3, 4, 5, 6, 7, 8].map(n =>
              h(SelectItem, { key: String(n), value: String(n) }, t.quorumMin(n))
            )
          )
        )
      })
    ),

    h(
      SectionCard,
      { title: t.sectionRetry, description: t.sectionRetryDesc },
      h(ListRow, {
        title: t.retryModeTitle,
        description: t.retryModeDesc,
        action: h(SegmentedControl, {
          sizing: 'content',
          value: modeKey,
          disabled: busy,
          onChange: handleRetryModeChange,
          options: [
            { id: 'manual', label: t.modeManual },
            { id: 'sync', label: t.modeSync },
            { id: 'custom', label: t.modeCustom }
          ]
        })
      }),
      modeKey === 'manual' &&
        h(ListRow, {
          title: t.manualTitle,
          description: t.manualDesc,
          action: h(
            'div',
            { className: 'flex items-center gap-2' },
            h(Input, {
              className: 'w-48 font-mono text-xs',
              value: ladderDraft,
              placeholder: t.manualPlaceholder,
              disabled: busy,
              onChange: e => setLadderDraft(e.target.value),
              onKeyDown: e => {
                if (isSubmitEnter(e)) {
                  e.preventDefault()
                  saveManualLadder()
                }
              }
            }),
            h(
              Button,
              {
                size: 'sm',
                variant: ladderDraft.trim() !== serializedLadder.trim() ? 'default' : 'secondary',
                disabled: busy,
                onClick: saveManualLadder
              },
              t.saveBtn
            )
          )
        }),
      modeKey === 'sync' &&
        h(ListRow, {
          title: t.syncInfoTitle,
          description: t.syncInfoDesc
        }),
      modeKey === 'custom' &&
        h(ListRow, {
          title: t.customTitle,
          description: t.customDesc,
          action: h(
            'div',
            { className: 'flex items-center gap-2' },
            h(Input, {
              className: 'w-24 tabular-nums text-xs',
              value: customDraft,
              disabled: busy,
              onChange: e => setCustomDraft(e.target.value),
              onKeyDown: e => {
                if (isSubmitEnter(e)) {
                  e.preventDefault()
                  saveCustomCount()
                }
              }
            }),
            h(
              Button,
              {
                size: 'sm',
                variant: customDraft.trim() !== String(settings.custom_retries || '') ? 'default' : 'secondary',
                disabled: busy,
                onClick: saveCustomCount
              },
              t.saveBtn
            )
          )
        }),
      h(
        'div',
        { className: 'py-2.5 text-xs text-muted-foreground' },
        h('span', { className: 'font-medium text-foreground' }, `${t.activeScheduleLabel} `),
        t.activeScheduleSummary(
          schedule.attempts ?? 1,
          Array.isArray(schedule.effective_ladder) ? schedule.effective_ladder : [],
          schedule.total_sleep_s ?? 0
        )
      ),
      h(ListRow, {
        title: t.budgetTitle,
        description: t.budgetDesc,
        action: h(
          'div',
          { className: 'flex items-center gap-2' },
          h(Input, {
            className: 'w-24 tabular-nums text-xs',
            value: budgetDraft,
            disabled: busy,
            onChange: e => setBudgetDraft(e.target.value),
            onKeyDown: e => {
              if (isSubmitEnter(e)) {
                e.preventDefault()
                saveBudget()
              }
            }
          }),
          h(
            Button,
            {
              size: 'sm',
              variant: budgetDraft.trim() !== String(settings.max_wait_budget_s ?? 0) ? 'default' : 'secondary',
              disabled: busy,
              onClick: saveBudget
            },
            t.saveBtn
          )
        )
      })
    ),

    h(
      SectionCard,
      { title: t.sectionBackup, description: t.sectionBackupDesc },
      h(ToggleRow, {
        label: t.backupToggleLabel,
        description: t.backupToggleDesc,
        checked: Boolean(settings.enable_secondary_fallback),
        disabled: busy,
        onChange: on => commitPatch({ enable_secondary_fallback: on })
      }),
      Boolean(settings.enable_secondary_fallback) &&
        h(
          Fragment,
          null,
          h(ListRow, {
            title: t.backupModelsTitle,
            description: t.backupModelsDesc,
            action: h(
              'div',
              { className: 'flex items-center gap-2' },
              h(Input, {
                className: 'w-60 font-mono text-xs',
                value: backupModelDraft,
                placeholder: t.backupModelsPlaceholder,
                disabled: busy,
                onChange: e => setBackupModelDraft(e.target.value),
                onKeyDown: e => {
                  if (isSubmitEnter(e)) {
                    e.preventDefault()
                    saveBackupModels()
                  }
                }
              }),
              h(
                Button,
                {
                  size: 'sm',
                  variant:
                    backupModelDraft.trim() !== String(settings.secondary_reference_model || '').trim()
                      ? 'default'
                      : 'secondary',
                  disabled: busy,
                  onClick: saveBackupModels
                },
                t.saveBtn
              )
            )
          }),
          h(ListRow, {
            title: t.backupTriggerTitle,
            description: t.backupTriggerDesc,
            action: h(
              Select,
              {
                value: String(settings.fallback_trigger_attempts ?? 2),
                onValueChange: val => commitPatch({ fallback_trigger_attempts: Number(val) })
              },
              h(SelectTrigger, { className: 'w-52' }, h(SelectValue, null)),
              h(
                SelectContent,
                null,
                ...[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(n =>
                  h(SelectItem, { key: String(n), value: String(n) }, t.backupTriggerOption(n))
                )
              )
            )
          })
        )
    ),

    h(
      SectionCard,
      {
        title: t.sectionHealth,
        description: t.sectionHealthDesc,
        badge: h(
          'div',
          { className: 'flex items-center gap-2' },
          doctorBadge,
          h(
            Button,
            {
              size: 'sm',
              variant: 'ghost',
              disabled: busy,
              onClick: () => patchMutation.mutate({ path: '/reverify', body: {}, toastText: t.reverifyToast })
            },
            t.reverifyBtn
          )
        )
      },
      h(
        'div',
        { className: 'grid gap-2 py-2.5' },
        ...(Array.isArray(doctor.checks) ? doctor.checks : []).map(chk => {
          const tone = chk.level === 'ok' ? 'good' : chk.level === 'warn' ? 'warn' : 'bad'
          const label = t.checkLabels[chk.id] || chk.name
          return h(
            'div',
            {
              key: chk.id,
              className: 'flex items-start justify-between gap-3 rounded-lg bg-background/60 px-3 py-2 text-xs'
            },
            h(
              'div',
              { className: 'flex items-center gap-2 font-medium text-foreground' },
              h(StatusDot, { tone }),
              h('span', null, label)
            ),
            h('span', { className: 'text-right text-muted-foreground' }, chk.detail)
          )
        })
      ),
      h(
        'div',
        { className: 'grid grid-cols-2 gap-2 py-3 sm:grid-cols-5' },
        ...statItems.map(item =>
          h(
            'div',
            {
              key: item.key,
              className: 'rounded-lg border border-(--ui-stroke-secondary) bg-background/60 px-2.5 py-2 text-center'
            },
            h('div', { className: 'tabular-nums text-sm font-semibold text-foreground' }, String(item.val)),
            h('div', { className: 'text-[0.6875rem] text-muted-foreground' }, item.label)
          )
        )
      ),
      h(Separator, null),
      h(ToggleRow, {
        label: t.statusbarToggleLabel,
        description: t.statusbarToggleDesc,
        checked: Boolean(showChip),
        onChange: next => {
          ctx.storage.set(STORAGE_STATUSBAR_KEY, Boolean(next))
          $showStatusbarChip.set(Boolean(next))
        }
      })
    )
  )
}

function TrueMoAStatusbarChip({ ctx }) {
  const showChip = useValue($showStatusbarChip)
  const activeProfile = useValue(host.state.profile)
  const queryKey = useMemo(() => [...QUERY_ROOT, activeProfile || 'default'], [activeProfile])

  const stateQuery = useQuery({
    queryKey,
    queryFn: () => ctx.rest('/state'),
    enabled: Boolean(showChip),
    staleTime: 60_000
  })

  if (!showChip || !stateQuery.data) return null

  const settings = stateQuery.data.settings || {}
  const doctor = stateQuery.data.doctor || { ok: true, has_warnings: false }
  const lang = normalizeLang(settings.language)
  const t = COPY[lang] || COPY.en
  const armed = Boolean(settings.enabled)
  const tone = !doctor.ok ? 'bad' : !armed ? 'muted' : doctor.has_warnings ? 'warn' : 'good'

  return h(
    Tip,
    { label: armed ? t.chipTipArmed : t.chipTipPaused },
    h(
      'button',
      {
        type: 'button',
        onClick: () => host.navigate(pluginSettingsHref(`${PLUGIN_ID}:${SETTINGS_PAGE_ID}`)),
        className: cn(
          'inline-flex items-center gap-1.5 rounded px-1.5 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground'
        )
      },
      h(StatusDot, { tone }),
      h('span', { className: 'tabular-nums' }, armed ? t.chipArmed : t.chipPaused)
    )
  )
}

export default {
  id: PLUGIN_ID,
  name: 'True MoA',
  description: 'Bilingual settings and offline health check for Mixture of Agents strict protection.',
  register(ctx) {
    $showStatusbarChip.set(Boolean(ctx.storage.get(STORAGE_STATUSBAR_KEY, true)))

    const syncCaches = () => {
      queryClient.invalidateQueries({ queryKey: QUERY_ROOT })
    }

    ctx.onEvent(`plugin.${PLUGIN_ID}.updated`, syncCaches)
    ctx.onEvent(`plugin.${PLUGIN_ID}.settings.changed`, syncCaches)

    ctx.registerSettingsPage?.({
      id: SETTINGS_PAGE_ID,
      title: 'True MoA',
      icon: 'shield',
      order: 12,
      render: () => h(TrueMoASettingsPage, { ctx })
    })

    ctx.register({
      id: 'statusbar-chip',
      area: STATUSBAR_AREAS.right,
      order: 46,
      render: () => h(TrueMoAStatusbarChip, { ctx })
    })

    ctx.registerMany([
      {
        id: 'open-settings',
        area: PALETTE_AREA,
        data: {
          id: 'open-settings',
          label: COPY.en.paletteOpen,
          icon: icons.Shield || icons.Activity,
          keywords: ['moa', 'true-moa', 'advisor', 'doctor', 'language', 'vietnamese', 'tieng viet'],
          run: () => host.navigate(pluginSettingsHref(`${PLUGIN_ID}:${SETTINGS_PAGE_ID}`))
        }
      },
      {
        id: 'toggle-protection',
        area: PALETTE_AREA,
        data: {
          id: 'toggle-protection',
          label: COPY.en.paletteToggle,
          icon: icons.Shield || icons.RefreshCw,
          keywords: ['moa', 'true-moa', 'strict', 'toggle', 'arm', 'pause'],
          run: () => {
            ctx
              .rest('/state')
              .then(cur => {
                const nextEnabled = !cur?.settings?.enabled
                const lang = normalizeLang(cur?.settings?.language)
                return ctx.rest('/settings', { method: 'POST', body: { enabled: nextEnabled } }).then(() => {
                  syncCaches()
                  host.notify((COPY[lang] || COPY.en).savedToast)
                })
              })
              .catch(err => host.notifyError(err instanceof Error ? err.message : String(err)))
          }
        }
      }
    ])
  }
}

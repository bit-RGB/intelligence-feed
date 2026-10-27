import { Button, Codicon, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, EmptyState, ErrorState, Input, ROUTES_AREA, ScrollArea, SIDEBAR_NAV_AREA, Skeleton, Switch, host, useMutation, useQuery, useQueryClient, useValue } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'intelligence-feed'
const muted = 'var(--ui-text-secondary)'
// 卡片边框保留柔和主题色；控件使用 Desktop 原生背景与选中态。
const softAccent = 'color-mix(in srgb, var(--ui-accent) 50%, transparent)'
const softButtonStyle = { background: 'var(--ui-bg-quaternary)', color: 'var(--ui-text-primary)' }
const selectedControlStyle = { background: 'var(--ui-control-active-background)', color: 'var(--ui-text-primary)', fontWeight: 600 }
const DOMAIN_ICONS = {
  all: 'home',
  semiconductor: 'circuit-board',
  ai: 'hubot',
  automotive: 'dashboard',
  music: 'music',
  weibo: 'flame',
  favorites: 'star-empty'
}
const SOURCE_GROUPS = [
  ['semiconductor', '半导体'],
  ['ai', 'AI'],
  ['automotive', '汽车'],
  ['music', '音乐'],
  ['weibo', '热搜']
]

// 关键词表：按领域整理的常见标签词库，长词在前，优先命中更具体的词。
const KEYWORDS = [
  // 半导体
  '中芯国际', '长江存储', '北方华创', '中微公司', '兆易创新', '韦尔股份', '寒武纪', '摩尔线程',
  '长鑫', '华虹', '海光', '燧原', '壁仞', '华为', '昇腾', '麒麟', '鸿蒙',
  'HBM', 'DRAM', 'NAND', '存储', '光刻', 'EUV', '先进封装', 'Chiplet', '芯粒', '晶圆', 'EDA',
  'RISC-V', 'GPU', 'CPU', 'MCU', 'AI芯片', '碳化硅', 'SiC', '氮化镓', 'GaN',
  '台积电', '三星', '英特尔', '高通', '英伟达', 'AMD', 'Arm', 'ASML',
  '涨价', 'IPO', '并购', '国产替代', '2nm', '3nm', '制程', '封装',
  '晶圆代工', '封测', '光模块', 'CPO', '硅光', '功率半导体', 'IGBT', '模拟芯片', '射频',
  '传感器', 'CIS', '光刻胶', '刻蚀', '薄膜沉积', '良率', '产能',
  // AI
  '大模型', '智能体', 'Agent', '多模态', '具身智能', 'DeepSeek', 'OpenAI', 'GPT', 'Sora',
  '通义', '文心', '豆包', 'Kimi', '千问', '混元', '推理模型', '开源模型', '微调', '算力',
  'AIGC', '生成式AI', 'Copilot', 'Claude', 'Gemini', 'Llama', 'Anthropic', 'xAI', 'Grok',
  'AI编程', 'AI办公', 'AI视频', '数字人', '人形机器人', '机器人', '世界模型', '强化学习',
  '蒸馏', '量化', '边缘AI', '端侧AI', 'AI眼镜', 'AI手机', 'AI PC', 'RAG',
  // 汽车
  '新能源', '电动车', '智驾', '自动驾驶', '激光雷达', '固态电池', '超充', '换电',
  '蔚来', '小鹏', '理想', '小米汽车', '比亚迪', '特斯拉', '极氪', '鸿蒙智行',
  '问界', '阿维塔', '零跑', '哪吒', '奇瑞', '吉利', '长城', '混动', '增程', '纯电',
  '动力电池', '800V', '智能座舱', 'OTA', 'NOA', 'L3', 'L4', 'Robotaxi', '召回', '交付量', '销量',
  // 音乐
  '新专辑', '新歌', '单曲', 'MV', '演唱会', '巡演', '音乐节', '金曲奖', '首发', '上线',
  '原声', 'OST', '乐队', '歌手', '制作人', '唱片', '作曲', '作词', '编曲', '翻唱',
  'Livehouse', '世界巡演', '音源', '播放量', '音乐奖', '摇滚', '民谣', '说唱', '嘻哈', '国风'
]

function keywordsFor(headline) {
  if (!headline) return []
  const hits = []
  for (const word of KEYWORDS) {
    if (headline.includes(word) && !hits.some(hit => hit.includes(word) || word.includes(hit))) hits.push(word)
    if (hits.length === 3) break
  }
  return hits
}

// 2026-09-14T16:44:12+08:00 → 2026-09-14 16:44:12（北京时间数字，不带时区后缀）
function formatTime(iso) {
  return (iso ?? '').replace('T', ' ').replace(/([+-]\d{2}:\d{2}|Z)$/, '')
}

function relativeTime(iso) {
  const then = new Date(iso).getTime()
  if (!then) return ''
  const minutes = Math.max(0, Math.floor((Date.now() - then) / 60000))
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes}分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}小时前`
  return `${Math.floor(hours / 24)}天前`
}

export default {
  id: ID,
  name: '资讯',
  defaultEnabled: false,
  register(ctx) {
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/intelligence-feed' }, render: () => jsx(FeedPage, { ctx }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 70, data: { path: '/intelligence-feed', label: '资讯', codicon: 'rss' } }
    ])
  }
}

function FeedPage({ ctx }) {
  const profile = useValue(host.state.profile)
  const queryClient = useQueryClient()
  const [domain, setDomain] = useState('all')
  const [readFilter, setReadFilter] = useState('all')
  const [draftSearch, setDraftSearch] = useState('')
  const [searchText, setSearchText] = useState('')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [showTags, setShowTags] = useState(() => ctx.storage.get('settings.showTags', true) !== false)
  useEffect(() => {
    const timer = setTimeout(() => setSearchText(draftSearch), 250)
    return () => clearTimeout(timer)
  }, [draftSearch])
  const updateMutation = useMutation({
    mutationFn: () => ctx.rest('/updates', { method: 'POST', timeoutMs: 160_000 }),
    onSuccess: result => {
      queryClient.invalidateQueries({ queryKey: [ID] })
      if (result.status === 'failed') host.notify({ kind: 'error', message: '本次更新失败，已保留原有资讯' })
      else if (result.status === 'partial') host.notify({ kind: 'warning', message: '部分来源更新失败，已保存成功结果' })
      else host.notify({ kind: 'success', message: '资讯更新完成' })
    },
    onError: () => host.notify({ kind: 'error', message: '更新失败或正在更新，请稍后重试' })
  })
  const counts = useQuery({
    queryKey: [ID, 'counts', profile],
    queryFn: () => ctx.rest('/counts?' + new URLSearchParams({ profile })),
    refetchInterval: 60_000
  })
  const sourceSettings = useQuery({
    queryKey: [ID, 'sources'],
    queryFn: () => ctx.rest('/sources')
  })
  const sourceMutation = useMutation({
    mutationFn: ({ id, value }) => ctx.rest(`/sources/${id}`, { method: 'PATCH', body: { value } }),
    onSuccess: () => Promise.all([
      queryClient.invalidateQueries({ queryKey: [ID, 'sources'] }),
      queryClient.invalidateQueries({ queryKey: [ID, 'counts', profile] }),
      queryClient.invalidateQueries({ queryKey: [ID, 'events', profile] })
    ]),
    onError: () => host.notify({ kind: 'error', message: '信息源设置保存失败，请重试' })
  })
  // React's key resets cursor/pages atomically, including on profile switches.
  const filterKey = JSON.stringify([profile, domain, readFilter, searchText])
  function domainButton(value, label) {
    const count = value === 'favorites' ? counts.data?.favorites_unread : counts.data?.[value]?.unread
    const countLabel = count > 0 ? ` (${Math.min(count, 999)})` : ''
    const selected = domain === value
    return jsxs(Button, {
      variant: 'ghost', size: 'sm',
      'aria-pressed': selected,
      onClick: () => setDomain(value),
      style: { width: 120, minHeight: 28, alignSelf: 'flex-start', justifyContent: 'flex-start', gap: 8, ...(selected ? selectedControlStyle : undefined) },
      children: [
        jsx(Codicon, { name: DOMAIN_ICONS[value], 'aria-hidden': true, style: { fontSize: 14, flexShrink: 0 } }),
        jsx('span', { children: label }),
        countLabel && jsx('span', { style: { marginLeft: 'auto', fontSize: 11, fontWeight: 400, color: muted }, children: countLabel })
      ]
    }, value)
  }
  return jsxs('div', {
    style: { display: 'flex', height: '100%', minHeight: 0, color: 'var(--ui-text-primary)' },
    children: [
      jsxs('nav', {
        'aria-label': '资讯分类',
        style: { display: 'flex', flexDirection: 'column', width: 144, flexShrink: 0, padding: 12, gap: 4, borderRight: '1px solid var(--ui-stroke-secondary)' },
        children: [domainButton('all', '全部'), domainButton('semiconductor', '半导体'), domainButton('ai', 'AI'), domainButton('automotive', '汽车'), domainButton('music', '音乐'), domainButton('weibo', '热搜'),
          counts.isError && jsx(Button, { onClick: () => counts.refetch(), style: softButtonStyle, children: '重试计数' }),
          jsx('div', { style: { marginTop: 'auto', borderTop: '1px solid var(--ui-stroke-secondary)', paddingTop: 12 }, children: domainButton('favorites', '收藏夹') })]
      }),
      jsxs('main', {
        style: { display: 'flex', flexDirection: 'column', flex: 1, minWidth: 0, minHeight: 0, padding: 20, gap: 12 },
        children: [
          jsx(Input, { 'aria-label': '搜索资讯', placeholder: '搜索资讯', value: draftSearch, maxLength: 100, onChange: e => setDraftSearch(e.target.value) }),
          jsxs('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, borderBottom: '1px solid var(--ui-stroke-secondary)', paddingBottom: 12 }, children: [
            jsx('div', { role: 'group', 'aria-label': '阅读状态', style: { display: 'flex', gap: 4 }, children:
              [['all', '全部'], ['unread', '未读'], ['read', '已读']].map(([value, label]) => jsx(Button, {
                variant: 'ghost', size: 'sm',
                'aria-pressed': readFilter === value, onClick: () => setReadFilter(value),
                style: readFilter === value ? selectedControlStyle : undefined,
                children: label
              }, value)) }),
            jsxs(Button, { variant: 'ghost', size: 'sm', 'aria-label': '资讯设置', onClick: () => setSettingsOpen(true), children: [
              jsx(Codicon, { name: 'gear' }),
              '设置'
            ] })
          ] }),
          jsx(Dialog, { open: settingsOpen, onOpenChange: setSettingsOpen, children: jsxs(DialogContent, { children: [
            jsxs(DialogHeader, { children: [
              jsx(DialogTitle, { children: '资讯设置' }),
              jsx(DialogDescription, { children: '控制资讯卡片中显示的辅助信息。' })
            ] }),
            jsxs('label', { style: { display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer' }, children: [
              jsx(Switch, { 'aria-label': '显示关键词标签', checked: showTags, size: 'xs', onCheckedChange: value => {
                const next = Boolean(value)
                setShowTags(next)
                ctx.storage.set('settings.showTags', next)
              } }),
              jsxs('span', { style: { display: 'flex', flexDirection: 'column', gap: 2 }, children: [
                jsx('span', { children: '显示关键词标签' }),
                jsx('span', { style: { color: muted, fontSize: 12 }, children: '关闭后隐藏资讯标题下方的关键词标签。' })
              ] })
            ] }),
            jsxs('div', { style: { display: 'flex', flexDirection: 'column', gap: 8, paddingTop: 12, borderTop: '1px solid var(--ui-stroke-secondary)' }, children: [
              jsx('div', { style: { fontWeight: 600 }, children: '信息源' }),
              jsx('div', { style: { color: muted, fontSize: 12 }, children: '关闭后停止采集并从普通列表隐藏，历史收藏仍会保留。' }),
              sourceSettings.isLoading && jsx(Skeleton, { style: { height: 96 }, 'aria-label': '加载信息源中' }),
              sourceSettings.isError && jsxs('div', { children: [
                jsx(ErrorState, { title: '信息源加载失败', description: '无法读取当前信息源设置。' }),
                jsx(Button, { onClick: () => sourceSettings.refetch(), style: softButtonStyle, children: '重试' })
              ] }),
              !sourceSettings.isLoading && !sourceSettings.isError && jsx(ScrollArea, {
                style: { maxHeight: 360 },
                children: jsx('div', { style: { display: 'flex', flexDirection: 'column', gap: 12, paddingRight: 8 }, children:
                  SOURCE_GROUPS.map(([value, label]) => {
                    const domainSources = sourceSettings.data?.filter(source => source.domain === value) ?? []
                    if (!domainSources.length) return null
                    return jsxs('section', { style: { display: 'flex', flexDirection: 'column', gap: 6 }, children: [
                      jsx('div', { style: { color: muted, fontSize: 12, fontWeight: 600 }, children: label }),
                      domainSources.map(source => jsxs('label', {
                        style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, cursor: sourceMutation.isPending ? 'default' : 'pointer' },
                        children: [
                          jsx('span', { children: source.name }),
                          jsx(Switch, { 'aria-label': `${source.enabled ? '停用' : '启用'}${source.name}`, checked: source.enabled, size: 'xs', disabled: sourceMutation.isPending, onCheckedChange: checked => {
                            const value = Boolean(checked)
                            sourceMutation.mutate({ id: source.id, value })
                          } })
                        ]
                      }, source.id))
                    ] }, value)
                  })
                })
              })
            ] }),
            jsx(DialogFooter, { children: jsx(Button, { onClick: () => setSettingsOpen(false), style: softButtonStyle, children: '完成' }) })
          ] }) }),
          jsx(FeedResults, { ctx, profile, domain, readFilter, searchText, showTags, updateMutation }, filterKey)
        ]
      })
    ]
  })
}

function FeedResults({ ctx, profile, domain, readFilter, searchText, showTags, updateMutation }) {
  const queryClient = useQueryClient()
  const [cursor, setCursor] = useState(null)
  const [pages, setPages] = useState([])
  const [loadingMore, setLoadingMore] = useState(false)
  const [previewUrl, setPreviewUrl] = useState(null)
  const paginationGeneration = useRef(0)
  const events = useQuery({
    queryKey: [ID, 'events', profile, domain, readFilter, searchText, null],
    queryFn: () => ctx.rest('/events?' + new URLSearchParams({
      profile, domain, read: readFilter, q: searchText, limit: '50'
    })),
    staleTime: 30_000,
    refetchInterval: 60_000
  })
  const latest = useQuery({
    queryKey: [ID, 'runs/latest', profile],
    queryFn: () => ctx.rest('/runs/latest'),
    refetchInterval: 60_000
  })
  useEffect(() => {
    if (!events.data) return
    paginationGeneration.current += 1
    setPages([])
    setCursor(events.data.next_cursor)
  }, [events.dataUpdatedAt])
  async function refresh() {
    paginationGeneration.current += 1
    setPages([])
    setCursor(null)
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: [ID, 'events', profile] }),
      queryClient.invalidateQueries({ queryKey: [ID, 'counts', profile] }),
      queryClient.invalidateQueries({ queryKey: [ID, 'runs/latest', profile] })
    ])
  }
  const favoriteMutation = useMutation({
    mutationFn: ({ id, value }) => ctx.rest(`/events/${id}/favorite`, { method: 'PATCH', body: { profile, value } }),
    onSuccess: refresh,
    onError: () => host.notify({ kind: 'error', message: '收藏操作失败，请重试' })
  })
  function validDestination(value) {
    try {
      const parsed = new URL(value)
      return parsed.protocol === 'https:' && Boolean(parsed.hostname)
    } catch {
      return false
    }
  }
  async function openEvent(event) {
    if (!validDestination(event.url)) {
      host.notify({ kind: 'warning', message: '该消息的原始链接不可用' })
      return
    }
    try {
      await ctx.rest(`/events/${event.id}/read`, { method: 'PATCH', body: { profile, value: true } })
      await refresh()
      setPreviewUrl(event.url)
    } catch {
      host.notify({ kind: 'error', message: '打开消息失败，请重试' })
    }
  }
  function favoriteClick(domEvent, event) {
    domEvent.stopPropagation()
    favoriteMutation.mutate({ id: event.id, value: !event.favorite })
  }
  async function loadMore() {
    if (events.isFetching || loadingMore || !cursor) return
    const generation = paginationGeneration.current
    setLoadingMore(true)
    try {
      const page = await queryClient.fetchQuery({
        queryKey: [ID, 'events', profile, domain, readFilter, searchText, cursor],
        queryFn: () => ctx.rest('/events?' + new URLSearchParams({
          profile, domain, read: readFilter, q: searchText, limit: '50', cursor
        }))
      })
      if (generation !== paginationGeneration.current) return
      setPages(previous => [...previous, page])
      setCursor(page.next_cursor)
    } catch {
      host.notify({ kind: 'error', message: '加载更多失败，请重试' })
    } finally {
      setLoadingMore(false)
    }
  }
  // The polled first page is authoritative; when it changes, the effect above
  // clears accumulated pages so stale cursor snapshots are never mixed in.
  const items = [...new Map([...(events.data?.items ?? []), ...pages.flatMap(page => page.items)]
    .map(event => [event.id, event])).values()]
  const emptyMessage = searchText ? '没有匹配的消息'
    : domain === 'favorites' && readFilter === 'unread' ? '收藏夹中暂无未读消息'
    : domain === 'favorites' && readFilter === 'read' ? '收藏夹中暂无已读消息'
    : domain === 'favorites' ? '收藏夹为空'
    : readFilter === 'unread' ? '暂无未读消息' : readFilter === 'read' ? '暂无已读消息' : '近 30 天暂无消息'
  return jsxs('div', {
    style: { display: 'flex', flex: 1, minHeight: 0, gap: 12 },
    children: [
      jsxs('div', {
        style: { display: 'flex', flexDirection: 'column', flex: previewUrl ? 2 : 1, minWidth: 0, minHeight: 0, gap: 12 },
        children: [
      jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 12 }, children: [
        jsx('span', { style: { color: muted }, children: `更新时间：${latest.data?.run?.finished_at ?? latest.data?.run?.started_at ?? '尚未更新'}` }),
        latest.isError && jsx(Button, { onClick: () => latest.refetch(), style: softButtonStyle, children: '重试更新时间' }),
        jsx(Button, { disabled: updateMutation.isPending, onClick: () => updateMutation.mutate(), style: softButtonStyle, children: updateMutation.isPending ? '更新中…' : '手动更新' })
      ] }),
      events.isLoading && !items.length && jsx(Skeleton, { style: { height: 120 }, 'aria-label': '加载资讯中', role: 'status', 'aria-live': 'polite' }),
      events.isError && jsxs('div', { children: [
        jsx(ErrorState, { title: '加载失败', description: '无法加载资讯，请重试。' }),
        jsx(Button, { onClick: () => events.refetch(), style: softButtonStyle, children: '重试' })
      ] }),
      !events.isLoading && !events.isError && !items.length && jsx('div', { role: 'status', 'aria-live': 'polite', children: jsx(EmptyState, { title: emptyMessage }) }),
      jsx(ScrollArea, { style: { flex: 1, minHeight: 0 }, children: jsx('ul', {
        // paddingRight 给滚动条让位，避免与卡片右侧的星标重叠。
        style: { listStyle: 'none', padding: 0, paddingRight: 12, margin: 0, display: 'flex', flexDirection: 'column', gap: 8 },
        children: items.map(event => jsxs('li', {
          style: { display: 'flex', alignItems: 'center', gap: 8 },
          children: [
            jsx('div', {
              style: { flex: 1, minWidth: 0, padding: '6px 10px', border: `1px solid ${softAccent}`, borderRadius: 8 },
              children: jsx(Button, {
                variant: 'ghost', size: 'inline',
                onClick: () => openEvent(event),
                style: { width: '100%', minWidth: 0, textAlign: 'left', height: 'auto', whiteSpace: 'normal', justifyContent: 'flex-start', color: event.read ? muted : 'var(--ui-text-primary)', fontWeight: event.read ? 400 : 600 },
                children: jsxs('span', { style: { display: 'block', width: '100%' }, children: [
                  // 圆点常显：未读=柔和主题蓝（与按钮统一），已读=灰。
                  jsx('span', { 'aria-label': event.read ? '已读' : '未读', style: { color: event.read ? muted : softAccent }, children: '● ' }),
                  event.translated_headline || event.headline,
                  event.translated_headline && jsx('span', {
                    style: { display: 'block', color: muted, fontSize: 12, marginTop: 2, fontWeight: 400 },
                    children: event.headline
                  }),
                  jsxs('span', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, marginTop: 2, fontWeight: 400 }, children: [
                    showTags && jsx('span', { style: { display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }, children:
                      keywordsFor(event.translated_headline || event.headline).map(word => jsx('span', {
                        style: { background: 'color-mix(in srgb, var(--ui-accent) 14%, transparent)', color: 'var(--ui-text-primary)', borderRadius: 999, padding: '1px 8px', fontSize: 12 },
                        children: word
                      }, word)) }),
                    jsxs('span', { style: { color: muted, whiteSpace: 'nowrap', marginLeft: 'auto' }, children: (() => {
                      const iso = event.published_at ?? event.first_seen_at ?? ''
                      const relative = relativeTime(iso)
                      const hot = domain === 'weibo' && event.summary ? event.summary
                        : event.evidence_count > 1 ? `×${event.evidence_count}` : '-'
                      return [
                        jsx('span', { key: 'hot', 'aria-label': `热度：${event.evidence_count > 1 ? `${event.evidence_count} 个来源报道` : '无'}`, title: event.evidence_count > 1 ? `${event.evidence_count} 个来源报道` : '单一来源', children: `${hot} · ` }),
                        `${event.source_name ?? ''} · ${formatTime(iso)}${relative ? ` · ${relative}` : ''}`
                      ]
                    })() })
                  ] })
                ] })
              })
            }),
            jsx(Button, { variant: 'ghost', size: 'icon-xs', 'aria-label': `${event.favorite ? '取消收藏' : '收藏'}：${event.headline}`, 'aria-pressed': Boolean(event.favorite),
              disabled: favoriteMutation.isPending, onClick: e => favoriteClick(e, event),
              children: jsx(Codicon, { name: event.favorite ? 'star-full' : 'star-empty' }) })
          ]
        }, event.id))
      }) }),
          loadingMore && jsx(Skeleton, { style: { height: 32 }, 'aria-label': '加载更多中', role: 'status' }),
          !events.isError && cursor && jsx(Button, { disabled: events.isFetching || loadingMore, onClick: loadMore, style: softButtonStyle, children: '加载更多' })
        ]
      }),
      previewUrl && jsxs('div', {
        style: { display: 'flex', flexDirection: 'column', flex: 3, minWidth: 0, minHeight: 0, border: '1px solid var(--ui-stroke-secondary)', borderRadius: 8, overflow: 'hidden' },
        children: [
          jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 8, padding: '4px 8px', borderBottom: '1px solid var(--ui-stroke-secondary)' }, children: [
            jsx('span', { style: { flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: muted, fontSize: 12 }, children: (() => { try { return new URL(previewUrl).hostname } catch { return previewUrl } })() }),
            jsx('span', { style: { color: muted, fontSize: 12, whiteSpace: 'nowrap' }, children: 'Ctrl+滚轮缩放' }),
            jsx(Button, { variant: 'ghost', size: 'icon-xs', 'aria-label': '在外部浏览器打开', onClick: () => ctx.os.openExternal(previewUrl), children: jsx(Codicon, { name: 'link-external' }) }),
            jsx(Button, { variant: 'ghost', size: 'icon-xs', 'aria-label': '关闭预览', onClick: () => setPreviewUrl(null), children: jsx(Codicon, { name: 'close' }) })
          ] }),
          jsx('div', {
            style: { position: 'relative', flex: 1, minWidth: 0, minHeight: 0, overflow: 'hidden' },
            children: jsx(WebView, { url: previewUrl })
          })
        ]
      })
    ]
  })
}

// Electron webview：独立浏览器进程渲染原文，不受 iframe 的 X-Frame-Options 限制。
// src 用 setAttribute 维护——React 不会把 src 变更可靠地映射到 webview 导航。
function WebView({ url }) {
  const ref = useRef(null)
  useEffect(() => {
    const el = ref.current
    if (el && el.getAttribute('src') !== url) el.setAttribute('src', url)
  }, [url])
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const installZoom = () => {
      // webview 内的滚轮事件不会冒泡到插件页面，所以在访客页面中安装监听器。
      // 使用 CSS zoom 可同时缩小固定宽度网站，让原本横向被截断的内容进入视口。
      void el.executeJavaScript(`(() => {
        if (window.__intelligenceFeedZoomInstalled) return;
        window.__intelligenceFeedZoomInstalled = true;
        const key = '__intelligenceFeedZoom';
        let zoom = Number(sessionStorage.getItem(key) || '1');
        const clamp = value => Math.min(2, Math.max(0.4, Math.round(value * 10) / 10));
        const apply = () => {
          zoom = clamp(zoom);
          document.documentElement.style.zoom = String(zoom);
          sessionStorage.setItem(key, String(zoom));
        };
        apply();
        window.addEventListener('wheel', event => {
          if (!event.ctrlKey) return;
          event.preventDefault();
          event.stopPropagation();
          zoom = clamp(zoom + (event.deltaY < 0 ? 0.1 : -0.1));
          apply();
        }, { capture: true, passive: false });
        window.addEventListener('keydown', event => {
          if (event.ctrlKey && event.key === '0') {
            event.preventDefault();
            zoom = 1;
            apply();
          }
        }, { capture: true });
      })()`).catch(() => {})
    }
    el.addEventListener('dom-ready', installZoom)
    return () => el.removeEventListener('dom-ready', installZoom)
  }, [])
  return jsx('webview', {
    ref,
    partition: 'persist:intelligence-feed',
    webpreferences: 'contextIsolation=yes,nodeIntegration=no,sandbox=yes',
    // 绝对铺满工具栏下方的剩余空间。不能在父级纵向 flex 中直接使用
    // height:100%，否则会再叠加工具栏高度，导致网页底部被 overflow 裁切。
    style: { position: 'absolute', inset: 0, width: '100%', height: '100%', border: 'none', display: 'flex' }
  })
}

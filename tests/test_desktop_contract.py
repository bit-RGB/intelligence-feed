"""Offline static contracts for the build-free Hermes desktop entry point."""
from pathlib import Path
import re

P = Path(__file__).parents[1] / "desktop" / "plugin.js"


def contract(check):
    def test():
        check(P.read_text(encoding="utf-8"))
    test.__name__ = check.__name__
    return test


@contract
def test_registration(source):
    for token in ("const ID = 'intelligence-feed'", "id: ID", "defaultEnabled: false",
                  "area: ROUTES_AREA", "area: SIDEBAR_NAV_AREA",
                  "path: '/intelligence-feed'", "name: '讯集'", "label: '讯集'", "codicon: 'rss'"):
        assert token in source


@contract
def test_build_free_imports_and_theme(source):
    imports = re.findall(r"import\s+.*?\s+from\s+['\"]([^'\"]+)", source, re.S)
    assert set(imports) == {'@hermes/plugin-sdk', 'react', 'react/jsx-runtime'}
    assert not re.search(r'<(?:[A-Za-z][\w.]*\b|/|>)', source)
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b|rgba?\s*\(|[\'"](?:black|white)[\'"]', source)
    # Backgrounds are allowed only via theme tokens (card fill, tint capsules).
    for value in re.findall(r"background:\s*'([^']+)'", source):
        assert 'var(--ui-' in value
    assert 'var(--ui-' in source
    sdk = re.search(r'import\s*{([^}]+)}\s*from\s*[\'"]@hermes/plugin-sdk', source).group(1)
    imported = set(re.findall(r'\b\w+\b', sdk))
    local = set(re.findall(r'function\s+([A-Z]\w*)', source))
    referenced = set(re.findall(r'jsx[s]?\(\s*([A-Z]\w*)', source))
    assert referenced - local <= imported


@contract
def test_filters_debounce_and_query_keys(source):
    for state in ('domain', 'readFilter', 'draftSearch', 'searchText', 'cursor', 'pages'):
        assert re.search(r'const \[' + state + r',\s*set\w+\] = useState\(', source)
    assert 'useValue(host.state.profile)' in source
    assert 'setTimeout(() => setSearchText(draftSearch), 250)' in source
    assert 'clearTimeout(timer)' in source
    assert '[draftSearch]' in source
    assert "[ID, 'events', profile, domain, readFilter, searchText, null]" in source
    assert "[ID, 'events', profile, domain, readFilter, searchText, cursor]" in source
    assert 'JSON.stringify([profile, domain, readFilter, searchText])' in source
    assert '}, filterKey)' in source  # remount results, resetting both pagination states
    for token in ('read: readFilter', 'q: searchText', "limit: '50'", 'cursor'):
        assert token in source
    assert '[events.dataUpdatedAt]' in source
    assert 'queryClient.fetchQuery' in source
    assert 'const paginationGeneration = useRef(0)' in source
    assert 'const generation = paginationGeneration.current' in source
    assert 'if (generation !== paginationGeneration.current) return' in source


@contract
def test_settings_dialog_can_persistently_hide_keyword_tags(source):
    for component in ('Switch', 'Dialog', 'DialogContent', 'DialogDescription',
                      'DialogFooter', 'DialogHeader', 'DialogTitle'):
        assert component in source
    assert 'Checkbox' not in source
    assert "const [settingsOpen, setSettingsOpen] = useState(false)" in source
    assert "ctx.storage.get('settings.showTags', true)" in source
    assert "ctx.storage.set('settings.showTags', next)" in source
    assert "aria-label': '讯集设置'" in source
    assert "children: '讯集设置'" in source
    assert "children: '显示关键词标签'" in source
    assert "showTags && jsx('span'" in source
    assert "jsx(Switch, { 'aria-label': '显示关键词标签', checked: showTags" in source
    assert "style: { color: muted, whiteSpace: 'nowrap', marginLeft: 'auto' }" in source
    assert "jsx(FeedResults, { ctx, profile, domain, readFilter, searchText, showTags, updateMutation, previewZoom, onPreviewZoomChange: changePreviewZoom }" in source
    assert "ctx.storage.get('settings.previewZoom', DEFAULT_PREVIEW_ZOOM)" in source
    assert "ctx.storage.set('settings.previewZoom', next)" in source
    assert "jsx(PreviewZoomControls, { zoom: previewZoom, onZoomChange: changePreviewZoom })" in source


@contract
def test_settings_dialog_lists_and_toggles_sources_by_domain(source):
    assert "const SOURCE_GROUPS = [" in source
    for domain, label in (
        ("semiconductor", "半导体"), ("ai", "AI"), ("automotive", "汽车"),
        ("music", "音乐"), ("weibo", "热搜"),
    ):
        assert f"['{domain}', '{label}']" in source
    assert "queryKey: [ID, 'sources']" in source
    assert "ctx.rest('/sources')" in source
    assert "ctx.rest(`/sources/${id}`, { method: 'PATCH', body: { value } })" in source
    assert "children: '信息源'" in source
    assert "sourceSettings.data?.filter(source => source.domain === value)" in source
    assert "jsx(Switch, { 'aria-label': `${source.enabled ? '停用' : '启用'}${source.name}`" in source
    assert "checked: source.enabled" in source
    assert "sourceMutation.mutate({ id: source.id, value })" in source
    assert "queryClient.invalidateQueries({ queryKey: [ID, 'sources'] })" in source
    assert "queryClient.invalidateQueries({ queryKey: [ID, 'counts', profile] })" in source
    assert "queryClient.invalidateQueries({ queryKey: [ID, 'events', profile] })" in source
    assert "关闭后停止采集并从普通列表隐藏，历史收藏仍会保留。" in source


@contract
def test_scoped_routes_and_mutations(source):
    for route in ('/events?', '/counts?', '/sources', '/sources/${id}', '/runs/latest', '/updates', '/events/${event.id}/read', '/events/${id}/favorite'):
        assert route in source
    assert "method: 'POST'" in source
    assert source.count("method: 'PATCH'") == 3
    assert 'body: { profile, value: true }' in source
    assert 'body: { profile, value }' in source
    opened = source.split('async function openEvent(event) {')[1].split('function favoriteClick')[0]
    assert opened.index('/read') < opened.index('await refresh()') < opened.index('setPreviewUrl(event.url)')
    assert 'catch' in opened and 'host.notify' in opened
    assert opened.index('validDestination(event.url)') < opened.index('/read')
    assert 'domEvent.stopPropagation()' in source
    # 内嵌 webview 预览面板 + 独立外部打开入口。
    assert "useState(null)" in source
    assert 'setPreviewUrl(event.url)' in source
    assert "jsx('webview'" in source
    assert "getAttribute('src')" in source and "setAttribute('src', url)" in source
    assert "jsx(WebView, { url: displayUrl, zoom, onZoomChange })" in source
    assert "position: 'relative', flex: 1, minWidth: 0, minHeight: 0, overflow: 'hidden'" in source
    assert "position: 'absolute', inset: 0, width: '100%', height: '100%'" in source
    assert 'Ctrl+滚轮缩放' in source
    assert "el.addEventListener('dom-ready', onReady)" in source
    assert "window.addEventListener('wheel'" in source
    assert "Math.max(0.4" in source and "Math.min(1.5" in source
    assert "el.setZoomFactor(zoomRef.current)" in source
    assert "sessionStorage" not in source
    assert "document.documentElement.style.zoom" not in source
    assert "overflow-x: auto !important" in source
    assert "prefers-reduced-motion: reduce" in source
    assert "grid-template-columns 180ms ease-out" in source
    assert "event.ctrlKey && event.key === '0'" in source
    assert '关闭预览' in source
    # 外部打开入口集中在预览面板头部，卡片上不再有。
    assert 'function externalClick' not in source
    assert source.count('link-external') == 1
    assert "partition: 'persist:intelligence-feed'" in source
    assert "width: '100%'" in source and "height: '100%'" in source
    assert 'ctx.os.openExternal' in source
    assert 'value: !event.favorite' in source
    assert "event.favorite ? 'star-full' : 'star-empty'" in source
    assert 'onMutate' not in source  # server-confirmed state, no optimistic loss on error
    assert "queryKey: [ID, 'events', profile]" in source
    assert "queryKey: [ID, 'counts', profile]" in source
    assert 'onError:' in source and "kind: 'error'" in source
    assert "timeoutMs: 160_000" in source
    assert "result.status === 'failed'" in source and "result.status === 'partial'" in source
    assert "[ID, 'runs/latest', profile]" in source


@contract
def test_cards_tags_and_time_display(source):
    # 卡片：主题色边框（减弱 50%），去掉旧的分隔线样式。
    assert "border: `1px solid ${softAccent}`" in source
    assert "const softAccent = 'color-mix(in srgb, var(--ui-accent) 50%, transparent)'" in source
    assert "const softButtonStyle = { background: 'var(--ui-bg-quaternary)'" in source
    # 未读标题用主题蓝色，已读灰色；所有操作按钮（含加载更多/重试）统一柔和填充。
    assert "color: event.read ? muted : 'var(--ui-text-primary)'" in source
    assert "color: event.read ? muted : softAccent" in source
    assert "event.read ? '已读' : '未读'" in source
    assert source.count('style: softButtonStyle') >= 5
    # 领域/阅读筛选沿用 Desktop 原生浅色选中态，非选中项透明。
    assert "const selected = domain === value" in source
    assert "color-mix(in srgb, var(--ui-accent) 85%, transparent)" not in source
    assert "const selectedControlStyle = { background: 'var(--ui-control-active-background)'" in source
    assert "selected ? selectedControlStyle : undefined" in source
    assert "readFilter === value ? selectedControlStyle : undefined" in source
    # 左侧领域按钮与收藏夹固定为同一宽度；只显示正数未读数量并封顶 999。
    domain_button = source.split('function domainButton(value, label) {', 1)[1].split("return jsxs('div'", 1)[0]
    assert "counts.data?.favorites_unread" in domain_button
    assert "Math.min(count, 999)" in domain_button
    assert "count > 0" in domain_button
    assert domain_button.count("width: 120") == 1
    assert "variant: 'ghost', size: 'sm'" in domain_button
    assert "jsx(Codicon, { name: DOMAIN_ICONS[value]" in domain_button
    assert "children: label" in domain_button
    assert "children: countLabel" in domain_button
    for value, icon in (('all', 'home'), ('semiconductor', 'circuit-board'),
                        ('ai', 'hubot'), ('automotive', 'dashboard'),
                        ('music', 'music'), ('weibo', 'flame'), ('favorites', 'star-empty')):
        assert f"{value}: '{icon}'" in source
    # 阅读状态筛选按钮同样的选中高亮。
    assert "readFilter === value" in source
    # 列表右侧为滚动条留位，避免与星标冲突。
    assert 'paddingRight: 12' in source
    assert 'borderRadius: 8' in source
    # 卡片 li 本体不再用 borderBottom（预览面板头部的分隔线不受此限）。
    card_line = next(line for line in source.splitlines() if "padding: '6px 10px'" in line)
    assert 'borderBottom' not in card_line
    # 卡片内部按钮不填充（默认 variant 是主题色填充，必须显式 ghost）。
    assert source.count("variant: 'ghost'") >= 2
    # 紧凑卡片：按钮无内边距，卡片 padding 收紧；星标在卡片边框外侧。
    assert "variant: 'ghost', size: 'inline'" in source
    assert "size: 'icon-xs'" in source
    assert "padding: '6px 10px'" in source
    assert 'marginTop: 2' in source
    assert "useState('all')" in source
    assert "domainButton('all', '全部')" in source
    # 热度元素：多源报道显示 ×N，单源显示 -。
    assert 'event.evidence_count > 1' in source
    assert "'热度：" in source or '热度：' in source
    # 关键词标签：内置词表、最多 3 个、浅色主题色胶囊。
    assert 'const KEYWORDS = [' in source
    assert 'function keywordsFor(headline)' in source
    assert 'if (hits.length === 3) break' in source
    assert 'color-mix(in srgb, var(--ui-accent) 14%, transparent)' in source
    assert 'borderRadius: 999' in source
    # 时间：北京时间数字格式 + 距今相对时间；标签在左、时间在右同一行。
    assert 'function formatTime(iso)' in source
    assert "replace('T', ' ')" in source
    assert 'function relativeTime(iso)' in source
    assert "'刚刚'" in source and '分钟前' in source and '小时前' in source and '天前' in source
    assert "justifyContent: 'space-between'" in source


@contract
def test_navigation_and_reading_toolbar_have_region_dividers(source):
    assert "marginTop: 'auto', borderTop: '1px solid var(--ui-stroke-secondary)', paddingTop: 12" in source
    toolbar = source.split("jsx(Input, { 'aria-label': '搜索资讯'", 1)[1].split("jsx(Dialog,", 1)[0]
    assert "borderBottom: '1px solid var(--ui-stroke-secondary)', paddingBottom: 12" in toolbar


@contract
def test_translated_title_is_primary_and_original_is_preserved(source):
    assert "event.translated_headline || event.headline" in source
    assert "event.translated_headline && jsx('span'" in source
    assert "children: event.headline" in source
    assert "fontSize: 12" in source
    assert "keywordsFor(event.translated_headline || event.headline)" in source


@contract
def test_layout_and_states(source):
    for label in ('全部', '半导体', 'AI', '汽车', '音乐', '热搜', '收藏夹', '未读', '已读', '手动更新', '更新时间',
                  '暂无未读消息', '近 30 天暂无消息', '收藏夹为空', '没有匹配的消息', '加载更多', '重试'):
        assert label in source
    for component in ('Skeleton', 'ErrorState', 'EmptyState', 'ScrollArea'):
        assert re.search(r'jsx[s]?\(' + component + r'\b', source)
    assert "marginTop: 'auto'" in source
    assert "display: 'flex'" in source
    assert 'events.isError' in source and 'events.refetch()' in source
    assert 'events.isLoading' in source
    assert 'pages.flatMap(page => page.items)' in source
    assert 'setPages(previous => [...previous, page])' in source
    assert 'setCursor(page.next_cursor)' in source
    assert 'setPages([])' in source and 'setCursor(null)' in source
    assert 'disabled: events.isFetching || loadingMore' in source
    assert 'fontWeight: event.read ? 400 : 600' in source
    assert 'var(--ui-border)' not in source
    assert 'var(--ui-stroke-secondary)' in source
    assert "'aria-live': 'polite'" in source
    assert "${event.headline}" in source


if __name__ == '__main__':
    import unittest
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(
        unittest.FunctionTestCase(check) for name, check in list(globals().items())
        if name.startswith('test_')
    ))  # pytest and the stdlib runner execute the identical contracts
    raise SystemExit(not result.wasSuccessful())

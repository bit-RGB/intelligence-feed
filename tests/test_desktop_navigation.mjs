import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { test } from 'node:test'

// Execute the real build-free component with deterministic SDK/hook adapters.
const source = readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8')
const sdkNames = source.match(/import \{ ([^}]+) \} from '@hermes\/plugin-sdk'/)[1].split(', ')
function harness() {
  const state = []
  let slot = 0
  const counts = { all: { unread: 1234 }, ai: { unread: 0 }, favorites_unread: 8 }
  const context = Object.fromEntries(sdkNames.map(name => [name, name]))
  Object.assign(context, {
    jsx: (type, props, key) => ({ type, props, key }),
    jsxs: (type, props, key) => ({ type, props, key }),
    useState: initial => {
      const i = slot++
      if (!(i in state)) state[i] = typeof initial === 'function' ? initial() : initial
      return [state[i], next => { state[i] = next }]
    },
    useEffect() {},
    useValue: () => 'default',
    useQueryClient: () => ({}),
    useMutation: () => ({}),
    useQuery: options => ({ data: options.queryKey[1] === 'counts' ? counts : [] }),
    host: { state: { profile: {} } }
  })
  const compiled = source.replace(/^import .*$/gm, '').replace('export default {', 'const pluginDefinition = {')
  runInNewContext(compiled + '\nglobalThis.renderFeed = FeedPage', context)
  const ctx = { storage: { get: (_key, fallback) => fallback } }
  const render = () => { slot = 0; return context.renderFeed({ ctx }) }
  return { render }
}
function find(node, predicate) {
  if (!node || typeof node !== 'object') return undefined
  if (predicate(node)) return node
  const children = node.props?.children
  for (const child of Array.isArray(children) ? children.flat(Infinity) : [children]) {
    const found = find(child, predicate)
    if (found) return found
  }
}
function groups(tree) {
  const nav = find(tree, n => n.props?.['aria-label'] === '资讯分类')
  const navButtons = nav.props.children.filter(n => n?.type === 'Button')
  navButtons.push(nav.props.children.at(-1).props.children)
  const filters = find(tree, n => n.props?.['aria-label'] === '阅读状态').props.children
  return { navButtons, filters }
}

test('navigation keeps equal widths, native icons, unread caps and exactly one selection', () => {
  const app = harness()
  let { navButtons } = groups(app.render())
  assert.equal(navButtons.length, 7)
  assert.deepEqual(Array.from(navButtons, n => n.props.children[0].props.name), ['home', 'circuit-board', 'hubot', 'dashboard', 'music', 'flame', 'star-empty'])
  for (const button of navButtons) {
    assert.equal(button.props.variant, 'ghost')
    assert.equal(button.props.style.width, 120)
  }
  assert.equal(navButtons[0].props.children[2].props.children, ' (999)')
  assert.equal(navButtons[2].props.children[2], '')
  assert.equal(navButtons[6].props.children[2].props.children, ' (8)')
  navButtons[5].props.onClick()
  ;({ navButtons } = groups(app.render()))
  assert.equal(navButtons.filter(n => n.props['aria-pressed']).length, 1)
  assert.equal(navButtons[5].props.style.background, 'var(--ui-control-active-background)')
  assert.equal(navButtons[0].props.style.background, undefined)
  navButtons[6].props.onClick()
  ;({ navButtons } = groups(app.render()))
  assert.equal(navButtons[6].props['aria-pressed'], true)
  assert.equal(navButtons[5].props.style.background, undefined)
})

test('reading filters switch selection without persistent fill on inactive items', () => {
  const app = harness()
  let { filters } = groups(app.render())
  assert.equal(filters[0].props['aria-pressed'], true)
  filters[1].props.onClick()
  ;({ filters } = groups(app.render()))
  assert.equal(filters.filter(n => n.props['aria-pressed']).length, 1)
  assert.equal(filters[1].props.style.background, 'var(--ui-control-active-background)')
  assert.equal(filters[0].props.style, undefined)
  assert.equal(filters[2].props.style, undefined)
  assert.ok(filters.every(n => n.props.variant === 'ghost'))
})

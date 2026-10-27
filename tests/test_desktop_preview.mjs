import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { test } from 'node:test'

const source = readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8')
const compiled = source.replace(/^import .*$/gm, '').replace('export default {', 'const pluginDefinition = {')
function load(extra = {}) {
  const context = {
    jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }),
    Button: 'Button', Codicon: 'Codicon', Input: 'Input',
    ...extra
  }
  runInNewContext(compiled + '\nglobalThis.api = { normalizePreviewZoom, WebView, PreviewPane }', context)
  return context.api
}

test('zoom defaults, validation, bounds and ten-percent steps', () => {
  const { normalizePreviewZoom: zoom } = load()
  for (const invalid of [undefined, null, '', 'bad', Infinity, NaN]) assert.equal(zoom(invalid), 0.8)
  assert.equal(zoom(0), 0.4)
  assert.equal(zoom(3), 1.5)
  assert.equal(zoom(0.86), 0.9)
  assert.equal(zoom('0.7'), 0.7)
})

test('webview applies native zoom per navigation, bridges shortcuts, restores overflow and cleans listeners', async () => {
  const listeners = new Map(), refs = [], effects = [], calls = [], changes = []
  let slot = 0
  let guest, styleText = ''
  const webview = {
    addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: (name, fn) => { if (listeners.get(name) === fn) listeners.delete(name) },
    getAttribute: () => 'https://example.com', setAttribute() {},
    setZoomFactor: value => calls.push(value),
    executeJavaScript: async script => {
      guest = { document: {
        getElementById: () => null,
        createElement: () => ({ set id(v) {}, set textContent(v) { styleText = v } }),
        head: { appendChild() {} }
      }, window: { addEventListener: (name, fn) => guest.events[name] = fn },
      events: {}, console: { debug: message => listeners.get('console-message')?.({ message }) } }
      runInNewContext(script, guest)
    }
  }
  const api = load({
    useRef: initial => refs[slot++] ??= { current: initial },
    useEffect: fn => effects.push(fn)
  })
  const node = api.WebView({ url: 'https://example.com', zoom: 0.8, onZoomChange: value => changes.push(value) })
  node.props.ref.current = webview
  const cleanups = effects.map(fn => fn()).filter(Boolean)
  await listeners.get('dom-ready')()
  assert.equal(calls.at(-1), 0.8)
  assert.match(styleText, /overflow-x:\s*auto\s*!important/)
  let prevented = 0
  guest.events.wheel({ ctrlKey: true, deltaY: -20, preventDefault: () => prevented++, stopPropagation() {} })
  assert.equal(prevented, 1)
  assert.equal(changes.at(-1), 0.9)
  assert.equal(calls.at(-1), 0.9)
  guest.events.keydown({ ctrlKey: true, key: '0', preventDefault() {}, stopPropagation() {} })
  assert.equal(changes.at(-1), 0.8)
  const before = changes.length
  listeners.get('console-message')({ message: 'page-controlled-unrelated-log' })
  assert.equal(changes.length, before)
  await listeners.get('dom-ready')()
  assert.equal(calls.at(-1), 0.8)
  for (const cleanup of cleanups) cleanup()
  assert.equal(listeners.size, 0)
  assert.equal(node.props.webpreferences, 'contextIsolation=yes,nodeIntegration=no,sandbox=yes')
})

test('preview exit retains guest until animation finishes and respects reduced motion', () => {
  const state = [], effects = [], timers = new Map()
  let cursor = 0, serial = 0, reduced = false
  const api = load({
    useState: value => { const i = cursor++; if (!(i in state)) state[i] = typeof value === 'function' ? value() : value; return [state[i], v => state[i] = v] },
    useEffect: fn => effects.push(fn),
    setTimeout: fn => { timers.set(++serial, fn); return serial },
    clearTimeout: id => timers.delete(id),
    window: { matchMedia: () => ({ matches: reduced }) }
  })
  const props = { url: 'https://example.com', zoom: 0.8, onZoomChange() {}, onClose() {}, ctx: {} }
  function render(url) { cursor = 0; effects.length = 0; return api.PreviewPane({ ...props, url }) }
  render(props.url); effects.forEach(fn => fn())
  let node = render(null); effects.forEach(fn => fn())
  node = render(null)
  assert.ok(node.props.children)
  assert.equal(node.props.style.opacity, 0)
  for (const callback of timers.values()) callback()
  node = render(null)
  assert.ok(!node.props.children)
  reduced = true
  render(props.url); effects.forEach(fn => fn())
  render(null); effects.forEach(fn => fn())
  assert.ok(!render(null).props.children)
})
